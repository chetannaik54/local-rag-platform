import { useEffect, useRef, useState } from "react";
import { CitationPanel } from "../components/CitationPanel";
import { MessageBubble } from "../components/MessageBubble";
import { useVoice } from "../hooks/hooks";
import { useChat } from "../hooks/useChat";
import type { Agent, Message } from "../types";

interface Props {
  sessionId: string | null;
  onSession: (id: string) => void;
  selectedDocs: string[];
  hasDocs: boolean;
  onSent: () => void;
  agents?: Agent[];
  agentId?: string | null;
  onAgentChange?: (id: string | null) => void;
  /** agent bound to the open session (shown read-only) */
  sessionAgentId?: string | null;
}

const SUGGESTIONS = ["Summarize the main points of my documents", "What are the key dates mentioned?"];

export function ChatPage({
  sessionId, onSession, selectedDocs, hasDocs, onSent,
  agents = [], agentId = null, onAgentChange, sessionAgentId = null,
}: Props) {
  const chat = useChat(sessionId, onSession, sessionId ? sessionAgentId : agentId);
  const voice = useVoice();
  const [text, setText] = useState("");
  const [panel, setPanel] = useState<{ msg: Message; n?: number } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => { setPanel(null); }, [sessionId]);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [chat.messages]);

  const submit = async (q = text) => {
    const question = q.trim();
    if (!question || chat.sending) return;
    setText("");
    await chat.send(question, selectedDocs.length ? selectedDocs : null);
    onSent();
  };

  const toggleMic = async () => {
    if (voice.recording) { voice.stopRecording(); return; }
    const spoken = await voice.startRecording();
    if (spoken) setText((t) => (t ? `${t} ${spoken}` : spoken));
  };

  const failed = chat.messages.at(-1)?.error;
  const activeAgent = agents.find((a) => a.id === (sessionId ? sessionAgentId : agentId)) ?? agents.find((a) => a.id === "general");

  return (
    <div className="flex h-full min-w-0">
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
          {agents.length > 0 && (
            <label className="flex items-center gap-2">
              <span className="text-slate-500">Assistant</span>
              <select
                className="input w-auto py-1" aria-label="Assistant"
                value={activeAgent?.id ?? "general"} disabled={!!sessionId}
                onChange={(e) => onAgentChange?.(e.target.value === "general" ? null : e.target.value)}
                title={sessionId ? "Start a new chat to switch assistants" : undefined}
              >
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </label>
          )}
          {activeAgent?.description && <span className="hidden text-xs text-slate-500 sm:inline">{activeAgent.description}</span>}
          <div className="ml-auto">
            {chat.handoff ? (
              <button className="btn-ghost text-xs" onClick={chat.backToBot}>Back to assistant</button>
            ) : (
              <button className="btn-ghost text-xs" onClick={chat.requestHuman} disabled={chat.sending}>🙋 Talk to a human</button>
            )}
          </div>
        </div>
        {chat.handoff && (
          <p role="status" className="bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            {chat.handoff === "waiting"
              ? "Waiting for a human agent to join… you can keep typing; they will see your messages."
              : "You're chatting with a human agent."}
          </p>
        )}

        <div className="flex-1 overflow-y-auto px-3 py-4 sm:px-6" aria-live="polite">
          {chat.messages.length === 0 ? (
            <div className="mx-auto mt-16 max-w-md text-center">
              <h1 className="text-xl font-semibold">Ask your documents</h1>
              <p className="mt-2 text-sm text-slate-500">
                {hasDocs
                  ? "Answers come from your uploaded files, with sources you can check. Everything stays on this computer."
                  : "Upload a document from the Documents tab first, then ask a question about it."}
              </p>
              {hasDocs && (
                <div className="mt-4 flex flex-col gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="btn-ghost border border-slate-200 dark:border-slate-800" onClick={() => submit(s)}>{s}</button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col gap-4">
              {chat.messages.map((m, i) => (
                <MessageBubble
                  key={m.id} message={m}
                  onOpenSources={(msg, n) => setPanel({ msg, n })}
                  onRetry={i === chat.messages.length - 1 && failed ? chat.retry : undefined}
                  onRate={chat.rate}
                  onAction={chat.resolveAction}
                  onSpeak={voice.canSpeak ? voice.speak : undefined}
                />
              ))}
              {chat.warnings.map((w) => (
                <p key={w} role="status" className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">{w}</p>
              ))}
              <div ref={bottom} />
            </div>
          )}
        </div>

        {voice.error && <p role="alert" className="px-4 text-xs text-red-600">{voice.error}</p>}
        <form
          onSubmit={(e) => { e.preventDefault(); void submit(); }}
          className="border-t border-slate-200 p-3 dark:border-slate-800"
        >
          <div className="mx-auto flex max-w-3xl items-end gap-2">
            <textarea
              value={text} onChange={(e) => setText(e.target.value)} rows={1}
              aria-label="Message" placeholder={voice.recording ? "Listening…" : "Ask a question about your documents…"}
              className="input max-h-40 min-h-[40px] flex-1 resize-none"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } }}
            />
            {voice.canRecord && (
              <button
                type="button" onClick={toggleMic} disabled={voice.transcribing}
                aria-label={voice.recording ? "Stop recording" : "Speak your question"} aria-pressed={voice.recording}
                className={`btn-ghost border border-slate-300 dark:border-slate-700 ${voice.recording ? "animate-pulse text-red-600" : ""}`}
              >{voice.transcribing ? "…" : voice.recording ? "⏹" : "🎤"}</button>
            )}
            {voice.speaking && (
              <button type="button" className="btn-ghost" onClick={voice.stopSpeaking} aria-label="Stop reading aloud">🔇</button>
            )}
            {chat.sending ? (
              <button type="button" className="btn-ghost border border-slate-300 dark:border-slate-700" onClick={chat.stop}>Stop</button>
            ) : (
              <button type="submit" className="btn-primary" disabled={!text.trim()}>Send</button>
            )}
            {chat.messages.length > 0 && !chat.sending && (
              <button type="button" className="btn-ghost" onClick={chat.clear} title="Clear this conversation">Clear</button>
            )}
          </div>
        </form>
      </section>

      {panel && (
        <div className="fixed inset-0 z-30 border-l border-slate-200 dark:border-slate-800 lg:static lg:inset-auto lg:w-96 lg:shrink-0">
          <CitationPanel message={panel.msg} focus={panel.n} onClose={() => setPanel(null)} />
        </div>
      )}
    </div>
  );
}
