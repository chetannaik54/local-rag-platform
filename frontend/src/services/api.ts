import type {
  Agent, Analytics, AppUser, Canned, Connector, Doc, EmbedKey, Handoff, Health, LoginResult, Me, Message, ModelInfo,
  Org, PermissionCatalog, Role, Session, Settings, SettingsResponse, StreamEvent, ToolInfo, UploadResult,
} from "../types";

// Empty by default: Vite proxies /api in dev, nginx does in Docker. Set VITE_API_URL to override.
const BASE: string = import.meta.env.VITE_API_URL ?? "";
const TOKEN_KEY = "rag-token";

export const auth = {
  get: (): string => {
    try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; }
  },
  set: (t: string) => {
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
  },
};

const ORG_KEY = "rag-org";

/** Platform admins can work "inside" another organization; every request then carries X-Org-Id. */
export const orgContext = {
  get: (): string => {
    try { return localStorage.getItem(ORG_KEY) ?? ""; } catch { return ""; }
  },
  set: (id: string) => {
    try { id ? localStorage.setItem(ORG_KEY, id) : localStorage.removeItem(ORG_KEY); } catch { /* private mode */ }
  },
};

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message);
  }
}

const authHeaders = (): Record<string, string> => {
  const t = auth.get();
  const org = orgContext.get();
  return { ...(t ? { Authorization: `Bearer ${t}` } : {}), ...(org ? { "X-Org-Id": org } : {}) };
};

/** Fired when the server rejects our token, so the app can return to the sign-in screen. */
export const UNAUTHORIZED_EVENT = "rag:unauthorized";

function notifyUnauthorized(status: number, code?: string) {
  // Only a real 401 *while we held a credential* means it went bad (wrong-password 401s on the login form
  // happen with no credential and must not bounce the user around).
  if (status === 401 && code !== "rate_limited" && code !== "invalid_credentials" && auth.get() && typeof window !== "undefined") {
    auth.set("");
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
}

async function toError(res: Response): Promise<ApiError> {
  try {
    const body = await res.json();
    if (body?.error) {
      notifyUnauthorized(res.status, body.error.code);
      return new ApiError(body.error.code, body.error.message, res.status);
    }
  } catch { /* fall through */ }
  notifyUnauthorized(res.status);
  return new ApiError("http_error", `Request failed (HTTP ${res.status})`, res.status);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { ...init, headers: { ...authHeaders(), ...(init.headers as object) } });
  } catch {
    throw new ApiError("backend_unreachable", "Cannot reach the backend. Is it running on port 8000?", 0);
  }
  if (!res.ok) throw await toError(res);
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export interface AgentInput {
  name: string; description: string; system_prompt: string; tools: string[]; document_ids: string[];
  required_permission: string;
}

export interface UserInput { name: string; role: string; email?: string; password?: string; api_key?: boolean }
export interface UserPatch { name?: string; role?: string; email?: string; password?: string; active?: boolean }
export interface EmbedKeyInput {
  name: string; allowed_origins: string[]; agent_id?: string | null; title: string; welcome: string; color: string;
}
export interface OrgInput { name: string; slug: string; admin_name: string; admin_email: string; admin_password: string }

export interface ActionOutcome {
  status: "confirmed" | "failed" | "rejected";
  ok: boolean;
  result: Record<string, unknown>;
  message: Message;
}

/** Fetch every page of a list endpoint (the API caps a page at 200 rows). */
async function allPages<T>(path: string, limit = 200, maxPages = 25): Promise<T[]> {
  const out: T[] = [];
  for (let page = 0; page < maxPages; page++) {
    const sep = path.includes("?") ? "&" : "?";
    const r = await request<{ items: T[]; total: number }>(`${path}${sep}limit=${limit}&offset=${page * limit}`);
    out.push(...r.items);
    if (out.length >= r.total || r.items.length < limit) break;
  }
  return out;
}

export const api = {
  me: () => request<Me>("/api/auth/me"),
  health: () => request<Health>("/api/health"),
  models: () => request<{ models: ModelInfo[] }>("/api/models").then((r) => r.models),
  getSettings: () => request<SettingsResponse>("/api/settings"),
  putSettings: (s: Settings) => request<SettingsResponse>("/api/settings", json("PUT", s)),

  documents: () => allPages<Doc>("/api/documents"),
  deleteDocument: (id: string) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),

  sessions: () => allPages<Session>("/api/sessions"),
  confirmAction: (id: string) => request<ActionOutcome>(`/api/actions/${id}/confirm`, json("POST")),
  rejectAction: (id: string) => request<ActionOutcome>(`/api/actions/${id}/reject`, json("POST")),
  createSession: (agentId: string | null) => request<Session>("/api/sessions", json("POST", { agent_id: agentId })),
  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  clearSession: (id: string) => request<void>(`/api/sessions/${id}/messages`, { method: "DELETE" }),
  messages: (id: string) =>
    request<{ items: Message[]; handoff_status: Session["handoff_status"] }>(`/api/sessions/${id}/messages`),
  feedback: (messageId: string, rating: 1 | -1) =>
    request<void>(`/api/messages/${messageId}/feedback`, json("POST", { rating })),

  agents: () => request<{ items: Agent[]; available_tools: ToolInfo[] }>("/api/agents"),
  saveAgent: (id: string | null, a: AgentInput) =>
    request<Agent>(id ? `/api/agents/${id}` : "/api/agents", json(id ? "PUT" : "POST", a)),
  deleteAgent: (id: string) => request<void>(`/api/agents/${id}`, { method: "DELETE" }),

  requestHuman: (sessionId: string) => request<Handoff>(`/api/sessions/${sessionId}/handoff`, json("POST", {})),
  cancelHandoff: (sessionId: string) => request<void>(`/api/sessions/${sessionId}/handoff`, { method: "DELETE" }),
  handoffs: () => request<{ items: Handoff[] }>("/api/handoffs").then((r) => r.items),
  claim: (id: string) => request<Handoff>(`/api/handoffs/${id}/claim`, json("POST")),
  reply: (id: string, text: string) => request<Message>(`/api/handoffs/${id}/reply`, json("POST", { text })),
  resolve: (id: string) => request<Handoff>(`/api/handoffs/${id}/resolve`, json("POST")),
  paraphrase: (text: string, tone: string) =>
    request<{ text: string }>("/api/handoffs/paraphrase", json("POST", { text, tone })).then((r) => r.text),
  canned: () => request<{ items: Canned[] }>("/api/canned").then((r) => r.items),
  addCanned: (title: string, text: string) => request<Canned>("/api/canned", json("POST", { title, text })),
  deleteCanned: (id: string) => request<void>(`/api/canned/${id}`, { method: "DELETE" }),

  connectors: () => request<{ items: Connector[]; allowed_roots: string[] }>("/api/connectors"),
  addConnector: (type: "folder" | "url", ref: string, interval_minutes: number) =>
    request<Connector>("/api/connectors", json("POST", { type, ref, interval_minutes })),
  syncConnector: (id: string) => request<void>(`/api/connectors/${id}/sync`, json("POST")),
  deleteConnector: (id: string) => request<void>(`/api/connectors/${id}`, { method: "DELETE" }),

  analytics: (days: number) => request<Analytics>(`/api/analytics?days=${days}`),
  // sign in / account
  login: (email: string, password: string) => request<LoginResult>("/api/auth/login", json("POST", { email, password })),
  logout: () => request<void>("/api/auth/logout", json("POST")),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/auth/password", json("POST", { current_password, new_password })),

  // users & roles (RBAC)
  users: () => request<{ items: AppUser[] }>("/api/users").then((r) => r.items),
  createUser: (u: UserInput) => request<AppUser>("/api/users", json("POST", u)),
  updateUser: (id: string, patch: UserPatch) => request<AppUser>(`/api/users/${id}`, json("PUT", patch)),
  resetApiKey: (id: string) => request<AppUser>(`/api/users/${id}/api-key`, json("POST")),
  deleteUser: (id: string) => request<void>(`/api/users/${id}`, { method: "DELETE" }),
  roles: () => request<Role[]>("/api/roles"),
  permissions: () => request<PermissionCatalog>("/api/permissions"),
  saveRole: (id: string | null, name: string, permissions: string[]) =>
    request<Role>(id ? `/api/roles/${id}` : "/api/roles", json(id ? "PUT" : "POST", { name, permissions })),
  deleteRole: (id: string) => request<void>(`/api/roles/${id}`, { method: "DELETE" }),

  // organizations (platform admin)
  orgs: () => request<{ items: Org[] }>("/api/orgs").then((r) => r.items),
  createOrg: (o: OrgInput) => request<Org>("/api/orgs", json("POST", o)),
  updateOrg: (id: string, patch: { name?: string; active?: boolean }) => request<Org>(`/api/orgs/${id}`, json("PUT", patch)),
  platformSettings: () => request<Settings>("/api/platform/settings"),
  savePlatformSettings: (s: Settings) => request<Settings>("/api/platform/settings", json("PUT", s)),

  // website embedding
  embedKeys: () => request<{ items: EmbedKey[] }>("/api/embed-keys").then((r) => r.items),
  createEmbedKey: (k: EmbedKeyInput) => request<EmbedKey>("/api/embed-keys", json("POST", k)),
  updateEmbedKey: (id: string, patch: Partial<EmbedKeyInput> & { active?: boolean; clear_agent?: boolean }) =>
    request<EmbedKey>(`/api/embed-keys/${id}`, json("PUT", patch)),
  deleteEmbedKey: (id: string) => request<void>(`/api/embed-keys/${id}`, { method: "DELETE" }),

  voiceStatus: () => request<{ stt_available: boolean }>("/api/voice/status"),
  transcribe: async (blob: Blob): Promise<string> => {
    const form = new FormData();
    form.append("audio", blob, "speech.webm");
    const r = await request<{ text: string }>("/api/voice/transcribe", { method: "POST", body: form });
    return r.text;
  },
};

/** Upload with progress (fetch cannot report upload progress, XHR can). */
export function uploadFiles(
  files: File[],
  onProgress: (fraction: number) => void,
): Promise<UploadResult[]> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/documents/upload`);
    const t = auth.get();
    if (t) xhr.setRequestHeader("Authorization", `Bearer ${t}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onerror = () =>
      reject(new ApiError("backend_unreachable", "Cannot reach the backend. Is it running on port 8000?", 0));
    xhr.onload = () => {
      let body: any = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (body?.results) return resolve(body.results as UploadResult[]);
      const err = body?.error;
      notifyUnauthorized(xhr.status, err?.code);
      reject(new ApiError(err?.code ?? "http_error", err?.message ?? `Upload failed (HTTP ${xhr.status})`, xhr.status));
    };
    xhr.send(form);
  });
}

/** Parse an SSE byte stream into typed events. */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = /^event: (.+)$/m.exec(block)?.[1];
        const data = /^data: (.+)$/m.exec(block)?.[1];
        if (event && data) yield { type: event, ...JSON.parse(data) } as StreamEvent;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function* streamChat(
  question: string,
  sessionId: string | null,
  documentIds: string[] | null,
  signal: AbortSignal,
  agentId: string | null = null,
): AsyncGenerator<StreamEvent> {
  let res: Response;
  try {
    const init = json("POST", { question, session_id: sessionId, document_ids: documentIds, agent_id: agentId });
    res = await fetch(`${BASE}/api/chat/stream`, {
      ...init, headers: { ...authHeaders(), ...(init.headers as object) }, signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError("backend_unreachable", "Cannot reach the backend. Is it running on port 8000?", 0);
  }
  if (!res.ok || !res.body) throw await toError(res);
  yield* parseSse(res.body);
}
