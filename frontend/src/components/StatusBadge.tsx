import type { Health } from "../types";

interface Props {
  health: Health | null;
  backendError: string | null;
  onRetry: () => void;
}

export function StatusBadge({ health, backendError, onRetry }: Props) {
  let color = "bg-slate-400";
  let label = "Checking…";
  let detail = "";
  if (backendError) {
    color = "bg-red-500"; label = "Backend offline"; detail = backendError;
  } else if (health) {
    if (!health.ollama.reachable) {
      color = "bg-red-500"; label = "Ollama offline"; detail = health.ollama.error ?? "";
    } else if (health.status === "degraded") {
      color = "bg-amber-500"; label = "Model missing"; detail = health.ollama.error ?? "";
    } else {
      color = "bg-emerald-500"; label = health.ollama.chat_model;
      detail = `Ollama ${health.ollama.version ?? ""} · embeddings: ${health.ollama.embedding_model}`;
    }
  }
  return (
    <button
      type="button"
      onClick={onRetry}
      title={detail ? `${detail}\n(click to re-check)` : "Click to re-check"}
      aria-label={`Connection status: ${label}. Click to re-check.`}
      className="flex max-w-full items-center gap-2 rounded-full border border-slate-200 px-3 py-1 text-xs hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
    >
      <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} aria-hidden />
      <span className="truncate" role="status">{label}</span>
    </button>
  );
}
