export interface Citation {
  n: number;
  document_id: string;
  filename: string;
  page: number | null;
  chunk_index: number;
  excerpt: string;
  score: number;
  cited: boolean;
}

export interface Message {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "human";
  content: string;
  citations: Citation[];
  tools?: ToolCall[];
  handoff?: boolean;
  feedback?: number | null;
  created_at: string;
  /** client-only */
  streaming?: boolean;
  error?: string;
}

export interface Session {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  agent_id: string | null;
  handoff_status: "waiting" | "active" | null;
}

export interface Doc {
  source_type?: string;
  id: string;
  filename: string;
  size_bytes: number;
  status: "processing" | "ready" | "failed";
  error: string | null;
  page_count: number | null;
  chunk_count: number;
  embedding_model: string | null;
  created_at: string;
}

export interface UploadResult {
  filename: string;
  document: Doc | null;
  duplicate: boolean;
  error: string | null;
}

export interface Settings {
  ollama_url: string;
  chat_model: string;
  embedding_model: string;
  chunk_size: number;
  chunk_overlap: number;
  top_k: number;
  similarity_threshold: number;
  temperature: number;
  num_ctx: number;
  max_tokens: number;
  response_language: string;
}

export interface SettingsResponse {
  settings: Settings;
  validation: {
    ollama_reachable: boolean;
    chat_model_available: boolean;
    embedding_model_available: boolean;
    messages: string[];
  };
  reindex_required: boolean;
  stale_documents: number;
}

export interface Health {
  status: "ok" | "degraded";
  ollama: {
    reachable: boolean;
    url: string;
    version: string | null;
    chat_model: string;
    embedding_model: string;
    chat_model_available: boolean;
    embedding_model_available: boolean;
    error: string | null;
  };
}

export interface ModelInfo {
  name: string;
  size_bytes: number | null;
  family: string | null;
}

export type StreamEvent =
  | { type: "session"; session_id: string }
  | { type: "sources"; citations: Citation[]; warnings: string[] }
  | { type: "token"; text: string }
  | { type: "tool"; name: string; args: Record<string, unknown>; ok: boolean; result: Record<string, unknown> }
  | { type: "handoff"; status: "waiting" | "active" }
  | { type: "done"; message: Message }
  | { type: "error"; code: string; message: string };

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  ok: boolean;
  result: Record<string, unknown>;
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  tools: string[];
  document_ids: string[];
  required_permission: string;
  builtin: boolean;
  created_at: string;
}

export interface ToolInfo { name: string; description: string }

export interface Handoff {
  id: string;
  session_id: string;
  status: "waiting" | "active" | "resolved";
  reason: string;
  assignee: string | null;
  assignee_name: string | null;
  created_at: string;
  session_title: string | null;
  last_user_message: string | null;
}

export interface Canned { id: string; title: string; text: string }

export interface Connector {
  id: string;
  type: "folder" | "url";
  ref: string;
  interval_minutes: number;
  enabled: boolean;
  last_sync: string | null;
  last_status: string | null;
  document_count: number;
}

export interface Analytics {
  days: number;
  total_questions: number;
  answered: number;
  answer_rate: number | null;
  avg_latency_ms: number | null;
  handoffs: number;
  thumbs_up: number;
  thumbs_down: number;
  per_agent: Record<string, number>;
  tool_use: Record<string, number>;
  per_day: { day: string; count: number }[];
  unanswered: { question: string; ts: string; agent_id: string | null }[];
  top_documents: { filename: string; citations: number }[];
}

export interface Me {
  id: string;
  name: string;
  email: string | null;
  org_id: string;
  org_name: string;
  role: string;
  permissions: string[];
  is_super: boolean;
  kind: "user" | "local" | "visitor";
  auth_enabled: boolean;
}

export interface LoginResult { token: string; expires_at: string; me: Me }

export interface AppUser {
  id: string;
  name: string;
  email: string | null;
  role: string;
  role_id: string;
  active: boolean;
  has_password: boolean;
  has_api_key: boolean;
  created_at: string;
  token?: string | null;
}

export interface Role { id: string; name: string; builtin: boolean; permissions: string[]; user_count: number }
export interface PermissionCatalog { permissions: Record<string, string>; assistant_choices: string[] }

export interface Org {
  id: string;
  name: string;
  slug: string;
  active: boolean;
  created_at: string;
  user_count: number;
  document_count: number;
}

export interface EmbedKey {
  id: string;
  name: string;
  token: string;
  snippet: string;
  token_prefix: string;
  allowed_origins: string[];
  agent_id: string | null;
  title: string;
  welcome: string;
  color: string;
  active: boolean;
  created_at: string;
  last_used: string | null;
  wildcard: boolean;
}
