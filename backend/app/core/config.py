"""Static configuration, read from environment variables / .env."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(ROOT / ".env"), ".env"), extra="ignore"
    )

    # Defaults for user-editable runtime settings (overridable from the Settings page)
    ollama_url: str = "http://localhost:11434"
    chat_model: str = "llama3.1:8b"
    embedding_model: str = "nomic-embed-text"
    chunk_size: int = 1000
    chunk_overlap: int = 150
    top_k: int = 5
    similarity_threshold: float = 0.35
    temperature: float = 0.2
    num_ctx: int = 8192
    max_tokens: int = 1024
    response_language: str = "auto"

    # Deployment-level settings (env only)
    auth_enabled: bool = False          # off = single local user with admin rights
    admin_token: str = ""               # optional fixed bootstrap admin API key
    admin_email: str = ""               # platform admin login (default admin@localhost)
    admin_password: str = ""            # platform admin password (default: generated, saved to data/)
    session_hours: int = 12             # how long a login stays valid
    min_password_length: int = 10
    login_failures_per_15min: int = 8   # wrong passwords per email before a temporary lockout
    connector_roots: str = ""           # comma-separated folders connectors may read (default data/sources)
    allow_private_urls: bool = False    # URL connector may fetch localhost/LAN addresses
    widget_origins: str = ""            # extra origins allowed to call the API from a browser (besides embed keys)
    public_url: str = ""               # how browsers reach this server (used in embed snippets)
    whisper_model: str = "base"         # faster-whisper model for speech-to-text
    confirm_actions: bool = True        # state-changing assistant tools wait for the user to press Confirm
    rate_limit_per_minute: int = 60     # chat/upload requests per user per minute (0 = off)
    auth_failures_per_minute: int = 20  # wrong-token attempts per client per minute
    max_json_body_kb: int = 256         # request body cap for non-upload endpoints
    data_dir: Path = ROOT / "data"
    max_upload_mb: int = 50
    ollama_timeout_s: float = 120.0
    embed_batch_size: int = 32
    history_messages: int = 6
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in f"{self.cors_origins},{self.widget_origins}".split(",") if o.strip()]

    @property
    def connector_root_list(self) -> list[Path]:
        roots = [r.strip() for r in self.connector_roots.split(",") if r.strip()]
        return [Path(r).resolve() for r in roots] or [(self.data_dir / "sources").resolve()]
