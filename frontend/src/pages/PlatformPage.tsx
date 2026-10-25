import { useCallback, useEffect, useState } from "react";
import { api } from "../services/api";
import type { Org, Settings } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

interface Props {
  currentOrgId: string;
  onSwitchOrg: (orgId: string) => void;
  onOrgsChanged: () => void;
}

export function PlatformPage({ currentOrgId, onSwitchOrg, onOrgsChanged }: Props) {
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [form, setForm] = useState({ name: "", slug: "", admin_name: "admin", admin_email: "", admin_password: "" });
  const [slugTouched, setSlugTouched] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [o, s] = await Promise.all([api.orgs(), api.platformSettings()]);
      setOrgs(o);
      setSettings(s);
    } catch (e) { setError(errMsg(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    setError(null);
    try {
      await api.createOrg({ ...form, name: form.name.trim(), slug: form.slug.trim() });
      setForm({ name: "", slug: "", admin_name: "admin", admin_email: "", admin_password: "" });
      setSlugTouched(false);
      await load();
      onOrgsChanged();
    } catch (e) { setError(errMsg(e)); }
  };

  const toggle = async (o: Org) => {
    const verb = o.active ? "Disable" : "Enable";
    if (o.active && !confirm(`${verb} ${o.name}? Everyone in it is signed out immediately and its chat widgets stop working.`)) return;
    try { await api.updateOrg(o.id, { active: !o.active }); await load(); onOrgsChanged(); } catch (e) { setError(errMsg(e)); }
  };

  const saveSettings = async () => {
    if (!settings) return;
    setError(null); setSaved(false);
    try { setSettings(await api.savePlatformSettings(settings)); setSaved(true); } catch (e) { setError(errMsg(e)); }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Platform</h1>
      {error && <p role="alert" className="rounded-lg bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{error}</p>}

      <section className="space-y-3">
        <h2 className="font-semibold">Organizations</h2>
        <p className="text-xs text-slate-500">
          Each organization has its own users, roles, documents, chats, assistants and analytics. Nothing is shared between them.
          Use <b>Open</b> to work inside an organization (for support); the header shows which one you are in.
        </p>
        <ul className="space-y-2 text-sm">
          {orgs.map((o) => (
            <li key={o.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <span className="min-w-0 flex-1">
                <b>{o.name}</b> <code className="text-xs text-slate-500">{o.slug}</code>
                {o.id === currentOrgId && <span className="ml-2 rounded bg-indigo-100 px-1.5 text-xs text-indigo-800">current</span>}
                <span className="block text-xs text-slate-500">{o.user_count} user{o.user_count === 1 ? "" : "s"} · {o.document_count} document{o.document_count === 1 ? "" : "s"}</span>
              </span>
              {!o.active && <span className="rounded bg-red-100 px-1.5 text-xs text-red-800">disabled</span>}
              <button className="btn-ghost" disabled={o.id === currentOrgId} onClick={() => onSwitchOrg(o.id)}>Open</button>
              <button className="btn-ghost" onClick={() => toggle(o)}>{o.active ? "Disable" : "Enable"}</button>
            </li>
          ))}
        </ul>

        <fieldset className="space-y-2 rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
          <legend className="px-1 font-medium">New organization</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            <input className="input" aria-label="Organization name" placeholder="Organization name" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value, slug: slugTouched ? form.slug : slugify(e.target.value) })} />
            <input className="input" aria-label="Slug" placeholder="slug (letters, digits, dashes)" value={form.slug}
              onChange={(e) => { setSlugTouched(true); setForm({ ...form, slug: e.target.value }); }} />
            <input className="input" aria-label="Admin name" placeholder="First admin's name" value={form.admin_name} onChange={(e) => setForm({ ...form, admin_name: e.target.value })} />
            <input className="input" type="email" aria-label="Admin email" placeholder="First admin's email" value={form.admin_email} onChange={(e) => setForm({ ...form, admin_email: e.target.value })} />
            <input className="input sm:col-span-2" type="password" autoComplete="new-password" aria-label="Admin password" placeholder="First admin's password (min. 10 characters)" value={form.admin_password} onChange={(e) => setForm({ ...form, admin_password: e.target.value })} />
          </div>
          <button className="btn-primary" disabled={!form.name.trim() || !form.slug.trim() || !form.admin_email.trim() || !form.admin_password} onClick={create}>Create organization</button>
        </fieldset>
      </section>

      {settings && (
        <section className="space-y-3 text-sm">
          <h2 className="font-semibold">Platform-wide settings</h2>
          <p className="text-xs text-slate-500">
            The Ollama server is shared by every organization, so only platform admins can change it. Models and tuning below are the
            <b> defaults</b>; each organization may override everything except the server address.
          </p>
          <label className="block"><span className="font-medium">Ollama server URL</span>
            <input className="input mt-1" value={settings.ollama_url} onChange={(e) => setSettings({ ...settings, ollama_url: e.target.value })} /></label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block"><span className="font-medium">Default chat model</span>
              <input className="input mt-1" value={settings.chat_model} onChange={(e) => setSettings({ ...settings, chat_model: e.target.value })} /></label>
            <label className="block"><span className="font-medium">Default embedding model</span>
              <input className="input mt-1" value={settings.embedding_model} onChange={(e) => setSettings({ ...settings, embedding_model: e.target.value })} /></label>
          </div>
          <div className="flex items-center gap-3">
            <button className="btn-primary" onClick={saveSettings}>Save platform settings</button>
            {saved && <span role="status" className="text-emerald-600">Saved.</span>}
          </div>
        </section>
      )}
    </div>
  );
}
