from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


class RuntimeSettings(BaseModel):
    ollama_url: str
    chat_model: str = Field(min_length=1, max_length=200)
    embedding_model: str = Field(min_length=1, max_length=200)
    chunk_size: int = Field(ge=200, le=8000)
    chunk_overlap: int = Field(ge=0, le=2000)
    top_k: int = Field(ge=1, le=20)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    temperature: float = Field(ge=0.0, le=2.0)
    num_ctx: int = Field(ge=512, le=131072)
    max_tokens: int = Field(ge=16, le=16384)
    response_language: str = Field(default="auto", min_length=2, max_length=40)

    @field_validator("ollama_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("must be an http(s) URL such as http://localhost:11434")
        return v

    @field_validator("chat_model", "embedding_model")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def _overlap(self):
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError("chunk_overlap must be less than half of chunk_size")
        return self


class SettingsValidation(BaseModel):
    ollama_reachable: bool
    chat_model_available: bool
    embedding_model_available: bool
    messages: list[str] = []


class SettingsResponse(BaseModel):
    settings: RuntimeSettings
    validation: SettingsValidation
    reindex_required: bool = False
    stale_documents: int = 0


class OllamaStatus(BaseModel):
    reachable: bool
    url: str
    version: str | None = None
    chat_model: str
    embedding_model: str
    chat_model_available: bool = False
    embedding_model_available: bool = False
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    ollama: OllamaStatus


class ModelInfo(BaseModel):
    name: str
    size_bytes: int | None = None
    family: str | None = None


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class DocumentOut(BaseModel):
    id: str
    filename: str
    size_bytes: int
    status: Literal["processing", "ready", "failed"]
    error: str | None = None
    page_count: int | None = None
    chunk_count: int = 0
    embedding_model: str | None = None
    source_type: str = "upload"
    source_ref: str | None = None
    created_at: str


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class UploadResult(BaseModel):
    filename: str
    document: DocumentOut | None = None
    duplicate: bool = False
    error: str | None = None


class UploadResponse(BaseModel):
    results: list[UploadResult]


class Citation(BaseModel):
    n: int
    document_id: str
    filename: str
    page: int | None = None
    chunk_index: int
    excerpt: str
    score: float
    cited: bool = False


class ToolCallOut(BaseModel):
    name: str
    args: dict = {}
    ok: bool
    result: dict = {}

    @field_validator("args", "result", mode="before")
    @classmethod
    def _dict(cls, v):
        return v if isinstance(v, dict) else {}


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "human"]
    content: str
    citations: list[Citation] = []
    tools: list[ToolCallOut] = []
    handoff: bool = False
    feedback: int | None = None
    created_at: str


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    agent_id: str | None = None
    handoff_status: Literal["waiting", "active"] | None = None


class SessionList(BaseModel):
    items: list[SessionOut]
    total: int
    limit: int
    offset: int


class SessionCreate(BaseModel):
    title: str = Field(default="New chat", max_length=120)
    agent_id: str | None = None


class MessageList(BaseModel):
    items: list[MessageOut]
    handoff_status: Literal["waiting", "active"] | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    agent_id: str | None = None  # used only when a new session is created
    document_ids: list[str] | None = None

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class ChatResponse(BaseModel):
    session_id: str
    message: MessageOut
    warnings: list[str] = []


# -- agents ----------------------------------------------------------------
class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    system_prompt: str = Field(min_length=1, max_length=4000)
    tools: list[str] = []
    document_ids: list[str] = []
    required_permission: str = "chat.use"


class AgentOut(AgentIn):
    id: str
    builtin: bool
    created_at: str


class ToolInfo(BaseModel):
    name: str
    description: str


class AgentList(BaseModel):
    items: list[AgentOut]
    available_tools: list[ToolInfo]


# -- handoff ---------------------------------------------------------------
class HandoffRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class HandoffOut(BaseModel):
    id: str
    session_id: str
    status: Literal["waiting", "active", "resolved"]
    reason: str
    assignee: str | None = None
    assignee_name: str | None = None
    created_at: str
    claimed_at: str | None = None
    resolved_at: str | None = None
    session_title: str | None = None
    last_user_message: str | None = None


class HandoffList(BaseModel):
    items: list[HandoffOut]


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ParaphraseIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    tone: Literal["professional", "friendly", "concise", "empathetic"] = "professional"


class ParaphraseOut(BaseModel):
    text: str


class CannedIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)


class CannedOut(CannedIn):
    id: str


class CannedList(BaseModel):
    items: list[CannedOut]


# -- connectors ------------------------------------------------------------
class ConnectorIn(BaseModel):
    type: Literal["folder", "url"]
    ref: str = Field(min_length=1, max_length=1000)
    interval_minutes: int = Field(default=0, ge=0, le=10080)


class ConnectorOut(BaseModel):
    id: str
    type: str
    ref: str
    interval_minutes: int
    enabled: bool
    last_sync: str | None = None
    last_status: str | None = None
    created_at: str
    document_count: int = 0


class ConnectorList(BaseModel):
    items: list[ConnectorOut]
    allowed_roots: list[str]


# -- feedback / users / voice ------------------------------------------------
class FeedbackIn(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=1000)


class TranscriptionOut(BaseModel):
    text: str
    language: str | None = None


class ActionOutcome(BaseModel):
    status: Literal["confirmed", "failed", "rejected"]
    ok: bool
    result: dict = {}
    message: MessageOut


# -- auth / users / roles / organizations -------------------------------------------
class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class Me(BaseModel):
    id: str
    name: str
    email: str | None = None
    org_id: str
    org_name: str
    role: str
    permissions: list[str]
    is_super: bool
    kind: str
    auth_enabled: bool


class LoginOut(BaseModel):
    token: str
    expires_at: str
    me: Me


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    permissions: list[str] = []


class RoleOut(BaseModel):
    id: str
    name: str
    builtin: bool
    permissions: list[str]
    user_count: int = 0


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=60, pattern=r"^[\w .@-]+$")
    role: str = "member"                  # role name or id; legacy "user" means member
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=200)
    api_key: bool = True                  # also issue an API key (shown once)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60, pattern=r"^[\w .@-]+$")
    role: str | None = None
    email: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=200)
    active: bool | None = None


class UserOut(BaseModel):
    id: str
    name: str
    email: str | None = None
    role: str
    role_id: str
    active: bool
    has_password: bool
    has_api_key: bool
    created_at: str


class UserCreated(UserOut):
    token: str | None = None  # API key, shown once


class UserList(BaseModel):
    items: list[UserOut]


class OrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    slug: str = Field(min_length=3, max_length=40)
    admin_name: str = Field(default="admin", min_length=1, max_length=60, pattern=r"^[\w .@-]+$")
    admin_email: str = Field(min_length=3, max_length=200)
    admin_password: str = Field(min_length=1, max_length=200)


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    active: bool | None = None


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    active: bool
    created_at: str
    user_count: int = 0
    document_count: int = 0


class OrgList(BaseModel):
    items: list[OrgOut]


class PermissionCatalog(BaseModel):
    permissions: dict[str, str]
    assistant_choices: list[str]


# -- website embedding ---------------------------------------------------------------
class EmbedKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)
    agent_id: str | None = None
    title: str = Field(default="Ask our assistant", min_length=1, max_length=60)
    welcome: str = Field(default="Hi! How can I help?", min_length=1, max_length=300)
    color: str = Field(default="#4f46e5", pattern=r"^#[0-9a-fA-F]{6}$")


class EmbedKeyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    allowed_origins: list[str] | None = Field(default=None, max_length=20)
    agent_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=60)
    welcome: str | None = Field(default=None, min_length=1, max_length=300)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    active: bool | None = None
    clear_agent: bool = False


class EmbedKeyOut(BaseModel):
    id: str
    name: str
    token: str          # publishable: it is meant to appear in web pages, so admins can re-copy the snippet
    snippet: str = ""
    token_prefix: str
    allowed_origins: list[str]
    agent_id: str | None = None
    title: str
    welcome: str
    color: str
    active: bool
    created_at: str
    last_used: str | None = None
    wildcard: bool = False


class EmbedKeyList(BaseModel):
    items: list[EmbedKeyOut]


class EmbedConfig(BaseModel):
    title: str
    welcome: str
    color: str
    agent_id: str | None = None
