import { useEffect, useRef } from "react";
import type { Message } from "../types";

interface Props {
  message: Message | null;
  focus?: number;
  onClose: () => void;
}

export function CitationPanel({ message, focus, onClose }: Props) {
  const focused = useRef<HTMLLIElement>(null);
  useEffect(() => { focused.current?.scrollIntoView({ block: "nearest" }); }, [focus, message]);
  if (!message) return null;
  return (
    <aside aria-label="Sources" className="flex h-full flex-col bg-white dark:bg-slate-950">
      <div className="flex items-center justify-between border-b border-slate-200 p-3 dark:border-slate-800">
        <h2 className="text-sm font-semibold">Sources</h2>
        <button className="btn-ghost px-2 py-1" onClick={onClose} aria-label="Close sources">✕</button>
      </div>
      <ul className="flex-1 space-y-3 overflow-y-auto p-3">
        {message.citations.map((c) => (
          <li
            key={c.n} ref={c.n === focus ? focused : undefined}
            className={`rounded-lg border p-3 text-sm ${
              c.n === focus ? "border-indigo-500" : "border-slate-200 dark:border-slate-800"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <p className="min-w-0 truncate font-medium">
                <span className="mr-1.5 rounded bg-indigo-100 px-1.5 text-xs text-indigo-700 dark:bg-indigo-900/60 dark:text-indigo-200">{c.n}</span>
                {c.filename}
              </p>
              <span className="shrink-0 text-xs text-slate-500">
                {c.page ? `p. ${c.page} · ` : ""}{Math.round(c.score * 100)}% match
              </span>
            </div>
            {!c.cited && <p className="mt-1 text-xs text-slate-500">Retrieved, not cited in the answer</p>}
            <details className="mt-2" open={c.n === focus}>
              <summary className="cursor-pointer text-xs text-indigo-600 dark:text-indigo-400">Excerpt</summary>
              <blockquote className="mt-2 whitespace-pre-wrap border-l-2 border-slate-300 pl-3 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-400">
                {c.excerpt}
              </blockquote>
            </details>
          </li>
        ))}
      </ul>
    </aside>
  );
}
