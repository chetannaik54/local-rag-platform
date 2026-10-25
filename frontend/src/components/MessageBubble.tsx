import type { Message, ToolCall } from "../types";

interface Props {
  message: Message;
  onOpenSources: (m: Message, n?: number) => void;
  onRetry?: () => void;
  onRate?: (id: string, rating: 1 | -1) => void;
  onSpeak?: (text: string) => void;
  onAction?: OnAction;
}

/** Turns [1] / [1,2] markers into buttons that open the matching source. */
function withCitations(text: string, message: Message, open: Props["onOpenSources"]) {
  const parts = text.split(/(\[\d+(?:\s*,\s*\d+)*\])/g);
  return parts.map((part, i) => {
    const m = /^\[(\d+(?:\s*,\s*\d+)*)\]$/.exec(part);
    if (!m) return <span key={i}>{part}</span>;
    return (
      <span key={i}>
        {m[1].split(/\s*,\s*/).map((s) => {
          const n = Number(s);
          const valid = message.citations.some((c) => c.n === n);
          return valid ? (
            <button
              key={n} onClick={() => open(message, n)} aria-label={`Open source ${n}`}
              className="mx-0.5 rounded bg-indigo-100 px-1.5 text-xs font-semibold text-indigo-700 hover:bg-indigo-200 dark:bg-indigo-900/60 dark:text-indigo-200"
            >{n}</button>
          ) : (
            <span key={n}>[{n}]</span>
          );
        })}
      </span>
    );
  });
}

const TOOL_LABELS: Record<string, string> = {
  get_leave_balance: "Checked leave balance",
  request_leave: "Submitted leave request",
  list_my_leave_requests: "Listed leave requests",
  create_ticket: "Created ticket",
  get_ticket_status: "Looked up ticket",
  list_my_tickets: "Listed tickets",
  request_password_reset: "Logged password reset",
  schedule_followup: "Scheduled follow-up",
  list_followups: "Listed follow-ups",
  business_summary: "Fetched business summary",
  request_human_agent: "Asked for a human agent",
};

type OnAction = (actionId: string, decision: "confirm" | "reject") => void;

function ToolChip({ call, onAction }: { call: ToolCall; onAction?: OnAction }) {
  const status = typeof call.result.status === "string" ? call.result.status : "";
  const actionId = typeof call.result.action_id === "string" ? call.result.action_id : "";

  if (actionId) {  // an action the assistant prepared; nothing happens until the user approves it
    const summary = String(call.result.summary ?? TOOL_LABELS[call.name] ?? call.name);
    const waiting = status === "awaiting_user_confirmation";
    return (
      <div
        role="group" aria-label="Action awaiting your confirmation"
        className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-100"
      >
        <p className="font-semibold">{waiting ? "Please confirm this action" : "Action"}</p>
        <p className="mt-1 break-words">{summary}</p>
        {waiting ? (
          <div className="mt-2 flex gap-2">
            <button className="btn-primary px-3 py-1 text-xs" disabled={!onAction} onClick={() => onAction?.(actionId, "confirm")}>Confirm</button>
            <button className="btn-ghost border border-slate-300 px-3 py-1 text-xs dark:border-slate-600" disabled={!onAction} onClick={() => onAction?.(actionId, "reject")}>Cancel</button>
          </div>
        ) : (
          <p className="mt-1 font-medium">
            {{ confirmed: "✓ Confirmed", failed: "✕ Could not be completed", rejected: "Cancelled", expired: "Expired", cancelled: "Cancelled" }[status] ?? status}
          </p>
        )}
      </div>
    );
  }

  const detail = call.ok ? JSON.stringify(call.result, null, 2) : String(call.result.error ?? "failed");
  return (
    <details className="mt-2 text-xs">
      <summary
        className={`inline-flex cursor-pointer items-center gap-1 rounded-full px-2 py-0.5 ${
          call.ok
            ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"
            : "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200"
        }`}
      >
        {call.ok ? "✓" : "✕"} {TOOL_LABELS[call.name] ?? call.name}
      </summary>
      <pre className="mt-1 max-h-40 overflow-auto rounded bg-white/70 p-2 text-[11px] dark:bg-slate-950/60">{detail}</pre>
    </details>
  );
}
export function MessageBubble({ message, onOpenSources, onRetry, onRate, onSpeak, onAction }: Props) {
  const user = message.role === "user";
  const human = message.role === "human";
  const used = message.citations.filter((c) => c.cited).length;
  const tone = user
    ? "bg-indigo-600 text-white"
    : human
      ? "bg-emerald-100 text-emerald-950 dark:bg-emerald-900/40 dark:text-emerald-50"
      : "bg-slate-100 dark:bg-slate-900";
  return (
    <div className={`flex ${user ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[92%] rounded-2xl px-4 py-3 text-sm leading-relaxed sm:max-w-[80%] ${tone}`}>
        {human && <p className="mb-1 text-xs font-semibold uppercase tracking-wide">Human agent</p>}
        <div className="whitespace-pre-wrap break-words">
          {user || human ? message.content : withCitations(message.content, message, onOpenSources)}
          {message.streaming && !message.content && <span className="text-slate-500">Thinking…</span>}
          {message.streaming && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-slate-400 align-middle" />}
        </div>
        {(message.tools ?? []).map((t, i) => <ToolChip key={i} call={t} onAction={onAction} />)}
        {message.error && (
          <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
            {message.error} {onRetry && <button className="underline" onClick={onRetry}>Retry</button>}
          </p>
        )}
        {message.role === "assistant" && message.citations.length > 0 && !message.streaming && (
          <button
            className="mt-2 block text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400"
            onClick={() => onOpenSources(message)}
          >
            {used > 0 ? `${used} source${used > 1 ? "s" : ""} cited` : "View retrieved sources"} ({message.citations.length} retrieved)
          </button>
        )}
        {message.role === "assistant" && !message.streaming && message.content && !message.id.startsWith("a-") && (
          <div className="mt-2 flex items-center gap-1 text-slate-500">
            {onRate && (
              <>
                <button
                  className={`rounded px-1.5 py-0.5 hover:bg-slate-200 dark:hover:bg-slate-800 ${message.feedback === 1 ? "text-emerald-600" : ""}`}
                  aria-label="Helpful" aria-pressed={message.feedback === 1} onClick={() => onRate(message.id, 1)}
                >👍</button>
                <button
                  className={`rounded px-1.5 py-0.5 hover:bg-slate-200 dark:hover:bg-slate-800 ${message.feedback === -1 ? "text-red-600" : ""}`}
                  aria-label="Not helpful" aria-pressed={message.feedback === -1} onClick={() => onRate(message.id, -1)}
                >👎</button>
              </>
            )}
            {onSpeak && (
              <button
                className="rounded px-1.5 py-0.5 hover:bg-slate-200 dark:hover:bg-slate-800"
                aria-label="Read aloud" onClick={() => onSpeak(message.content)}
              >🔊</button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
