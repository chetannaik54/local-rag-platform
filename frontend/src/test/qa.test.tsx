/**
 * QA break-tests. Each test asserts the CORRECT behaviour, so a failing test is a confirmed bug.
 * See QA_REPORT.md (IDs QA-08 / QA-09 are the former open bugs, now fixed).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { MessageBubble } from "../components/MessageBubble";
import { CitationPanel } from "../components/CitationPanel";
import { ChatPage } from "../pages/ChatPage";
import type { Message } from "../types";
import { meOf } from "./util";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const msg = (over: Partial<Message>): Message => ({
  id: "m1", session_id: "s", role: "assistant", content: "", citations: [], created_at: "", ...over,
});
const enc = new TextEncoder();

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("QA: streaming vs. navigation", () => {
  // QA-08
  it("a stream that is still running must not write into a different chat after switching sessions", async () => {
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/voice/status")) return json({ stt_available: false });
      if (url.includes("/api/sessions/B/messages")) return json({ items: [msg({ id: "b1", session_id: "B", content: "Chat B history" })], handoff_status: null });
      if (url.includes("/api/sessions/A/messages")) return json({ items: [msg({ id: "a1", session_id: "A", content: "Chat A history" })], handoff_status: null });
      if (url.includes("/api/chat/stream")) {
        return new Response(new ReadableStream({ start(c) { ctrl = c; } }), { status: 200 });
      }
      return json({}, 404);
    });
    const props = { onSession: vi.fn(), selectedDocs: [], hasDocs: true, onSent: vi.fn() };
    const { rerender } = render(<ChatPage {...props} sessionId="A" />);
    await screen.findByText("Chat A history");
    await userEvent.type(screen.getByLabelText("Message"), "question for A{Enter}");
    await waitFor(() => expect(ctrl).toBeDefined());

    rerender(<ChatPage {...props} sessionId="B" />);          // user clicks another chat mid-answer
    await screen.findByText("Chat B history");
    ctrl.enqueue(enc.encode('event: token\ndata: {"text":"LEAKED-ANSWER-FOR-A"}\n\n'));
    ctrl.close();
    await new Promise((r) => setTimeout(r, 200));
    expect(screen.queryByText(/LEAKED-ANSWER-FOR-A/)).not.toBeInTheDocument();
    expect(screen.getByText("Chat B history")).toBeInTheDocument();
  });
});

describe("QA: expired or revoked token", () => {
  // QA-09
  it("returns to the sign-in screen when the API starts answering 401 after login", async () => {
    let revoked = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(String(input), "http://x").pathname;
      if (revoked && url !== "/api/health") return json({ error: { code: "unauthorized", message: "Missing or invalid API token." } }, 401);
      if (url === "/api/auth/me") return json(meOf("member"));
      if (url === "/api/health") return json({ status: "ok", ollama: { reachable: true, url: "", version: "x", chat_model: "m", embedding_model: "e", chat_model_available: true, embedding_model_available: true, error: null } });
      if (url === "/api/documents") return json({ items: [], total: 0 });
      if (url === "/api/sessions") return json({ items: [], total: 0 });
      if (url === "/api/agents") return json({ items: [], available_tools: [] });
      if (url === "/api/voice/status") return json({ stt_available: false });
      return json({}, 404);
    });
    localStorage.setItem("rag-token", "ses_valid");  // a real signed-in user always holds a credential
    render(<App />);
    await screen.findByText("Local RAG");
    revoked = true;
    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    // a correct app would send the user back to sign in; this one just shows an error bubble
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Sign in" })).toBeInTheDocument(), { timeout: 2000 });
  });
});

describe("QA: script injection through model/document text", () => {
  const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
  it("renders hostile message text as text, not HTML", () => {
    const { container } = render(<MessageBubble onOpenSources={vi.fn()} message={msg({ content: evil })} />);
    expect(container.querySelector("img, script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });
  it("renders hostile filenames and excerpts in the citation panel as text", () => {
    const m = msg({ citations: [{ n: 1, document_id: "d", filename: evil, page: 1, chunk_index: 0, excerpt: evil, score: 0.9, cited: true }] });
    const { container } = render(<CitationPanel message={m} onClose={vi.fn()} />);
    expect(container.querySelector("img, script")).toBeNull();
  });
  it("does not turn markdown-ish or javascript: links into anchors", () => {
    const { container } = render(<MessageBubble onOpenSources={vi.fn()} message={msg({ content: "[click](javascript:alert(1))" })} />);
    expect(container.querySelector("a")).toBeNull();
  });
});

describe("QA: citation marker edge cases", () => {
  it("ignores citation numbers that do not exist and does not crash on huge numbers", () => {
    render(<MessageBubble onOpenSources={vi.fn()} message={msg({
      content: "See [99] and [123456789012345678901234567890] and [1].",
      citations: [{ n: 1, document_id: "d", filename: "f.txt", page: null, chunk_index: 0, excerpt: "x", score: 1, cited: true }],
    })} />);
    expect(screen.getAllByLabelText(/Open source/)).toHaveLength(1);
  });
});

describe("QA-01: assistant actions need explicit confirmation", () => {
  const card = (status: string) => msg({
    content: "I prepared a ticket.",
    tools: [{ name: "create_ticket", args: { title: "VPN down" }, ok: true,
      result: { status, action_id: "act1", summary: "Create IT ticket (title: VPN down)" } }],
  });

  it("shows what will happen and offers Confirm / Cancel while waiting", async () => {
    const onAction = vi.fn();
    render(<MessageBubble onOpenSources={vi.fn()} onAction={onAction} message={card("awaiting_user_confirmation")} />);
    expect(screen.getByText(/Create IT ticket \(title: VPN down\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onAction).toHaveBeenCalledWith("act1", "confirm");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onAction).toHaveBeenCalledWith("act1", "reject");
  });

  it.each([["confirmed", /Confirmed/], ["rejected", /Cancelled/], ["expired", /Expired/], ["failed", /Could not be completed/]])(
    "shows the final state '%s' without buttons", (status, text) => {
      render(<MessageBubble onOpenSources={vi.fn()} onAction={vi.fn()} message={card(status)} />);
      expect(screen.getByText(text)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    });

  it("calls the confirm endpoint and then shows the outcome", async () => {
    let confirmed = false;
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://x").pathname;
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/voice/status") return json({ stt_available: false });
      if (url === "/api/sessions/S1/messages") {
        return json({ handoff_status: null, items: [
          msg({ id: "a1", session_id: "S1", content: "I prepared a ticket.", tools: [{ name: "create_ticket", args: {}, ok: true,
            result: { status: confirmed ? "confirmed" : "awaiting_user_confirmation", action_id: "act1", summary: "Create IT ticket (title: VPN down)" } }] }),
          ...(confirmed ? [msg({ id: "a2", session_id: "S1", content: "Done: Create IT ticket." })] : []),
        ] });
      }
      if (url === "/api/actions/act1/confirm") { confirmed = true; return json({ status: "confirmed", ok: true, result: {}, message: msg({}) }); }
      return json({}, 404);
    });
    render(<ChatPage sessionId="S1" onSession={vi.fn()} selectedDocs={[]} hasDocs onSent={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("Done: Create IT ticket.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(calls).toContain("POST /api/actions/act1/confirm");
  });
});

describe("QA-16: long lists are fully loaded", () => {
  it("fetches every page of documents, not just the first 200", async () => {
    const { api } = await import("../services/api");
    const doc = (i: number) => ({ id: `d${i}`, filename: `f${i}.txt`, size_bytes: 1, status: "ready", error: null, page_count: null, chunk_count: 1, embedding_model: "m", created_at: "" });
    const seen: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const u = new URL(String(input), "http://x");
      const offset = Number(u.searchParams.get("offset")), limit = Number(u.searchParams.get("limit"));
      seen.push(`${offset}/${limit}`);
      const items = Array.from({ length: Math.max(0, Math.min(limit, 450 - offset)) }, (_, i) => doc(offset + i));
      return json({ items, total: 450 });
    });
    const docs = await api.documents();
    expect(docs).toHaveLength(450);
    expect(seen).toEqual(["0/200", "200/200", "400/200"]);
  });
});
