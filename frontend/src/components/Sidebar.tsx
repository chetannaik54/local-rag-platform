import { useState, type ReactNode } from "react";
import type { Session } from "../types";

interface Props {
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  documents: ReactNode;
}

export function Sidebar({ sessions, activeId, onSelect, onNew, onDelete, documents }: Props) {
  const [tab, setTab] = useState<"chats" | "docs">("chats");
  const tabBtn = (id: "chats" | "docs", label: string) => (
    <button
      role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
      className={`flex-1 border-b-2 px-3 py-2 text-sm font-medium ${
        tab === id ? "border-indigo-500 text-indigo-600 dark:text-indigo-400" : "border-transparent text-slate-500"
      }`}
    >{label}</button>
  );
  return (
    <div className="flex h-full flex-col">
      <div className="p-3">
        <button className="btn-primary w-full" onClick={onNew}>＋ New chat</button>
      </div>
      <div role="tablist" className="flex border-b border-slate-200 dark:border-slate-800">
        {tabBtn("chats", "Chats")}
        {tabBtn("docs", "Documents")}
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {tab === "docs" ? documents : sessions.length === 0 ? (
          <p className="text-center text-sm text-slate-500">No chats yet.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {sessions.map((s) => (
              <li key={s.id} className="group flex items-center">
                <button
                  onClick={() => onSelect(s.id)} aria-current={s.id === activeId}
                  className={`flex-1 truncate rounded-lg px-3 py-2 text-left text-sm ${
                    s.id === activeId ? "bg-slate-100 dark:bg-slate-800" : "hover:bg-slate-50 dark:hover:bg-slate-900"
                  }`}
                  title={s.title}
                >{s.title}</button>
                <button
                  className="btn-ghost px-2 py-1 text-xs opacity-60 hover:opacity-100" aria-label={`Delete chat ${s.title}`}
                  onClick={() => { if (confirm("Delete this chat?")) onDelete(s.id); }}
                >✕</button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
