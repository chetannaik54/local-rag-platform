import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentPanel } from "../components/DocumentPanel";
import { ChatPage } from "../pages/ChatPage";
import { parseSse } from "../services/api";
import type { Doc } from "../types";

const sse = (events: [string, unknown][]) =>
  events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");

function streamResponse(body: string, chunkSize = 17): Response {
  const bytes = new TextEncoder().encode(body);
  const stream = new ReadableStream({
    start(c) {
      for (let i = 0; i < bytes.length; i += chunkSize) c.enqueue(bytes.slice(i, i + chunkSize));
      c.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const citation = {
  n: 1, document_id: "d1", filename: "paris.txt", page: 3, chunk_index: 0,
  excerpt: "The Eiffel Tower is in Paris.", score: 0.82, cited: true,
};

/** Route fetch by URL; `stream` supplies the /api/chat/stream response for each call. */
function mockFetch(stream: () => Response | Promise<Response>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes("/api/voice/status")) return new Response(JSON.stringify({ stt_available: false }), { status: 200 });
    if (url.includes("/api/chat/stream")) return stream();
    return new Response("{}", { status: 404 });
  });
}

afterEach(() => vi.restoreAllMocks());

describe("parseSse", () => {
  it("reassembles events split across arbitrary chunk boundaries", async () => {
    const res = streamResponse(sse([["token", { text: "Hi" }], ["token", { text: " there" }]]), 5);
    const out: string[] = [];
    for await (const ev of parseSse(res.body!)) if (ev.type === "token") out.push(ev.text);
    expect(out).toEqual(["Hi", " there"]);
  });
});

describe("DocumentPanel", () => {
  const base = {
    docs: [] as Doc[], loading: false, error: null, progress: null, results: [],
    onUpload: vi.fn(), onDelete: vi.fn(), onRetry: vi.fn(), selected: [], onToggle: vi.fn(),
  };
  const doc: Doc = {
    id: "d1", filename: "report.pdf", size_bytes: 2048, status: "ready", error: null,
    page_count: 2, chunk_count: 7, embedding_model: "nomic-embed-text", created_at: "2025-01-01T00:00:00Z",
  };

  it("shows an empty state", () => {
    render(<DocumentPanel {...base} />);
    expect(screen.getByText(/No documents yet/)).toBeInTheDocument();
  });

  it("lists documents with status details and failures", () => {
    render(<DocumentPanel {...base} docs={[doc, { ...doc, id: "d2", filename: "bad.pdf", status: "failed", error: "PDF is password-protected." }]} />);
    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText(/7 chunks/)).toBeInTheDocument();
    expect(screen.getByText("PDF is password-protected.")).toBeInTheDocument();
  });

  it("passes chosen files to onUpload and reports per-file errors", async () => {
    const onUpload = vi.fn();
    const { rerender } = render(<DocumentPanel {...base} onUpload={onUpload} />);
    const file = new File(["hello"], "a.txt", { type: "text/plain" });
    await userEvent.upload(screen.getByLabelText("Upload documents"), file);
    expect(onUpload).toHaveBeenCalledWith([file]);

    rerender(<DocumentPanel {...base} results={[{ filename: "x.exe", document: null, duplicate: false, error: "Unsupported file type '.exe'." }]} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Unsupported file type");
  });

  it("shows upload progress", () => {
    render(<DocumentPanel {...base} progress={0.4} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
  });
});

describe("ChatPage", () => {
  const props = { sessionId: null, onSession: vi.fn(), selectedDocs: [], hasDocs: true, onSent: vi.fn() };

  it("streams an answer and exposes its citations", async () => {
    const body = sse([
      ["session", { session_id: "s1" }],
      ["sources", { citations: [citation], warnings: [] }],
      ["token", { text: "It is in Paris " }],
      ["token", { text: "[1]." }],
      ["done", { message: { id: "m1", session_id: "s1", role: "assistant", content: "It is in Paris [1].", citations: [citation], created_at: "" } }],
    ]);
    mockFetch(() => streamResponse(body));
    render(<ChatPage {...props} />);
    await userEvent.type(screen.getByLabelText("Message"), "Where is the tower?{Enter}");

    expect(await screen.findByText(/It is in Paris/)).toBeInTheDocument();
    expect(props.onSession).toHaveBeenCalledWith("s1");
    await userEvent.click(await screen.findByLabelText("Open source 1"));
    expect(screen.getByText("paris.txt")).toBeInTheDocument();
    expect(screen.getByText(/p\. 3/)).toBeInTheDocument();
    expect(screen.getByText("The Eiffel Tower is in Paris.")).toBeInTheDocument();
  });

  it("shows server error events with a retry action", async () => {
    const body = sse([
      ["session", { session_id: "s1" }],
      ["error", { code: "model_missing", message: "Model 'llama3.1:8b' is not installed in Ollama. Run: ollama pull llama3.1:8b" }],
    ]);
    let calls = 0;
    const okBody = sse([["token", { text: "ok" }], ["done", { message: { id: "m", session_id: "s1", role: "assistant", content: "ok", citations: [], created_at: "" } }]]);
    mockFetch(() => streamResponse(++calls === 1 ? body : okBody));
    function Harness() {  // like App: the server-assigned session id flows back in as a prop
      const [id, setId] = useState<string | null>(null);
      return <ChatPage {...props} sessionId={id} onSession={setId} />;
    }
    render(<Harness />);
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("ollama pull llama3.1:8b");

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("ok")).toBeInTheDocument());
    expect(calls).toBe(2);
  });

  it("reports an unreachable backend", async () => {
    mockFetch(() => { throw new TypeError("Failed to fetch"); });
    render(<ChatPage {...props} />);
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the backend");
  });

  it("shows an empty state prompting upload when there are no documents", () => {
    render(<ChatPage {...props} hasDocs={false} />);
    expect(screen.getByText(/Upload a document/)).toBeInTheDocument();
  });
});
