import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamChat } from "../services/api";
import type { Citation, Message, Session } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
type HandoffStatus = Session["handoff_status"];

export function useChat(
  sessionId: string | null,
  onSession: (id: string) => void,
  agentId: string | null = null,
) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [handoff, setHandoff] = useState<HandoffStatus>(null);
  const abort = useRef<AbortController | null>(null);
  const skipLoad = useRef<string | null>(null);
  /** The chat currently on screen. A running stream may only touch the screen while this is its chat. */
  const currentSid = useRef<string | null>(sessionId);
  const lastQuestion = useRef<{ q: string; docs: string[] | null } | null>(null);

  const load = useCallback(async (id: string) => {
    const r = await api.messages(id);
    if (currentSid.current !== id) return;  // user already moved on
    setMessages(r.items);
    setHandoff(r.handoff_status);
  }, []);

  // Load history when switching sessions (but not for a session we just created mid-stream).
  useEffect(() => {
    currentSid.current = sessionId;
    setError(null);
    setWarnings([]);
    setHandoff(null);
    if (skipLoad.current !== sessionId) {
      abort.current?.abort();  // an answer still streaming for the previous chat must not follow us here
    }
    if (!sessionId) { setMessages([]); return; }
    if (skipLoad.current === sessionId) { skipLoad.current = null; return; }
    let live = true;
    api.messages(sessionId)
      .then((r) => { if (live) { setMessages(r.items); setHandoff(r.handoff_status); } })
      .catch((e) => live && setError(errMsg(e)));
    return () => { live = false; };
  }, [sessionId]);

  // While a human is involved, poll for their replies.
  useEffect(() => {
    if (!sessionId || !handoff || sending) return;
    const t = setInterval(() => { load(sessionId).catch(() => {}); }, 2500);
    return () => clearInterval(t);
  }, [sessionId, handoff, sending, load]);

  const patchLast = (fn: (m: Message) => Message) =>
    setMessages((ms) => ms.map((m, i) => (i === ms.length - 1 ? fn(m) : m)));

  const send = useCallback(async (question: string, docIds: string[] | null = null) => {
    lastQuestion.current = { q: question, docs: docIds };
    setError(null);
    setWarnings([]);
    setSending(true);
    const now = new Date().toISOString();
    setMessages((ms) => [
      ...ms,
      { id: `u-${Date.now()}`, session_id: sessionId ?? "", role: "user", content: question, citations: [], created_at: now },
      { id: `a-${Date.now()}`, session_id: sessionId ?? "", role: "assistant", content: "", citations: [], tools: [], created_at: now, streaming: true },
    ]);
    const ctrl = new AbortController();
    abort.current = ctrl;
    let streamSid = sessionId;                       // the chat this stream belongs to
    const live = () => currentSid.current === streamSid;
    let handedOff = false;
    try {
      for await (const ev of streamChat(question, sessionId, docIds, ctrl.signal, agentId)) {
        if (ev.type === "session") {
          if (!sessionId && currentSid.current === null) {  // still on the "new chat" screen: adopt the new session
            streamSid = ev.session_id;
            currentSid.current = ev.session_id;
            skipLoad.current = ev.session_id;
            onSession(ev.session_id);
          }
          continue;
        }
        if (!live()) continue;                       // the user left this chat; drop the rest of its stream
        if (ev.type === "sources") {
          setWarnings(ev.warnings);
          patchLast((m) => ({ ...m, citations: ev.citations as Citation[] }));
        } else if (ev.type === "token") {
          patchLast((m) => ({ ...m, content: m.content + ev.text }));
        } else if (ev.type === "tool") {
          patchLast((m) => ({ ...m, tools: [...(m.tools ?? []), { name: ev.name, args: ev.args, ok: ev.ok, result: ev.result }] }));
        } else if (ev.type === "handoff") {
          handedOff = true;
          setHandoff(ev.status);
        } else if (ev.type === "done") {
          // While a human owns the chat the server returns only the stored user message.
          if (ev.message.role === "user") setMessages((ms) => ms.slice(0, -1));
          else patchLast(() => ({ ...ev.message }));
        } else if (ev.type === "error") {
          patchLast((m) => ({ ...m, streaming: false, error: ev.message }));
          setError(ev.message);
        }
      }
    } catch (e) {
      if (live()) {
        if ((e as Error).name === "AbortError") {
          patchLast((m) => ({ ...m, streaming: false }));
        } else {
          patchLast((m) => ({ ...m, streaming: false, error: errMsg(e) }));
          setError(errMsg(e));
        }
      }
    } finally {
      if (live()) patchLast((m) => (m.streaming ? { ...m, streaming: false } : m));
      if (abort.current === ctrl) { setSending(false); abort.current = null; }
    }
    if (handedOff && streamSid && live()) load(streamSid).catch(() => {});
  }, [sessionId, onSession, agentId, load]);

  const stop = useCallback(() => abort.current?.abort(), []);

  const retry = useCallback(() => {
    const last = lastQuestion.current;
    if (!last) return;
    // drop the failed exchange (user + errored assistant) before re-sending
    setMessages((ms) => ms.slice(0, -2));
    void send(last.q, last.docs);
  }, [send]);

  const clear = useCallback(async () => {
    if (!sessionId) { setMessages([]); return; }
    try {
      await api.clearSession(sessionId);
      setMessages([]);
      setError(null);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [sessionId]);

  const requestHuman = useCallback(async () => {
    try {
      let id = sessionId;
      if (!id) {
        const s = await api.createSession(agentId);
        id = s.id;
        skipLoad.current = id;
        currentSid.current = id;
        onSession(id);
      }
      await api.requestHuman(id);
      await load(id);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [sessionId, agentId, onSession, load]);

  const backToBot = useCallback(async () => {
    if (!sessionId) return;
    try {
      await api.cancelHandoff(sessionId);
      await load(sessionId);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [sessionId, load]);

  const rate = useCallback(async (messageId: string, rating: 1 | -1) => {
    setMessages((ms) => ms.map((m) => (m.id === messageId ? { ...m, feedback: rating } : m)));
    try {
      await api.feedback(messageId, rating);
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  /** Approve or cancel an action the assistant prepared (e.g. a leave request or ticket). */
  const resolveAction = useCallback(async (actionId: string, decision: "confirm" | "reject") => {
    try {
      if (decision === "confirm") await api.confirmAction(actionId);
      else await api.rejectAction(actionId);
    } catch (e) {
      setError(errMsg(e));
    }
    if (sessionId) await load(sessionId).catch(() => {});  // shows the outcome and refreshes the card state
  }, [sessionId, load]);

  return { messages, sending, error, warnings, handoff, send, stop, retry, clear, requestHuman, backToBot, rate, resolveAction };
}
