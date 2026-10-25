import { vi } from "vitest";
import type { Me } from "../types";

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export type Routes = Record<string, (init?: RequestInit, url?: URL) => Response | Promise<Response>>;

export interface Call { key: string; body: any; headers: Record<string, string> }

/** Route fetch by "METHOD /path" (exact or prefix). Unmatched requests fail loudly. Returns the call log. */
export function mockApi(routes: Routes) {
  const calls: Call[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    const key = `${init?.method ?? "GET"} ${url.pathname}`;
    let body: any = undefined;
    try { body = init?.body ? JSON.parse(String(init.body)) : undefined; } catch { body = init?.body; }
    calls.push({ key, body, headers: { ...(init?.headers as Record<string, string>) } });
    const hit = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => key === k || key.startsWith(k));
    if (!hit) return json({ error: { code: "unmocked", message: `unmocked ${key}` } }, 500);
    return routes[hit](init, url);
  });
  return { spy, calls };
}

export const ALL_PERMS = [
  "chat.use", "documents.read", "documents.write", "settings.read", "settings.write", "agents.manage",
  "connectors.manage", "analytics.view", "handoffs.handle", "users.manage", "roles.manage", "embed.manage",
];

export const ROLE_PERMS: Record<string, string[]> = {
  member: ["chat.use", "documents.read"],
  agent: ["chat.use", "documents.read", "settings.read", "handoffs.handle"],
  admin: ALL_PERMS,
  super_admin: [...ALL_PERMS, "platform.manage"],
};

export function meOf(role: keyof typeof ROLE_PERMS | string, over: Partial<Me> = {}): Me {
  return {
    id: `id-${role}`, name: role === "admin" ? "Ada Admin" : `A ${role}`, email: `${role}@acme.test`, org_id: "org-acme",
    org_name: "Acme", role, permissions: ROLE_PERMS[role] ?? ["chat.use"], is_super: role === "super_admin",
    kind: "user", auth_enabled: true, ...over,
  };
}

/** Everything the shell calls on start-up, so tests only mock what they care about. */
export function shellRoutes(me: Me, extra: Routes = {}): Routes {
  return {
    "GET /api/auth/me": () => json(me),
    "GET /api/health": () => json({ status: "ok", ollama: { reachable: true, url: "", version: "x", chat_model: "m", embedding_model: "e", chat_model_available: true, embedding_model_available: true, error: null } }),
    "GET /api/documents": () => json({ items: [], total: 0 }),
    "GET /api/sessions": () => json({ items: [], total: 0 }),
    "GET /api/agents": () => json({ items: [], available_tools: [] }),
    "GET /api/voice/status": () => json({ stt_available: false }),
    "GET /api/orgs": () => json({ items: [] }),
    ...extra,
  };
}
