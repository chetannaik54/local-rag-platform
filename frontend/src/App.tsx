import { useCallback, useEffect, useMemo, useState } from "react";
import { DocumentPanel } from "./components/DocumentPanel";
import { Sidebar } from "./components/Sidebar";
import { StatusBadge } from "./components/StatusBadge";
import { useAgents, useAuth, useDocuments, useHealth, useSessions, useTheme } from "./hooks/hooks";
import { AccountPage } from "./pages/AccountPage";
import { AdminPage } from "./pages/AdminPage";
import { ChatPage } from "./pages/ChatPage";
import { ConsolePage } from "./pages/ConsolePage";
import { LoginPage } from "./pages/LoginPage";
import { PlatformPage } from "./pages/PlatformPage";
import { SettingsPage } from "./pages/SettingsPage";
import { api } from "./services/api";
import type { Me, Org } from "./types";

type View = "chat" | "console" | "admin" | "settings" | "platform" | "account";
type Auth = ReturnType<typeof useAuth>;

const ADMIN_PERMS = ["analytics.view", "agents.manage", "connectors.manage", "users.manage", "roles.manage", "embed.manage"];

export default function App() {
  const auth = useAuth();
  if (auth.loading) return <p className="p-6 text-sm text-slate-500">Loading…</p>;
  if (auth.needsLogin) return <LoginPage onLogin={auth.login} onLoginWithKey={auth.loginWithKey} />;
  if (!auth.me) {
    return (
      <p role="alert" className="p-6 text-sm text-red-600">
        {auth.error} <button className="underline" onClick={auth.retry}>Retry</button>
      </p>
    );
  }
  // Keyed by organization: switching organization remounts everything, so no data from the previous one lingers.
  return <Shell key={auth.me.org_id} auth={auth} me={auth.me} />;
}

function Shell({ auth, me }: { auth: Auth; me: Me }) {
  const theme = useTheme();
  const { health, backendError, refresh: refreshHealth } = useHealth();
  const can = auth.can;
  const docs = useDocuments(can("documents.read"));
  const sessions = useSessions();
  const { agents } = useAgents();
  const [orgs, setOrgs] = useState<Org[]>([]);

  const views = useMemo(() => {
    const v: { id: View; label: string }[] = [];
    if (can("chat.use")) v.push({ id: "chat", label: "Chat" });
    if (can("handoffs.handle")) v.push({ id: "console", label: "Console" });
    if (ADMIN_PERMS.some(can)) v.push({ id: "admin", label: "Admin" });
    if (can("settings.read")) v.push({ id: "settings", label: "Settings" });
    if (can("platform.manage")) v.push({ id: "platform", label: "Platform" });
    return v;
  }, [can]);

  const [view, setView] = useState<View>(views[0]?.id ?? "account");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [menu, setMenu] = useState(false);

  const loadOrgs = useCallback(() => {
    if (me.is_super) api.orgs().then(setOrgs).catch(() => setOrgs([]));
  }, [me.is_super]);
  useEffect(() => { loadOrgs(); }, [loadOrgs]);

  const onSession = useCallback((id: string) => { setSessionId(id); void sessions.refresh(); }, [sessions]);
  const toggle = (id: string) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
  const select = (id: string) => { setSessionId(id); setView("chat"); setMenu(false); };
  const readyDocs = docs.docs.filter((d) => d.status === "ready");
  const sessionAgent = sessions.sessions.find((s) => s.id === sessionId)?.agent_id ?? null;
  const showSidebar = can("chat.use");

  const navBtn = (id: View, label: string) => (
    <button
      key={id} className={`btn-ghost ${view === id ? "bg-slate-100 dark:bg-slate-800" : ""}`}
      aria-current={view === id ? "page" : undefined} onClick={() => { setView(id); setMenu(false); }}
    >{label}</button>
  );

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-800">
        {showSidebar && <button className="btn-ghost lg:hidden" aria-label="Toggle menu" aria-expanded={menu} onClick={() => setMenu(!menu)}>☰</button>}
        <span className="font-semibold">Local RAG</span>
        {me.is_super && orgs.length > 0 ? (
          <label className="flex items-center gap-1 text-xs text-slate-500">
            <span className="sr-only">Organization</span>
            <select
              className="input w-auto py-1 text-xs" aria-label="Organization" value={me.org_id}
              onChange={(e) => void auth.switchOrg(e.target.value)}
            >
              {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}{o.active ? "" : " (disabled)"}</option>)}
            </select>
          </label>
        ) : (
          <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300" title="Your organization">{me.org_name}</span>
        )}
        <nav aria-label="Sections" className="ml-2 hidden items-center gap-1 sm:flex">
          {views.map((v) => navBtn(v.id, v.label))}
        </nav>
        <div className="ml-auto flex min-w-0 items-center gap-2">
          <StatusBadge health={health} backendError={backendError} onRetry={refreshHealth} />
          <button className="btn-ghost" onClick={theme.toggle} aria-label="Toggle dark mode">{theme.dark ? "☀" : "☾"}</button>
          <button
            className={`btn-ghost max-w-[10rem] truncate ${view === "account" ? "bg-slate-100 dark:bg-slate-800" : ""}`}
            onClick={() => setView("account")} title={`${me.name} · ${me.role}`}
          >{me.name}</button>
          {me.auth_enabled && <button className="btn-ghost" onClick={() => void auth.logout()}>Sign out</button>}
        </div>
      </header>
      <nav aria-label="Sections (mobile)" className="flex gap-1 overflow-x-auto border-b border-slate-200 px-2 py-1 dark:border-slate-800 sm:hidden">
        {views.map((v) => navBtn(v.id, v.label))}
      </nav>

      <div className="relative flex min-h-0 flex-1">
        {showSidebar && (
          <aside
            aria-label="Sidebar"
            className={`absolute inset-y-0 left-0 z-20 w-72 border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950 lg:static ${menu ? "block" : "hidden"} lg:block`}
          >
            <Sidebar
              sessions={sessions.sessions} activeId={sessionId}
              onSelect={select}
              onNew={() => { setSessionId(null); setView("chat"); setMenu(false); }}
              onDelete={async (id) => { await sessions.remove(id); if (id === sessionId) setSessionId(null); }}
              documents={
                can("documents.read") ? (
                  <DocumentPanel
                    docs={docs.docs} loading={docs.loading} error={docs.error} progress={docs.progress}
                    results={docs.results} onUpload={docs.upload} onDelete={(id) => { setSelected((s) => s.filter((x) => x !== id)); void docs.remove(id); }}
                    onRetry={docs.refresh} selected={selected} onToggle={toggle} canManage={can("documents.write")}
                  />
                ) : <p className="text-sm text-slate-500">Your role cannot see the document list.</p>
              }
            />
          </aside>
        )}
        <main className="min-w-0 flex-1 overflow-hidden">
          {view === "chat" && can("chat.use") && (
            <ChatPage
              sessionId={sessionId} onSession={onSession} selectedDocs={selected}
              hasDocs={readyDocs.length > 0 || !can("documents.read")} onSent={sessions.refresh}
              agents={agents} agentId={agentId} onAgentChange={setAgentId} sessionAgentId={sessionAgent}
            />
          )}
          {view === "console" && can("handoffs.handle") && <ConsolePage me={me.id} />}
          {view === "admin" && <div className="h-full overflow-y-auto"><AdminPage docs={docs.docs} me={me} /></div>}
          {view === "settings" && can("settings.read") && (
            <div className="h-full overflow-y-auto"><SettingsPage onSaved={refreshHealth} canEdit={can("settings.write")} canEditServer={me.is_super} /></div>
          )}
          {view === "platform" && can("platform.manage") && (
            <div className="h-full overflow-y-auto">
              <PlatformPage currentOrgId={me.org_id} onSwitchOrg={(id) => void auth.switchOrg(id)} onOrgsChanged={loadOrgs} />
            </div>
          )}
          {view === "account" && <div className="h-full overflow-y-auto"><AccountPage me={me} onSignOut={() => void auth.logout()} /></div>}
        </main>
      </div>
    </div>
  );
}
