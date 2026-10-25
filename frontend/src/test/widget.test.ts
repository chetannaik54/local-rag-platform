/**
 * Runs the real backend/app/static/widget.js inside jsdom (which implements Shadow DOM) against a mocked API.
 * This is as close to a browser as the test setup gets; it is not a substitute for a manual check on a real page.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "../../../backend/app/static/widget.js"), "utf8");
const BASE = "https://chat.example.com";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const sse = (events: [string, unknown][]) =>
  new Response(events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join(""), { status: 200 });

type Handler = (init: RequestInit, url: URL) => Response | Promise<Response>;
let calls: { key: string; headers: Record<string, string>; body: any }[] = [];

function mockApi(routes: Record<string, Handler>) {
  calls = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input));
    const key = `${init?.method ?? "GET"} ${url.pathname}`;
    calls.push({ key, headers: { ...(init?.headers as Record<string, string>) }, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const h = routes[key];
    return h ? h(init ?? {}, url) : json({ error: { code: "unmocked", message: key } }, 500);
  });
}

const CONFIG = { title: "Ask Acme", welcome: "Welcome to Acme!", color: "#0a7d3c", agent_id: null };

/** Execute widget.js as if it were loaded by <script src=... data-...>. */
function load(attrs: Record<string, string> = { "data-key": "pk_test" }, parent: HTMLElement = document.body) {
  const script = document.createElement("script");
  script.src = `${BASE}/widget.js`;
  Object.entries(attrs).forEach(([k, v]) => script.setAttribute(k, v));
  parent.appendChild(script);
  Object.defineProperty(document, "currentScript", { value: script, configurable: true });
  new Function(SOURCE)();
  Object.defineProperty(document, "currentScript", { value: null, configurable: true });
  return script;
}

const host = () => document.querySelector("[data-rag-widget]") as HTMLElement;
const shadow = () => host().shadowRoot!;
const q = <T extends Element>(sel: string) => shadow().querySelector(sel) as T;
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

async function openAndType(text: string) {
  (q<HTMLButtonElement>(".bubble")).click();
  const input = q<HTMLInputElement>("input");
  input.value = text;
  q<HTMLFormElement>("form").dispatchEvent(new Event("submit", { cancelable: true }));
  await tick(80);
}

beforeEach(() => {
  document.body.innerHTML = "";
  localStorage.clear();
  sessionStorage.clear();
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => { vi.restoreAllMocks(); });

describe("widget.js", () => {
  it("mounts inside a Shadow DOM, takes its look from the server and talks to the right origin", async () => {
    mockApi({ "GET /api/embed/config": () => json(CONFIG) });
    load();
    expect(host()).toBeTruthy();
    expect(host().shadowRoot).toBeTruthy();
    expect(host().children.length).toBe(0);                                    // nothing leaks into the page's light DOM
    await tick();
    expect(q(".head span").textContent).toBe("Ask Acme");
    expect(shadow().textContent).toContain("Welcome to Acme!");
    expect(q("style").textContent).toContain("#0a7d3c");                       // the configured colour
    const cfg = calls.find((c) => c.key === "GET /api/embed/config")!;
    expect(cfg.headers["X-Embed-Key"]).toBe("pk_test");
    expect(cfg.headers["X-Visitor-Id"]).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  });

  it("keeps one stable anonymous visitor id per browser and never sends credentials of any other kind", async () => {
    mockApi({ "GET /api/embed/config": () => json(CONFIG) });
    load();
    await tick();
    const id1 = calls[0].headers["X-Visitor-Id"];
    expect(localStorage.getItem("rag-visitor")).toBe(id1);
    document.body.innerHTML = "";
    load();
    await tick();
    expect(calls.at(-1)!.headers["X-Visitor-Id"]).toBe(id1);
    for (const c of calls) {
      expect(Object.keys(c.headers).some((h) => /authorization|x-api-key|cookie/i.test(h))).toBe(false);
    }
  });

  it("refuses to start without a key", () => {
    mockApi({});
    load({});
    expect(host()).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("streams an answer into the chat and lists the sources it cites", async () => {
    mockApi({
      "GET /api/embed/config": () => json(CONFIG),
      "POST /api/chat/stream": () => sse([
        ["session", { session_id: "s1" }],
        ["sources", { citations: [{ n: 1, filename: "guide.pdf", page: 4, cited: true }, { n: 2, filename: "other.txt", page: null, cited: false }], warnings: [] }],
        ["token", { text: "Paris " }], ["token", { text: "[1]." }],
        ["done", { message: { role: "assistant", content: "Paris [1]." } }],
      ]),
    });
    load();
    await tick();
    await openAndType("Where is the tower?");
    const bubbles = [...shadow().querySelectorAll(".b")].map((b) => b.textContent);
    expect(bubbles).toContain("Where is the tower?");
    expect(bubbles).toContain("Paris [1].");
    expect(shadow().textContent).toContain("Sources: guide.pdf p.4");
    expect(shadow().textContent).not.toContain("other.txt");                   // only cited sources are named
    const sent = calls.find((c) => c.key === "POST /api/chat/stream")!;
    expect(sent.body).toEqual({ question: "Where is the tower?", session_id: null });
    expect(sessionStorage.getItem(Object.keys(sessionStorage).length ? sessionStorage.key(0)! : "")).toBe("s1");
  });

  it("shows model or document text as plain text, never as HTML", async () => {
    const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
    mockApi({
      "GET /api/embed/config": () => json({ ...CONFIG, title: evil, welcome: evil }),
      "POST /api/chat/stream": () => sse([["session", { session_id: "s1" }], ["token", { text: evil }], ["done", { message: { role: "assistant", content: evil } }]]),
    });
    load();
    await tick();
    await openAndType(evil);
    expect(shadow().querySelector("img, script")).toBeNull();
    expect(shadow().textContent).toContain("<img src=x");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("shows an unavailable message (and no way to send) when the key is rejected", async () => {
    mockApi({ "GET /api/embed/config": () => json({ error: { code: "origin_not_allowed", message: "This website is not allowed to use this chat widget." } }, 403) });
    load();
    await tick();
    expect(shadow().textContent).toContain("This website is not allowed to use this chat widget.");
    expect(q<HTMLInputElement>("input").disabled).toBe(true);
    expect(q<HTMLButtonElement>("button.p").disabled).toBe(true);
  });

  it("reports a failed request in the conversation instead of hanging", async () => {
    mockApi({
      "GET /api/embed/config": () => json(CONFIG),
      "POST /api/chat/stream": () => json({ error: { code: "rate_limited", message: "Too many requests. Please wait a moment and try again." } }, 429),
    });
    load();
    await tick();
    await openAndType("hello");
    expect(shadow().textContent).toContain("Too many requests");
    expect(q<HTMLButtonElement>("button.p").disabled).toBe(false);             // can try again
  });

  it("asks the visitor to confirm an action, and only runs it on Confirm", async () => {
    mockApi({
      "GET /api/embed/config": () => json(CONFIG),
      "POST /api/chat/stream": () => sse([
        ["session", { session_id: "s1" }],
        ["tool", { name: "create_ticket", args: {}, ok: true, result: { status: "awaiting_user_confirmation", action_id: "act1", summary: "Create IT ticket (title: Printer)" } }],
        ["token", { text: "Please confirm." }],
        ["done", { message: { role: "assistant", content: "Please confirm." } }],
      ]),
      "POST /api/actions/act1/confirm": () => json({ status: "confirmed", ok: true, result: {}, message: { content: "Done: Create IT ticket." } }),
    });
    load();
    await tick();
    await openAndType("open a ticket");
    expect(shadow().textContent).toContain("Create IT ticket (title: Printer)");
    expect(calls.some((c) => c.key === "POST /api/actions/act1/confirm")).toBe(false);   // nothing happens yet
    const confirm = [...shadow().querySelectorAll("button")].find((b) => b.textContent === "Confirm")!;
    confirm.click();
    await tick(60);
    expect(calls.some((c) => c.key === "POST /api/actions/act1/confirm")).toBe(true);
    expect(shadow().textContent).toContain("Done: Create IT ticket.");
  });

  it("can hand over to a human and shows the waiting banner", async () => {
    mockApi({
      "GET /api/embed/config": () => json(CONFIG),
      "POST /api/sessions": () => json({ id: "s9" }, 201),
      "POST /api/sessions/s9/handoff": () => json({ id: "h1", status: "waiting" }, 201),
      "GET /api/sessions/s9/messages": () => json({ items: [{ role: "assistant", content: "I've asked a human agent to join." }], handoff_status: "waiting" }),
    });
    load();
    await tick();
    (q<HTMLButtonElement>(".bubble")).click();
    (q<HTMLButtonElement>(".human-btn")).click();
    await tick(100);
    expect(q<HTMLElement>(".banner").hidden).toBe(false);
    expect(q<HTMLElement>(".banner").textContent).toContain("Waiting for a human agent");
    expect(q<HTMLButtonElement>(".human-btn").hidden).toBe(true);
  });

  it("renders inline into a container instead of floating when asked", async () => {
    mockApi({ "GET /api/embed/config": () => json(CONFIG) });
    document.body.innerHTML = '<div id="chat" style="height:500px"></div>';
    load({ "data-key": "pk_test", "data-container": "#chat" });
    await tick();
    expect(document.querySelector("#chat [data-rag-widget]")).toBeTruthy();
    expect(shadow().querySelector(".bubble")).toBeNull();                       // no floating button
    expect(q(".panel").classList.contains("open") || getComputedStyle(q(".panel")).display === "flex").toBe(true);
  });
});
