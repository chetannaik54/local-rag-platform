import { useRef, useState } from "react";
import type { Doc, UploadResult } from "../types";

interface Props {
  docs: Doc[];
  loading: boolean;
  error: string | null;
  progress: number | null;
  results: UploadResult[];
  onUpload: (files: File[]) => void;
  onDelete: (id: string) => void;
  onRetry: () => void;
  selected: string[];
  onToggle: (id: string) => void;
  canManage?: boolean;
}

const fmtSize = (b: number) =>
  b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

export function DocumentPanel(p: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const manage = p.canManage ?? true;

  return (
    <div className="flex flex-col gap-3">
      {manage && <div
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); p.onUpload(Array.from(e.dataTransfer.files)); }}
        className={`rounded-xl border-2 border-dashed p-4 text-center text-sm transition ${
          over ? "border-indigo-500 bg-indigo-50 dark:bg-indigo-950/40" : "border-slate-300 dark:border-slate-700"
        }`}
      >
        <p className="text-slate-600 dark:text-slate-400">Drag files here or</p>
        <button type="button" className="btn-primary mt-2" onClick={() => input.current?.click()} disabled={p.progress !== null}>
          Choose files
        </button>
        <input
          ref={input} type="file" multiple hidden aria-label="Upload documents"
          accept=".pdf,.txt,.md,.markdown,.docx,.csv"
          onChange={(e) => { p.onUpload(Array.from(e.target.files ?? [])); e.target.value = ""; }}
        />
        <p className="mt-2 text-xs text-slate-500">PDF, TXT, MD, DOCX, CSV</p>
        {p.progress !== null && (
          <div className="mt-3" role="progressbar" aria-valuenow={Math.round(p.progress * 100)} aria-valuemin={0} aria-valuemax={100}>
            <div className="h-1.5 overflow-hidden rounded bg-slate-200 dark:bg-slate-800">
              <div className="h-full bg-indigo-500 transition-all" style={{ width: `${p.progress * 100}%` }} />
            </div>
            <p className="mt-1 text-xs text-slate-500">Uploading… {Math.round(p.progress * 100)}%</p>
          </div>
        )}
      </div>}

      {p.results.filter((r) => r.error || r.duplicate).map((r, i) => (
        <p key={i} role="alert" className={`rounded-lg px-3 py-2 text-xs ${r.error ? "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300" : "bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300"}`}>
          <b>{r.filename}</b>: {r.error ?? "already uploaded — skipped."}
        </p>
      ))}

      {p.error && (
        <div role="alert" className="rounded-lg bg-red-50 p-3 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {p.error} <button className="underline" onClick={p.onRetry}>Retry</button>
        </div>
      )}

      {p.loading ? (
        <p className="text-sm text-slate-500">Loading documents…</p>
      ) : p.docs.length === 0 ? (
        <p className="text-center text-sm text-slate-500">No documents yet. Upload one to start asking questions.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {p.docs.map((d) => (
            <li key={d.id} className="rounded-lg border border-slate-200 p-2 text-sm dark:border-slate-800">
              <div className="flex items-start gap-2">
                <input
                  type="checkbox" className="mt-1" disabled={d.status !== "ready"}
                  checked={p.selected.includes(d.id)} onChange={() => p.onToggle(d.id)}
                  aria-label={`Limit questions to ${d.filename}`}
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium" title={d.filename}>{d.filename}</p>
                  <p className="text-xs text-slate-500">
                    {fmtSize(d.size_bytes)} · {new Date(d.created_at).toLocaleDateString()}
                    {d.status === "ready" && ` · ${d.chunk_count} chunks`}
                  </p>
                  {d.status === "processing" && <p className="text-xs text-indigo-600 dark:text-indigo-400">Indexing…</p>}
                  {d.status === "failed" && <p className="text-xs text-red-600 dark:text-red-400">{d.error ?? "Failed"}</p>}
                </div>
                {manage && <button
                  className="btn-ghost px-2 py-1 text-xs" aria-label={`Delete ${d.filename}`}
                  onClick={() => { if (confirm(`Delete "${d.filename}" and its embeddings?`)) p.onDelete(d.id); }}
                >✕</button>}
              </div>
            </li>
          ))}
        </ul>
      )}
      {p.selected.length > 0 && (
        <p className="text-xs text-slate-500">Questions are limited to {p.selected.length} selected document(s).</p>
      )}
    </div>
  );
}
