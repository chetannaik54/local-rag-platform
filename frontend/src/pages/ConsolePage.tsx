import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { Canned, Handoff, Message } from "../types";

const TONES = ["professional", "friendly", "concise", "empathetic"] as const;
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const ago = (iso: string) => {
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  return m < 1 ? "just now" : m < 60 ? `${m} min` : `${Math.round(m / 60)} h`;
};

export function ConsolePage({ me }: { me: string }) {
  const [queue, setQueue] = useState<Handoff[]>([]);
  const [canned, setCanned] = useState<Canned[]>([]);
  const [selected, setSelected] = useState<Handoff | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [tone, setTone] = useState<(typeof TONES)[number]>("professional");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const loadQueue = useCallback(async () => {
    try {
      const q = await api.handoffs();
      setQueue(q);
      setSelected((s) => (s ? q.find((h) => h.id === s.id) ?? { ...s, status: "resolved" } : s));
    } catch (e) { setError(errMsg(e)); }
  }, []);

  useEffect(() => {
    void loadQueue();
    api.canned().then(setCanned).catch(() => {});
    const t = setInterval(loadQueue, 3000);
    return () => clearInterval(t);
  }, [loadQueue]);

  const sessionId = selected?.session_id;
  useEffect(() => {
    if (!sessionId) { setMessages([]); return; }
    let live = true;
    const pull = () => api.messages(sessionId).then((r) => live && setMessages(r.items)).catch(() => {});
    void pull();
    const t = setInterval(pull, 2000);
    return () => { live = false; clearInterval(t); };
  }, [sessionId]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await loadQueue(); } catch (e) { setError(errMsg(e)); } finally { setBusy(false); }
  };

  const mine = selected?.status === "active" && selected.assignee === me;
  const canWrite = selected && selected.status !== "resolved" && (selected.status === "waiting" || mine);

  const send = () => act(async () => {
    if (!selected || !text.trim()) return;
    await api.reply(selected.id, text.trim());
    setText("");
    setMessages((await api.messages(selected.session_id)).items);
  });

  return (
    <div className="flex h-full min-w-0 flex-col md:flex-row">
      <aside className="max-h-56 shrink-0 overflow-y-auto border-b border-slate-200 p-3 dark:border-slate-800 md:max-h-none md:w-72 md:border-b-0 md:border-r">
        <h1 className="mb-2 text-sm font-semibold">Handoff queue ({queue.length})</h1>
        {queue.length === 0 && <p className="text-sm text-slate-500">No one is waiting for a human right now.</p>}
        <ul className="space-y-2">
          {queue.map((h) => (
            <li key={h.id}>
              <button
                onClick={() => setSelected(h)} aria-current={selected?.id === h.id}
                className={`w-full rounded-lg border p-2 text-left text-sm ${selected?.id === h.id ? "border-indigo-500" : "border-slate-200 dark:border-slate-800"}`}
              >
                <span className="flex items-center justify-between">
                  <span className="truncate font-medium">{h.session_title ?? "Chat"}</span>
                  <span className={`ml-2 shrink-0 rounded px-1.5 text-xs ${h.status === "waiting" ? "bg-amber-100 text-amber-800" : "bg-emerald-100 text-emerald-800"}`}>
                    {h.status === "waiting" ? `waiting ${ago(h.created_at)}` : h.assignee_name ?? "claimed"}
                  </span>
                </span>
                <span className="mt-1 block truncate text-xs text-slate-500">{h.last_user_message ?? h.reason}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        {!selected ? (
          <p className="m-auto p-6 text-sm text-slate-500">Select a conversation from the queue.</p>
        ) : (
          <>
            <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-2 dark:border-slate-800">
              <span className="truncate font-medium">{selected.session_title}</span>
              <span className="text-xs text-slate-500">{selected.status}</span>
              <div className="ml-auto flex gap-2">
                {selected.status === "waiting" && (
                  <button className="btn-primary" disabled={busy} onClick={() => act(() => api.claim(selected.id))}>Claim</button>
                )}
                {selected.status !== "resolved" && (
                  <button className="btn-ghost border border-slate-300 dark:border-slate-700" disabled={busy}
                    onClick={() => act(() => api.resolve(selected.id))}>Resolve &amp; return to bot</button>
                )}
              </div>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
              {messages.map((m) => (
                <div key={m.id} className={`flex ${m.role === "user" ? "justify-start" : "justify-end"}`}>
                  <div className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
                    m.role === "user" ? "bg-slate-100 dark:bg-slate-900"
                      : m.role === "human" ? "bg-emerald-100 text-emerald-950 dark:bg-emerald-900/40 dark:text-emerald-50"
                        : "bg-indigo-50 text-indigo-950 dark:bg-indigo-950/40 dark:text-indigo-100"}`}>
                    <span className="mb-0.5 block text-[10px] font-semibold uppercase tracking-wide opacity-60">
                      {m.role === "user" ? "Customer" : m.role === "human" ? "Agent" : "Bot"}
                    </span>
                    {m.content}
                  </div>
                </div>
              ))}
              <div ref={bottom} />
            </div>
            {error && <p role="alert" className="px-4 text-xs text-red-600">{error}</p>}
            <div className="space-y-2 border-t border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap gap-1">
                {canned.map((c) => (
                  <button key={c.id} className="rounded-full border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
                    title={c.text} disabled={!canWrite} onClick={() => setText(c.text)}>{c.title}</button>
                ))}
              </div>
              <textarea
                className="input min-h-[64px]" aria-label="Reply" value={text} disabled={!canWrite}
                placeholder={canWrite ? "Write a reply…" : selected.status === "resolved" ? "Resolved." : `Claimed by ${selected.assignee_name ?? "a colleague"}.`}
                onChange={(e) => setText(e.target.value)}
              />
              <div className="flex flex-wrap items-center gap-2">
                <select className="input w-auto py-1" aria-label="Tone" value={tone} onChange={(e) => setTone(e.target.value as typeof tone)}>
                  {TONES.map((t) => <option key={t}>{t}</option>)}
                </select>
                <button className="btn-ghost border border-slate-300 dark:border-slate-700" disabled={!canWrite || !text.trim() || busy}
                  onClick={() => act(async () => setText(await api.paraphrase(text, tone)))}>✨ Rewrite with AI</button>
                <button className="btn-primary ml-auto" disabled={!canWrite || !text.trim() || busy} onClick={send}>Send reply</button>
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
