import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { ModelInfo, Settings, SettingsResponse } from "../types";

type NumKey = "chunk_size" | "chunk_overlap" | "top_k" | "similarity_threshold" | "temperature" | "num_ctx" | "max_tokens";

const NUMBERS: { key: NumKey; label: string; step: number; hint: string }[] = [
  { key: "chunk_size", label: "Chunk size (characters)", step: 50, hint: "Applies to documents uploaded after saving." },
  { key: "chunk_overlap", label: "Chunk overlap (characters)", step: 10, hint: "Must be under half the chunk size." },
  { key: "top_k", label: "Top-k retrieved chunks", step: 1, hint: "How many passages are sent to the model." },
  { key: "similarity_threshold", label: "Similarity threshold (0–1)", step: 0.05, hint: "Higher = stricter. Below it, the assistant says it doesn't know." },
  { key: "temperature", label: "Temperature", step: 0.1, hint: "Lower is more factual." },
  { key: "num_ctx", label: "Context window (tokens)", step: 512, hint: "Larger uses more memory." },
  { key: "max_tokens", label: "Max answer tokens", step: 64, hint: "" },
];

export function SettingsPage({ onSaved, canEdit = true, canEditServer = true }: { onSaved: () => void; canEdit?: boolean; canEditServer?: boolean }) {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<Settings | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = async () => {
    try {
      const r = await api.getSettings();
      setData(r); setForm(r.settings); setError(null);
      api.models().then(setModels).catch(() => setModels([]));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => { void load(); }, []);

  if (!form) {
    return (
      <div className="p-6 text-sm">
        {error ? <>{error} <button className="underline" onClick={load}>Retry</button></> : "Loading settings…"}
      </div>
    );
  }

  const save = async () => {
    setSaving(true); setSaved(false); setError(null);
    try {
      const r = await api.putSettings(form);
      setData(r); setForm(r.settings); setSaved(true); onSaved();
      api.models().then(setModels).catch(() => setModels([]));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const modelField = (key: "chat_model" | "embedding_model", label: string) => (
    <label className="block text-sm">
      <span className="font-medium">{label}</span>
      <input
        className="input mt-1" list={`models-${key}`} value={form[key]} disabled={!canEdit}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
      <datalist id={`models-${key}`}>{models.map((m) => <option key={m.name} value={m.name} />)}</datalist>
    </label>
  );

  return (
    <div className="mx-auto max-w-2xl space-y-5 overflow-y-auto p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Settings</h1>

      {data && data.validation.messages.length > 0 && (
        <ul role="alert" className="space-y-1 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {data.validation.messages.map((m) => <li key={m}>{m}</li>)}
        </ul>
      )}
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>}

      <label className="block text-sm">
        <span className="font-medium">Ollama server URL</span>
        <input className="input mt-1" value={canEditServer ? form.ollama_url : "Managed by the platform administrator"}
          disabled={!canEditServer || !canEdit} onChange={(e) => setForm({ ...form, ollama_url: e.target.value })} />
        <span className="text-xs text-slate-500">{canEditServer
          ? "Default http://localhost:11434. Keep this on localhost unless you control access to the server."
          : "The model server is shared by all organizations and is managed by the platform administrator."}</span>
      </label>
      {modelField("chat_model", "Chat model")}
      {modelField("embedding_model", "Embedding model")}
      <p className="text-xs text-slate-500">
        Changing the embedding model does not re-embed existing documents; they must be re-uploaded to be searchable with the new model.
      </p>

      <label className="block text-sm">
        <span className="font-medium">Answer language</span>
        <input
          className="input mt-1" list="languages" value={form.response_language}
          onChange={(e) => setForm({ ...form, response_language: e.target.value })}
        />
        <datalist id="languages">
          {["auto", "English", "Spanish", "French", "German", "Portuguese", "Italian", "Hindi", "Arabic", "Chinese", "Japanese"].map((l) => <option key={l} value={l} />)}
        </datalist>
        <span className="text-xs text-slate-500">
          "auto" replies in the language of the question. For non-English documents, use a multilingual embedding model such as bge-m3 (ollama pull bge-m3).
        </span>
      </label>

      <div className="grid gap-4 sm:grid-cols-2">
        {NUMBERS.map((n) => (
          <label key={n.key} className="block text-sm">
            <span className="font-medium">{n.label}</span>
            <input
              type="number" step={n.step} className="input mt-1" value={form[n.key]} disabled={!canEdit}
              onChange={(e) => setForm({ ...form, [n.key]: Number(e.target.value) })}
            />
            {n.hint && <span className="text-xs text-slate-500">{n.hint}</span>}
          </label>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <button className="btn-primary" onClick={save} disabled={saving || !canEdit}>{saving ? "Saving…" : "Save settings"}</button>
        {saved && <span role="status" className="text-sm text-emerald-600">Saved.</span>}
      </div>
    </div>
  );
}
