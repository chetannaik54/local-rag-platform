import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { MessageBubble } from "../components/MessageBubble";
import { ChatPage } from "../pages/ChatPage";
import { ConsolePage } from "../pages/ConsolePage";
import type { Agent, Handoff, Message } from "../types";
import { meOf, shellRoutes } from "./util";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Routes = Record<string, (init?: RequestInit) => Response | Promise<Response>>;
/** Match by "METHOD /path-prefix"; first match wins. Unmatched requests fail loudly. */
function mockApi(routes: Routes) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    const key = `${init?.method ?? "GET"} ${url.pathname}`;
    const hit = Object.keys(routes).find((k) => key === k || key.startsWith(k));
    if (!hit) return json({ error: { code: "unmocked", message: `unmocked ${key}` } }, 500);
    return routes[hit](init);
  });
}

const sse = (events: [string, unknown][]) =>
  events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");
const sseResponse = (body: string) => new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });

const msg = (over: Partial<Message>): Message => ({
  id: "m1", session_id: "s1", role: "assistant", content: "", citations: [], created_at: "", ...over,
});

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("MessageBubble", () => {
  it("shows tool calls, with failures flagged", () => {
    render(<MessageBubble onOpenSources={vi.fn()} message={msg({
      content: "Done.",
      tools: [
        { name: "request_leave", args: {}, ok: true, result: { status: "pending" } },
        { name: "create_ticket", args: {}, ok: false, result: { error: "title is required" } },
      ],
    })} />);
    expect(screen.getByText(/Submitted leave request/)).toBeInTheDocument();
    expect(screen.getByText(/Created ticket/)).toBeInTheDocument();
    expect(screen.getByText(/title is required/)).toBeInTheDocument();
  });

  it("labels human-agent replies and sends feedback", async () => {
    const onRate = vi.fn();
    const { rerender } = render(<MessageBubble onOpenSources={vi.fn()} message={msg({ role: "human", content: "Hi, I'm Sam." })} />);
    expect(screen.getByText("Human agent")).toBeInTheDocument();
    rerender(<MessageBubble onOpenSources={vi.fn()} onRate={onRate} message={msg({ content: "Answer", feedback: 1 })} />);
    expect(screen.getByLabelText("Helpful")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByLabelText("Not helpful"));
    expect(onRate).toHaveBeenCalledWith("m1", -1);
  });
});

describe("ChatPage with agents and handoff", () => {
  const agents: Agent[] = [
    { id: "general", name: "General assistant", description: "", system_prompt: "", tools: [], document_ids: [], required_permission: "chat.use", builtin: true, created_at: "" },
    { id: "hr", name: "HR assistant", description: "Leave and policy", system_prompt: "", tools: ["get_leave_balance"], document_ids: [], required_permission: "chat.use", builtin: true, created_at: "" },
  ];
  const base = { sessionId: null, onSession: vi.fn(), selectedDocs: [], hasDocs: true, onSent: vi.fn(), agents, sessionAgentId: null };

  it("sends the selected assistant and renders streamed tool calls", async () => {
    let sentBody: Record<string, unknown> = {};
    mockApi({
      "GET /api/voice/status": () => json({ stt_available: false }),
      "POST /api/chat/stream": (init) => {
        sentBody = JSON.parse(String(init?.body));
        return sseResponse(sse([
          ["session", { session_id: "s9" }],
          ["sources", { citations: [], warnings: [] }],
          ["tool", { name: "get_leave_balance", args: {}, ok: true, result: { remaining_days: 20 } }],
          ["token", { text: "You have 20 days left." }],
          ["done", { message: msg({ id: "m9", session_id: "s9", content: "You have 20 days left.", tools: [{ name: "get_leave_balance", args: {}, ok: true, result: { remaining_days: 20 } }] }) }],
        ]));
      },
    });
    const onAgentChange = vi.fn();
    render(<ChatPage {...base} agentId="hr" onAgentChange={onAgentChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Assistant"), "general");
    expect(onAgentChange).toHaveBeenCalledWith(null);
    await userEvent.type(screen.getByLabelText("Message"), "How many leave days?{Enter}");
    expect(await screen.findByText("You have 20 days left.")).toBeInTheDocument();
    expect(screen.getByText(/Checked leave balance/)).toBeInTheDocument();
    expect(sentBody.agent_id).toBe("hr");
  });

  it("requests a human, shows the waiting banner and polls for the agent's reply", async () => {
    let polls = 0;
    mockApi({
      "GET /api/voice/status": () => json({ stt_available: false }),
      "POST /api/sessions": () => json({ id: "s7", title: "New chat", agent_id: null, handoff_status: null, created_at: "", updated_at: "" }, 201),
      "POST /api/sessions/s7/handoff": () => json({ id: "h1", session_id: "s7", status: "waiting" }, 201),
      "GET /api/sessions/s7/messages": () => {
        polls += 1;
        return json(polls === 1
          ? { items: [msg({ id: "a1", session_id: "s7", content: "I've asked a human agent to join." })], handoff_status: "waiting" }
          : { items: [msg({ id: "a1", session_id: "s7", content: "I've asked a human agent to join." }), msg({ id: "h1", session_id: "s7", role: "human", content: "Hi, Sam here." })], handoff_status: "active" });
      },
    });
    const onSession = vi.fn();
    function Harness() {  // mirrors App: the session id returned by the server flows back in as a prop
      const [id, setId] = useState<string | null>(null);
      return <ChatPage {...base} sessionId={id} agentId={null} onSession={(s) => { onSession(s); setId(s); }} />;
    }
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: /Talk to a human/ }));
    expect(await screen.findByText(/Waiting for a human agent/)).toBeInTheDocument();
    expect(await screen.findByText("Hi, Sam here.", {}, { timeout: 6000 })).toBeInTheDocument();
    expect(screen.getByText(/chatting with a human agent/)).toBeInTheDocument();
    expect(onSession).toHaveBeenCalledWith("s7");
  }, 15000);
});

describe("ConsolePage", () => {
  const waiting: Handoff = {
    id: "h1", session_id: "s1", status: "waiting", reason: "wants a person", assignee: null, assignee_name: null,
    created_at: new Date().toISOString(), session_title: "Billing question", last_user_message: "Can I talk to someone?",
  };

  it("claims a conversation, replies with a canned response and resolves", async () => {
    const calls: string[] = [];
    let active = false;
    mockApi({
      "GET /api/handoffs": () => json({ items: [{ ...waiting, status: active ? "active" : "waiting", assignee: active ? "sam" : null }] }),
      "GET /api/canned": () => json({ items: [{ id: "c1", title: "Greeting", text: "Hi, this is Sam from support." }] }),
      "GET /api/sessions/s1/messages": () => json({ items: [msg({ id: "u1", role: "user", content: "Can I talk to someone?" })], handoff_status: "waiting" }),
      "POST /api/handoffs/h1/claim": () => { calls.push("claim"); active = true; return json({ ...waiting, status: "active", assignee: "sam" }); },
      "POST /api/handoffs/h1/reply": (init) => { calls.push(`reply:${JSON.parse(String(init?.body)).text}`); return json(msg({ role: "human" }), 201); },
      "POST /api/handoffs/h1/resolve": () => { calls.push("resolve"); return json({ ...waiting, status: "resolved" }); },
    });
    render(<ConsolePage me="sam" />);
    await userEvent.click(await screen.findByText("Billing question"));
    expect(await screen.findByText("Customer")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Claim" }));
    await waitFor(() => expect(calls).toContain("claim"));
    await userEvent.click(screen.getByRole("button", { name: "Greeting" }));
    expect(screen.getByLabelText("Reply")).toHaveValue("Hi, this is Sam from support.");
    await userEvent.click(screen.getByRole("button", { name: "Send reply" }));
    await waitFor(() => expect(calls).toContain("reply:Hi, this is Sam from support."));
    await userEvent.click(screen.getByRole("button", { name: /Resolve/ }));
    await waitFor(() => expect(calls).toContain("resolve"));
  });

  it("shows an empty queue state", async () => {
    mockApi({
      "GET /api/handoffs": () => json({ items: [] }),
      "GET /api/canned": () => json({ items: [] }),
    });
    render(<ConsolePage me="sam" />);
    expect(await screen.findByText(/No one is waiting/)).toBeInTheDocument();
  });
});

describe("App auth gate", () => {
  it("shows the sign-in form on 401, signs in with email and password, and hides what the role may not use", async () => {
    let authed = false;
    mockApi(shellRoutes(meOf("member"), {
      "GET /api/auth/me": () => authed
        ? json(meOf("member"))
        : json({ error: { code: "unauthorized", message: "Missing or invalid credentials." } }, 401),
      "POST /api/auth/login": () => { authed = true; return json({ token: "ses_abc", expires_at: "2099-01-01", me: meOf("member") }); },
    }));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Email"), "member@acme.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-1");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Local RAG")).toBeInTheDocument();
    expect(localStorage.getItem("rag-token")).toBe("ses_abc");
    // a plain member sees chat only: no Console / Admin / Settings / Platform
    for (const name of ["Console", "Admin", "Settings", "Platform"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });
});
