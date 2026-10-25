"""SQLite persistence for documents, chat sessions/messages and runtime settings."""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import AppConfig
from app.models.schemas import RuntimeSettings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  page_count INTEGER,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  embedding_model TEXT,
  stored_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  citations TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# Settings only the platform operator may change (organisations inherit them).
PLATFORM_ONLY = {"ollama_url"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: Path, config: AppConfig):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- helpers -----------------------------------------------------------
    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    # public aliases used by app.database.repos
    run = _run
    all = _all
    one = _one

    @contextmanager
    def transaction(self):
        """All-or-nothing block for migrations. Yields the raw connection (do not call run()/all() inside)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """Additive, idempotent migration: add any missing columns."""
        have = {r["name"] for r in self._all(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                self._run(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- settings ----------------------------------------------------------
    def defaults(self) -> dict:
        c = self._config
        return {
            "ollama_url": c.ollama_url,
            "chat_model": c.chat_model,
            "embedding_model": c.embedding_model,
            "chunk_size": c.chunk_size,
            "chunk_overlap": c.chunk_overlap,
            "top_k": c.top_k,
            "similarity_threshold": c.similarity_threshold,
            "temperature": c.temperature,
            "num_ctx": c.num_ctx,
            "max_tokens": c.max_tokens,
            "response_language": c.response_language,
        }

    def _kv(self, key: str) -> dict:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else {}

    def get_settings(self, org_id: str | None = None) -> RuntimeSettings:
        """Env defaults < platform settings < the organisation's own overrides (never its Ollama URL)."""
        data = self.defaults()
        data.update(self._kv("global_settings") or self._kv("settings"))  # "settings" = pre-tenancy key
        if org_id:
            data.update({k: v for k, v in self._kv(f"settings:{org_id}").items() if k not in PLATFORM_ONLY})
        return RuntimeSettings(**data)

    def save_settings(self, s: RuntimeSettings, org_id: str | None = None) -> None:
        if org_id is None:
            key, value = "global_settings", s.model_dump_json()
        else:
            key = f"settings:{org_id}"
            value = json.dumps({k: v for k, v in s.model_dump().items() if k not in PLATFORM_ONLY})
        self._run(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- documents ---------------------------------------------------------
    def add_document(self, **f) -> dict:
        doc = {
            "id": new_id(), "status": "processing", "error": None,
            "page_count": None, "chunk_count": 0, "embedding_model": None,
            "created_at": now(), **f,
        }
        cols = ", ".join(doc)
        marks = ", ".join("?" for _ in doc)
        self._run(f"INSERT INTO documents({cols}) VALUES({marks})", tuple(doc.values()))
        return doc

    def update_document(self, doc_id: str, **f) -> None:
        sets = ", ".join(f"{k}=?" for k in f)
        self._run(f"UPDATE documents SET {sets} WHERE id=?", (*f.values(), doc_id))

    def get_document(self, doc_id: str) -> dict | None:
        return self._one("SELECT * FROM documents WHERE id=?", (doc_id,))

    def find_by_hash(self, sha256: str, org_id: str) -> dict | None:
        """Duplicate detection is per organisation: two tenants may upload the same file."""
        return self._one(
            "SELECT * FROM documents WHERE sha256=? AND org_id=? AND status IN ('processing','ready')",
            (sha256, org_id),
        )

    def list_documents(self, limit: int, offset: int, org_id: str) -> tuple[list[dict], int]:
        total = self._one("SELECT COUNT(*) AS c FROM documents WHERE org_id=?", (org_id,))["c"]
        rows = self._all(
            "SELECT * FROM documents WHERE org_id=? ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (org_id, limit, offset),
        )
        return rows, total

    def ready_documents(self, ids: list[str] | None = None, org_id: str | None = None) -> list[dict]:
        rows = (self._all("SELECT * FROM documents WHERE status='ready' AND org_id=?", (org_id,))
                if org_id else self._all("SELECT * FROM documents WHERE status='ready'"))
        if ids is not None:
            wanted = set(ids)
            rows = [r for r in rows if r["id"] in wanted]
        return rows

    def delete_document(self, doc_id: str) -> None:
        self._run("DELETE FROM documents WHERE id=?", (doc_id,))

    def fail_interrupted(self) -> int:
        cur = self._run(
            "UPDATE documents SET status='failed', error='Processing was interrupted by a restart. Delete and upload again.' "
            "WHERE status='processing'"
        )
        return cur.rowcount

    # -- sessions / messages ----------------------------------------------
    def create_session(self, title: str = "New chat", agent_id: str | None = None,
                       owner: str = "local", org_id: str = "default") -> dict:
        t = now()
        s = {"id": new_id(), "title": title, "created_at": t, "updated_at": t,
             "agent_id": agent_id, "owner": owner, "org_id": org_id}
        self._run(
            "INSERT INTO sessions(id, title, created_at, updated_at, agent_id, owner, org_id) "
            "VALUES(?,?,?,?,?,?,?)",
            tuple(s.values()),
        )
        return s

    def get_session(self, sid: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE id=?", (sid,))

    def list_sessions(self, limit: int, offset: int, owner: str | None = None,
                      org_id: str | None = None) -> tuple[list[dict], int]:
        clauses, args = [], ()
        if org_id:
            clauses.append("org_id=?")
            args += (org_id,)
        if owner:
            clauses.append("owner=?")
            args += (owner,)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self._one(f"SELECT COUNT(*) AS c FROM sessions {where}", args)["c"]
        rows = self._all(
            f"SELECT * FROM sessions {where} ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return rows, total

    def rename_session(self, sid: str, title: str) -> None:
        self._run("UPDATE sessions SET title=? WHERE id=?", (title, sid))

    def delete_session(self, sid: str) -> bool:
        return self._run("DELETE FROM sessions WHERE id=?", (sid,)).rowcount > 0

    def add_message(self, session_id: str, role: str, content: str,
                    citations: list[dict] | None = None, meta: dict | None = None) -> dict:
        m = {
            "id": new_id(), "session_id": session_id, "role": role,
            "content": content, "citations": json.dumps(citations or []),
            "created_at": now(), "meta": json.dumps(meta or {}),
        }
        self._run(
            "INSERT INTO messages(id, session_id, role, content, citations, created_at, meta) "
            "VALUES(?,?,?,?,?,?,?)",
            tuple(m.values()),
        )
        self._run("UPDATE sessions SET updated_at=? WHERE id=?", (m["created_at"], session_id))
        return self._decode(m)

    def list_messages(self, session_id: str, limit: int | None = None) -> list[dict]:
        rows = self._all(
            "SELECT * FROM messages WHERE session_id=? ORDER BY rowid ASC", (session_id,)
        )
        if limit is not None:
            rows = rows[-limit:]
        return [self._decode(r) for r in rows]

    def clear_messages(self, session_id: str) -> None:
        self._run("DELETE FROM messages WHERE session_id=?", (session_id,))

    @staticmethod
    def _decode(m: dict) -> dict:
        m = dict(m)
        if isinstance(m["citations"], str):
            m["citations"] = json.loads(m["citations"])
        meta = m.pop("meta", None)
        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
        m["tools"] = meta.get("tools", [])
        m["handoff"] = bool(meta.get("handoff"))
        return m
