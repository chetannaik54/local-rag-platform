import { useCallback, useEffect, useState } from "react";
import { api, type AgentInput } from "../services/api";
import type {
  Agent, Analytics, AppUser, Canned, Connector, Doc, EmbedKey, Me, PermissionCatalog, Role, ToolInfo,
} from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
/** Each tab needs one permission; people only see the tabs they may use. */
const TABS = [
  { name: "Analytics", perm: "analytics.view" },
  { name: "Assistants", perm: "agents.manage" },
  { name: "Connectors", perm: "connectors.manage" },
  { name: "Users", perm: "users.manage" },
  { name: "Roles", perm: "roles.manage" },
  { name: "Website embed", perm: "embed.manage" },
  { name: "Canned replies", perm: "handoffs.handle" },
] as const;
type Tab = (typeof TABS)[number]["name"];

const WHO_CAN_USE: Record<string, string> = {
  "chat.use": "Everyone who is signed in (and website visitors, if embedded)",
  "handoffs.handle": "Support agents and admins",
  "analytics.view": "People who can view analytics",
  "users.manage": "Admins only",
};

function Err({ msg }: { msg: string | null }) {
  return msg ? <p role="alert" className="rounded-lg bg-red-50 p-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">{msg}</p> : null;
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-xl font-semibold">{value}</p>
    </div>
  );
}

function AnalyticsTab() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.analytics(days).then(setData).catch((e) => setError(errMsg(e))); }, [days]);
  if (error) return <Err msg={error} />;
  if (!data) return <p className="text-sm text-slate-500">Loading…</p>;
  const max = Math.max(1, ...data.per_day.map((d) => d.count));
  return (
    <div className="space-y-5">
      <label className="text-sm">Period{" "}
        <select className="input inline w-auto py-1" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[7, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Questions" value={data.total_questions} />
        <Stat label="Answered from documents/tools" value={data.answer_rate == null ? "–" : `${Math.round(data.answer_rate * 100)}%`} />
        <Stat label="Avg response time" value={data.avg_latency_ms == null ? "–" : `${(data.avg_latency_ms / 1000).toFixed(1)} s`} />
        <Stat label="Human handoffs" value={data.handoffs} />
        <Stat label="👍 Helpful" value={data.thumbs_up} />
        <Stat label="👎 Not helpful" value={data.thumbs_down} />
      </div>
      <section>
        <h3 className="mb-2 text-sm font-semibold">Questions per day</h3>
        {data.per_day.length === 0 ? <p className="text-sm text-slate-500">No questions yet.</p> : (
          <div className="flex h-24 items-end gap-1" role="img" aria-label="Questions per day">
            {data.per_day.map((d) => (
              <div key={d.day} title={`${d.day}: ${d.count}`} className="flex-1 rounded-t bg-indigo-500" style={{ height: `${(d.count / max) * 100}%`, minHeight: 2 }} />
            ))}
          </div>
        )}
      </section>
      <div className="grid gap-5 md:grid-cols-2">
        <section>
          <h3 className="mb-2 text-sm font-semibold">Unanswered questions (knowledge gaps)</h3>
          {data.unanswered.length === 0 ? <p className="text-sm text-slate-500">None 🎉</p> : (
            <ul className="space-y-1 text-sm">{data.unanswered.map((u, i) => <li key={i} className="rounded bg-amber-50 px-2 py-1 dark:bg-amber-950/30">{u.question}</li>)}</ul>
          )}
        </section>
        <section>
          <h3 className="mb-2 text-sm font-semibold">Most cited documents</h3>
          {data.top_documents.length === 0 ? <p className="text-sm text-slate-500">No citations yet.</p> : (
            <ul className="space-y-1 text-sm">{data.top_documents.map((d) => <li key={d.filename} className="flex justify-between"><span className="truncate">{d.filename}</span><span>{d.citations}</span></li>)}</ul>
          )}
        </section>
        <section>
          <h3 className="mb-2 text-sm font-semibold">By assistant</h3>
          <ul className="space-y-1 text-sm">{Object.entries(data.per_agent).map(([k, v]) => <li key={k} className="flex justify-between"><span>{k}</span><span>{v}</span></li>)}</ul>
        </section>
        <section>
          <h3 className="mb-2 text-sm font-semibold">Actions taken</h3>
          {Object.keys(data.tool_use).length === 0 ? <p className="text-sm text-slate-500">No tool use yet.</p> : (
            <ul className="space-y-1 text-sm">{Object.entries(data.tool_use).map(([k, v]) => <li key={k} className="flex justify-between"><span>{k}</span><span>{v}</span></li>)}</ul>
          )}
        </section>
      </div>
    </div>
  );
}

const BLANK: AgentInput = { name: "", description: "", system_prompt: "", tools: [], document_ids: [], required_permission: "chat.use" };

function AgentsTab({ docs }: { docs: Doc[] }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [editing, setEditing] = useState<string | null | "new">(null);
  const [form, setForm] = useState<AgentInput>(BLANK);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.agents().then((r) => { setAgents(r.items); setTools(r.available_tools); }).catch((e) => setError(errMsg(e))), []);
  useEffect(() => { void load(); }, [load]);

  const edit = (a: Agent | null) => {
    setError(null);
    setEditing(a ? a.id : "new");
    setForm(a ? { name: a.name, description: a.description, system_prompt: a.system_prompt, tools: a.tools, document_ids: a.document_ids, required_permission: a.required_permission } : BLANK);
  };
  const toggle = (key: "tools" | "document_ids", v: string) =>
    setForm((f) => ({ ...f, [key]: f[key].includes(v) ? f[key].filter((x) => x !== v) : [...f[key], v] }));
  const save = async () => {
    try { await api.saveAgent(editing === "new" ? null : editing, form); setEditing(null); await load(); } catch (e) { setError(errMsg(e)); }
  };

  return (
    <div className="space-y-3">
      <Err msg={error} />
      {editing === null ? (
        <>
          <button className="btn-primary" onClick={() => edit(null)}>＋ New assistant</button>
          <ul className="space-y-2">
            {agents.map((a) => (
              <li key={a.id} className="flex items-start gap-3 rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">{a.name} {a.builtin && <span className="ml-1 rounded bg-slate-200 px-1.5 text-xs dark:bg-slate-800">built-in</span>}</p>
                  <p className="text-xs text-slate-500">{a.description}</p>
                  <p className="mt-1 text-xs text-slate-500">
                    Tools: {a.tools.length ? a.tools.join(", ") : "none"} · Documents: {a.document_ids.length ? `${a.document_ids.length} selected` : "all"} · Available to: {WHO_CAN_USE[a.required_permission] ?? a.required_permission}
                  </p>
                </div>
                <button className="btn-ghost" onClick={() => edit(a)}>Edit</button>
                {!a.builtin && <button className="btn-ghost text-red-600" onClick={async () => { if (confirm(`Delete ${a.name}?`)) { await api.deleteAgent(a.id).catch((e) => setError(errMsg(e))); await load(); } }}>Delete</button>}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <div className="space-y-3">
          <label className="block text-sm"><span className="font-medium">Name</span>
            <input className="input mt-1" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="block text-sm"><span className="font-medium">Description</span>
            <input className="input mt-1" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
          <label className="block text-sm"><span className="font-medium">Instructions (system prompt)</span>
            <textarea className="input mt-1 min-h-[96px]" value={form.system_prompt} onChange={(e) => setForm({ ...form, system_prompt: e.target.value })} /></label>
          <label className="block text-sm"><span className="font-medium">Who can use this assistant</span>
            <select className="input mt-1 w-auto" aria-label="Who can use this assistant" value={form.required_permission}
              onChange={(e) => setForm({ ...form, required_permission: e.target.value })}>
              {Object.entries(WHO_CAN_USE).map(([perm, label]) => <option key={perm} value={perm}>{label}</option>)}
            </select></label>
          <fieldset className="text-sm"><legend className="font-medium">Tools (actions)</legend>
            <div className="mt-1 grid gap-1 sm:grid-cols-2">
              {tools.filter((t) => t.name !== "request_human_agent").map((t) => (
                <label key={t.name} className="flex items-start gap-2" title={t.description}>
                  <input type="checkbox" checked={form.tools.includes(t.name)} onChange={() => toggle("tools", t.name)} />
                  <span>{t.name}</span>
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-slate-500">Assistants with tools can also escalate to a human.</p>
          </fieldset>
          <fieldset className="text-sm"><legend className="font-medium">Knowledge scope</legend>
            <p className="text-xs text-slate-500">Leave all unchecked to search every document.</p>
            {docs.filter((d) => d.status === "ready").map((d) => (
              <label key={d.id} className="flex items-center gap-2">
                <input type="checkbox" checked={form.document_ids.includes(d.id)} onChange={() => toggle("document_ids", d.id)} />
                <span className="truncate">{d.filename}</span>
              </label>
            ))}
          </fieldset>
          <div className="flex gap-2">
            <button className="btn-primary" onClick={save} disabled={!form.name.trim() || !form.system_prompt.trim()}>Save</button>
            <button className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function ConnectorsTab() {
  const [items, setItems] = useState<Connector[]>([]);
  const [roots, setRoots] = useState<string[]>([]);
  const [type, setType] = useState<"folder" | "url">("folder");
  const [ref, setRef] = useState("");
  const [interval, setIntervalMin] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.connectors().then((r) => { setItems(r.items); setRoots(r.allowed_roots); }).catch((e) => setError(errMsg(e))), []);
  useEffect(() => { void load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);
  const add = async () => {
    setError(null);
    try { await api.addConnector(type, ref.trim(), interval); setRef(""); await load(); } catch (e) { setError(errMsg(e)); }
  };
  return (
    <div className="space-y-4">
      <Err msg={error} />
      <div className="space-y-2 rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
        <div className="flex flex-wrap gap-2">
          <select className="input w-auto" aria-label="Connector type" value={type} onChange={(e) => setType(e.target.value as "folder" | "url")}>
            <option value="folder">Local folder</option><option value="url">Web page URL</option>
          </select>
          <input className="input min-w-0 flex-1" aria-label="Folder path or URL" value={ref} onChange={(e) => setRef(e.target.value)}
            placeholder={type === "folder" ? `${roots[0] ?? "C:\\path\\to\\folder"}` : "https://example.com/policy"} />
          <label className="flex items-center gap-1">Refresh every
            <input type="number" min={0} className="input w-20" value={interval} onChange={(e) => setIntervalMin(Number(e.target.value))} /> min
          </label>
          <button className="btn-primary" disabled={!ref.trim()} onClick={add}>Add &amp; sync</button>
        </div>
        <p className="text-xs text-slate-500">0 = manual sync only. Folders must be inside: {roots.join(", ") || "…"}. Private-network URLs are blocked by default.</p>
      </div>
      {items.length === 0 ? <p className="text-sm text-slate-500">No connectors yet.</p> : (
        <ul className="space-y-2">
          {items.map((c) => (
            <li key={c.id} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
              <div className="flex items-center gap-2">
                <span className="rounded bg-slate-200 px-1.5 text-xs dark:bg-slate-800">{c.type}</span>
                <span className="min-w-0 flex-1 truncate" title={c.ref}>{c.ref}</span>
                <button className="btn-ghost" onClick={() => api.syncConnector(c.id).then(load).catch((e) => setError(errMsg(e)))}>Sync now</button>
                <button className="btn-ghost text-red-600" onClick={async () => { if (confirm("Remove this connector and its documents?")) { await api.deleteConnector(c.id).catch((e) => setError(errMsg(e))); await load(); } }}>Remove</button>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {c.document_count} document(s) · every {c.interval_minutes || "–"} min · last sync {c.last_sync ? new Date(c.last_sync).toLocaleString() : "never"} · {c.last_status ?? "syncing…"}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UsersTab({ me }: { me: Me }) {
  const [users, setUsers] = useState<AppUser[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [form, setForm] = useState({ name: "", email: "", role: "member", password: "", api_key: false });
  const [secret, setSecret] = useState<string | null>(null);
  const [pwFor, setPwFor] = useState<string | null>(null);
  const [pw, setPw] = useState("");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { const [u, r] = await Promise.all([api.users(), api.roles()]); setUsers(u); setRoles(r); }
    catch (e) { setError(errMsg(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  // You can only hand out roles made of permissions you hold yourself.
  const grantable = (r: Role) => r.permissions.every((p) => me.permissions.includes(p));
  const run = async (fn: () => Promise<unknown>) => { setError(null); try { await fn(); await load(); } catch (e) { setError(errMsg(e)); } };

  const create = () => run(async () => {
    const u = await api.createUser({
      name: form.name.trim(), role: form.role, email: form.email.trim() || undefined,
      password: form.password || undefined, api_key: form.api_key || !form.password,
    });
    setSecret(u.token ? `${u.name}: ${u.token}` : null);
    setForm({ name: "", email: "", role: "member", password: "", api_key: false });
  });

  return (
    <div className="space-y-4 text-sm">
      <Err msg={error} />
      <fieldset className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
        <legend className="px-1 font-medium">Add a user</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <input className="input" aria-label="Name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="input" aria-label="Email" type="email" placeholder="Email (needed to sign in)" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <input className="input" aria-label="Initial password" type="password" autoComplete="new-password" placeholder="Initial password (min. 10 characters)" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <select className="input" aria-label="Role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
            {roles.map((r) => <option key={r.id} value={r.id} disabled={!grantable(r)}>{r.name}{r.builtin ? "" : " (custom)"}{grantable(r) ? "" : " - you cannot grant this"}</option>)}
          </select>
        </div>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.checked })} /> Also create an API key (for scripts and integrations)</label>
        <button className="btn-primary" disabled={!form.name.trim()} onClick={create}>Create user</button>
      </fieldset>
      {secret && (
        <p role="status" className="break-all rounded-lg bg-emerald-50 p-2 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          Copy this API key now; it will not be shown again. <b>{secret}</b>
        </p>
      )}
      <ul className="space-y-2">
        {users.map((u) => {
          const self = u.id === me.id;
          const role = roles.find((r) => r.id === u.role_id);
          const touchable = !role || grantable(role);
          return (
            <li key={u.id} className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1">
                  <b>{u.name}</b> {self && <span className="text-xs text-slate-500">(you)</span>}
                  <span className="block truncate text-xs text-slate-500">{u.email ?? "no email"} · {u.has_password ? "password" : "no password"}{u.has_api_key ? " · API key" : ""}</span>
                </span>
                {!u.active && <span className="rounded bg-red-100 px-1.5 text-xs text-red-800">disabled</span>}
                <select className="input w-auto py-1" aria-label={`Role of ${u.name}`} value={u.role_id} disabled={self || !touchable}
                  onChange={(e) => run(() => api.updateUser(u.id, { role: e.target.value }))}>
                  {roles.map((r) => <option key={r.id} value={r.id} disabled={!grantable(r)}>{r.name}</option>)}
                  {!roles.some((r) => r.id === u.role_id) && <option value={u.role_id}>{u.role}</option>}
                </select>
                <button className="btn-ghost" disabled={!touchable} onClick={() => setPwFor(pwFor === u.id ? null : u.id)}>Password…</button>
                <button className="btn-ghost" disabled={!touchable} onClick={() => run(async () => { const k = await api.resetApiKey(u.id); setSecret(`${u.name}: ${k.token}`); })}>New API key</button>
                <button className="btn-ghost" disabled={self || !touchable} onClick={() => run(() => api.updateUser(u.id, { active: !u.active }))}>{u.active ? "Disable" : "Enable"}</button>
                <button className="btn-ghost text-red-600" disabled={self || !touchable} onClick={() => { if (confirm(`Delete ${u.name}?`)) void run(() => api.deleteUser(u.id)); }}>Delete</button>
              </div>
              {pwFor === u.id && (
                <div className="mt-2 flex gap-2">
                  <input className="input" type="password" autoComplete="new-password" aria-label={`New password for ${u.name}`} placeholder="New password" value={pw} onChange={(e) => setPw(e.target.value)} />
                  <button className="btn-primary" disabled={!pw} onClick={() => run(async () => { await api.updateUser(u.id, { password: pw }); setPw(""); setPwFor(null); })}>Set password</button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function RolesTab({ me }: { me: Me }) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [catalog, setCatalog] = useState<PermissionCatalog | null>(null);
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [name, setName] = useState("");
  const [perms, setPerms] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { const [r, c] = await Promise.all([api.roles(), api.permissions()]); setRoles(r); setCatalog(c); }
    catch (e) { setError(errMsg(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const edit = (r: Role | null) => { setError(null); setEditing(r ? r.id : "new"); setName(r?.name ?? ""); setPerms(r?.permissions ?? ["chat.use"]); };
  const toggle = (p: string) => setPerms((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  const save = async () => {
    try { await api.saveRole(editing === "new" ? null : editing, name.trim(), perms); setEditing(null); await load(); }
    catch (e) { setError(errMsg(e)); }
  };
  const assignable = Object.entries(catalog?.permissions ?? {}).filter(([p]) => p !== "platform.manage");

  return (
    <div className="space-y-3 text-sm">
      <Err msg={error} />
      {editing === null ? (
        <>
          <p className="text-xs text-slate-500">Built-in roles are fixed. Create your own to give people exactly the access they need. You can only include permissions you have yourself.</p>
          <button className="btn-primary" onClick={() => edit(null)}>＋ New role</button>
          <ul className="space-y-2">
            {roles.map((r) => (
              <li key={r.id} className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                <div className="flex items-center gap-2">
                  <b className="flex-1">{r.name} {r.builtin && <span className="ml-1 rounded bg-slate-200 px-1.5 text-xs font-normal dark:bg-slate-800">built-in</span>}</b>
                  <span className="text-xs text-slate-500">{r.user_count} user{r.user_count === 1 ? "" : "s"}</span>
                  {!r.builtin && <button className="btn-ghost" onClick={() => edit(r)}>Edit</button>}
                  {!r.builtin && <button className="btn-ghost text-red-600" onClick={async () => { if (confirm(`Delete role ${r.name}?`)) { await api.deleteRole(r.id).catch((e) => setError(errMsg(e))); await load(); } }}>Delete</button>}
                </div>
                <p className="mt-1 text-xs text-slate-500">{r.permissions.map((p) => catalog?.permissions[p] ?? p).join(" · ")}</p>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <div className="space-y-3">
          <label className="block"><span className="font-medium">Role name</span>
            <input className="input mt-1" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <fieldset><legend className="font-medium">Permissions</legend>
            <div className="mt-1 space-y-1">
              {assignable.map(([p, label]) => {
                const held = me.permissions.includes(p);
                return (
                  <label key={p} className={`flex items-start gap-2 ${held ? "" : "opacity-50"}`} title={held ? undefined : "You do not have this permission yourself"}>
                    <input type="checkbox" checked={perms.includes(p)} disabled={!held} onChange={() => toggle(p)} aria-label={p} />
                    <span><code className="text-xs">{p}</code> <span className="text-slate-500">{label}</span></span>
                  </label>
                );
              })}
            </div>
          </fieldset>
          <div className="flex gap-2">
            <button className="btn-primary" disabled={name.trim().length < 2 || perms.length === 0} onClick={save}>Save role</button>
            <button className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}

function EmbedTab() {
  const [keys, setKeys] = useState<EmbedKey[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [form, setForm] = useState({ name: "", origins: "", agent: "", title: "Ask our assistant", welcome: "Hi! How can I help?", color: "#4f46e5" });
  const [copied, setCopied] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { const [k, a] = await Promise.all([api.embedKeys(), api.agents()]); setKeys(k); setAgents(a.items.filter((x) => x.required_permission === "chat.use")); }
    catch (e) { setError(errMsg(e)); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const origins = form.origins.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
  const create = async () => {
    setError(null);
    try {
      await api.createEmbedKey({ name: form.name.trim(), allowed_origins: origins, agent_id: form.agent || null, title: form.title, welcome: form.welcome, color: form.color });
      setForm({ ...form, name: "", origins: "" });
      await load();
    } catch (e) { setError(errMsg(e)); }
  };
  const copy = async (k: EmbedKey) => {
    try { await navigator.clipboard.writeText(k.snippet); setCopied(k.id); setTimeout(() => setCopied(null), 2000); } catch { setError("Copy failed. Select the code and copy it manually."); }
  };
  const demoUrl = (k: EmbedKey) => `${/src="([^"]+)\/widget\.js"/.exec(k.snippet)?.[1] ?? ""}/widget-demo?key=${encodeURIComponent(k.token)}`;

  return (
    <div className="space-y-4 text-sm">
      <Err msg={error} />
      <p className="text-xs text-slate-500">
        Put the chat on your website with one line of code. Each key works only on the websites you list, can only chat (no documents, no settings), and
        answers with one assistant. Keys are publishable: they are meant to appear in your page source.
      </p>
      <fieldset className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
        <legend className="px-1 font-medium">New embed key</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <input className="input" aria-label="Key name" placeholder="Name, e.g. Company website" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select className="input" aria-label="Assistant" value={form.agent} onChange={(e) => setForm({ ...form, agent: e.target.value })}>
            <option value="">General assistant</option>
            {agents.filter((a) => a.id !== "general").map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          <input className="input" aria-label="Widget title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <label className="flex items-center gap-2">Colour <input type="color" aria-label="Colour" value={form.color} onChange={(e) => setForm({ ...form, color: e.target.value })} /></label>
        </div>
        <input className="input" aria-label="Welcome message" value={form.welcome} onChange={(e) => setForm({ ...form, welcome: e.target.value })} />
        <textarea className="input min-h-[64px]" aria-label="Allowed websites" placeholder={"Allowed websites, one per line:\nhttps://www.example.com\nhttps://*.example.org"} value={form.origins} onChange={(e) => setForm({ ...form, origins: e.target.value })} />
        <button className="btn-primary" disabled={!form.name.trim() || origins.length === 0} onClick={create}>Create key</button>
      </fieldset>

      {keys.length === 0 ? <p className="text-slate-500">No embed keys yet.</p> : (
        <ul className="space-y-3">
          {keys.map((k) => (
            <li key={k.id} className="space-y-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2">
                <b className="flex-1">{k.name}</b>
                <span className={`rounded px-1.5 text-xs ${k.active ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-700"}`}>{k.active ? "active" : "disabled"}</span>
                <button className="btn-ghost" onClick={async () => { await api.updateEmbedKey(k.id, { active: !k.active }).catch((e) => setError(errMsg(e))); await load(); }}>{k.active ? "Disable" : "Enable"}</button>
                <a className="btn-ghost" href={demoUrl(k)} target="_blank" rel="noreferrer">Try it</a>
                <button className="btn-ghost text-red-600" onClick={async () => { if (confirm(`Delete key ${k.name}? Websites using it will stop working.`)) { await api.deleteEmbedKey(k.id).catch((e) => setError(errMsg(e))); await load(); } }}>Delete</button>
              </div>
              {k.wildcard && <p role="alert" className="rounded bg-amber-50 p-2 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">This key accepts <b>any</b> website. Anyone can copy it and use your chat (and your model capacity). List your own sites instead.</p>}
              <p className="text-xs text-slate-500">Websites: {k.allowed_origins.join(", ")} · Last used: {k.last_used ? new Date(k.last_used).toLocaleString() : "never"}</p>
              <div className="flex gap-2">
                <textarea readOnly rows={2} className="input font-mono text-xs" aria-label={`Embed code for ${k.name}`} value={k.snippet} onFocus={(e) => e.currentTarget.select()} />
                <button className="btn-ghost border border-slate-300 dark:border-slate-700" onClick={() => copy(k)}>{copied === k.id ? "Copied ✓" : "Copy"}</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CannedTab() {
  const [items, setItems] = useState<Canned[]>([]);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.canned().then(setItems).catch((e) => setError(errMsg(e))), []);
  useEffect(() => { void load(); }, [load]);
  return (
    <div className="space-y-3 text-sm">
      <Err msg={error} />
      <div className="space-y-2">
        <input className="input" aria-label="Title" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <textarea className="input" aria-label="Text" placeholder="Reply text" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="btn-primary" disabled={!title.trim() || !text.trim()}
          onClick={async () => { await api.addCanned(title.trim(), text.trim()).catch((e) => setError(errMsg(e))); setTitle(""); setText(""); await load(); }}>Add canned reply</button>
      </div>
      <ul className="space-y-1">
        {items.map((c) => (
          <li key={c.id} className="flex items-start gap-2 rounded border border-slate-200 px-3 py-2 dark:border-slate-800">
            <div className="flex-1"><p className="font-medium">{c.title}</p><p className="text-xs text-slate-500">{c.text}</p></div>
            <button className="btn-ghost text-red-600" onClick={async () => { await api.deleteCanned(c.id).catch((e) => setError(errMsg(e))); await load(); }}>Delete</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AdminPage({ docs, me }: { docs: Doc[]; me: Me }) {
  const visible = TABS.filter((tab) => me.permissions.includes(tab.perm));
  const [tab, setTab] = useState<Tab | null>(null);
  const current = tab && visible.some((v) => v.name === tab) ? tab : visible[0]?.name;
  if (!current) return <p className="p-6 text-sm text-slate-500">Your role has no administration permissions.</p>;
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Admin <span className="text-sm font-normal text-slate-500">· {me.org_name}</span></h1>
      <div role="tablist" className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
        {visible.map((v) => (
          <button key={v.name} role="tab" aria-selected={current === v.name} onClick={() => setTab(v.name)}
            className={`border-b-2 px-3 py-2 text-sm font-medium ${current === v.name ? "border-indigo-500 text-indigo-600 dark:text-indigo-400" : "border-transparent text-slate-500"}`}>{v.name}</button>
        ))}
      </div>
      {current === "Analytics" && <AnalyticsTab />}
      {current === "Assistants" && <AgentsTab docs={docs} />}
      {current === "Connectors" && <ConnectorsTab />}
      {current === "Users" && <UsersTab me={me} />}
      {current === "Roles" && <RolesTab me={me} />}
      {current === "Website embed" && <EmbedTab />}
      {current === "Canned replies" && <CannedTab />}
    </div>
  );
}
