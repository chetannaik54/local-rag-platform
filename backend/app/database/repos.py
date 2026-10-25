"""Tenant-aware tables: organisations, roles, users, login sessions, embed keys, assistants, handoffs,
connectors, analytics and the records agent tools create.

Every row that belongs to a customer carries an ``org_id``; every query here filters on it.
"""
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from app.core import rbac
from app.core.errors import AppError
from app.core.security import new_token, token_hash
from app.database.db import Database, new_id, now

MAX_OPEN_HANDOFFS_PER_USER = 5
ACTION_TTL_MINUTES = 30
DEFAULT_ORG = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
  id TEXT PRIMARY KEY, org_id TEXT, name TEXT NOT NULL, permissions TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT, password_hash TEXT,
  role_id TEXT NOT NULL, token_hash TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1,
  is_super INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE(org_id, name)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embed_keys (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  token TEXT NOT NULL, token_prefix TEXT NOT NULL, allowed_origins TEXT NOT NULL DEFAULT '[]', agent_id TEXT,
  title TEXT NOT NULL DEFAULT 'Ask our assistant', welcome TEXT NOT NULL DEFAULT 'Hi! How can I help?',
  color TEXT NOT NULL DEFAULT '#4f46e5', active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, last_used TEXT
);
CREATE TABLE IF NOT EXISTS agents (
  org_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]',
  document_ids TEXT NOT NULL DEFAULT '[]', builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, required_permission TEXT NOT NULL DEFAULT 'chat.use',
  PRIMARY KEY (org_id, id)
);
CREATE TABLE IF NOT EXISTS handoffs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  assignee TEXT, created_at TEXT NOT NULL, claimed_at TEXT, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_handoffs_session ON handoffs(session_id);
CREATE TABLE IF NOT EXISTS canned (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, ref TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, last_status TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT, agent_id TEXT, question TEXT,
  answered INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0, top_score REAL,
  latency_ms INTEGER, tools TEXT NOT NULL DEFAULT '[]', handoff INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
  message_id TEXT PRIMARY KEY, rating INTEGER NOT NULL, comment TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
  status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS leave_requests (
  id TEXT PRIMARY KEY, employee TEXT NOT NULL, leave_type TEXT NOT NULL, start_date TEXT NOT NULL,
  end_date TEXT NOT NULL, days INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, employee TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followups (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, contact TEXT NOT NULL, note TEXT NOT NULL,
  due_date TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

# columns added to tables that predate multi-tenancy; every legacy row belongs to the default organisation
ORG_COLUMNS = ("documents", "sessions", "handoffs", "canned", "connectors", "events",
               "pending_actions", "leave_requests", "tickets", "followups")

BUILTIN_AGENTS = [
    {
        "id": "general", "name": "General assistant",
        "description": "Answers questions from all uploaded documents.",
        "system_prompt": "You are a general document assistant.", "tools": [],
    },
    {
        "id": "hr", "name": "HR assistant",
        "description": "Policy questions, leave balance and leave requests.",
        "system_prompt": (
            "You are an HR assistant for employees. Answer policy and process questions from the "
            "documents. For the user's own leave, use the leave tools instead of guessing."
        ),
        "tools": ["get_leave_balance", "request_leave", "list_my_leave_requests"],
    },
    {
        "id": "it", "name": "IT helpdesk",
        "description": "Troubleshooting from the knowledge base, tickets and password-reset requests.",
        "system_prompt": (
            "You are an IT helpdesk assistant. Troubleshoot using the documents first. If the problem "
            "is not solved, offer to create a ticket and use the ticket tools when the user agrees."
        ),
        "tools": ["create_ticket", "get_ticket_status", "list_my_tickets", "request_password_reset"],
    },
    {
        "id": "sales", "name": "Sales assistant",
        "description": "Product and pricing questions; schedules customer follow-ups.",
        "system_prompt": (
            "You are a sales assistant. Answer product questions from the documents and never "
            "invent prices or commitments. Use the follow-up tools to schedule customer follow-ups."
        ),
        "tools": ["schedule_followup", "list_followups"],
    },
    {
        "id": "management", "name": "Management insights",
        "description": "Live operational summary across tickets, leave, documents and chats.",
        "system_prompt": (
            "You are a management assistant. Use the business_summary tool for current numbers and "
            "the documents for policies. Be concise and quantitative."
        ),
        "tools": ["business_summary"],
        "required_permission": "analytics.view",
    },
]

BUILTIN_CANNED = [
    ("Greeting", "Hi, this is a member of our team. I've read your conversation and I'm happy to help."),
    ("Need more info", "Could you share a few more details so I can look into this properly?"),
    ("Resolved", "I've taken care of this. Is there anything else I can help you with?"),
]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_LEGACY_ROLE_IDS = {"admin": "admin", "agent": "agent"}


def _agent(row: dict) -> dict:
    return {**row, "tools": json.loads(row["tools"]), "document_ids": json.loads(row["document_ids"]),
            "builtin": bool(row["builtin"])}


class Store:
    def __init__(self, db: Database):
        self.db = db
        with db._lock:
            db._conn.executescript(SCHEMA)
            db._conn.commit()
        self._seed_roles_and_default_org()
        self._rebuild_users_if_legacy()
        self._rebuild_agents_if_legacy()
        db.ensure_columns("sessions", {"agent_id": "TEXT", "owner": "TEXT NOT NULL DEFAULT 'local'"})
        db.ensure_columns("messages", {"meta": "TEXT NOT NULL DEFAULT '{}'"})
        db.ensure_columns("documents", {
            "source_type": "TEXT NOT NULL DEFAULT 'upload'", "source_ref": "TEXT", "connector_id": "TEXT",
        })
        for table in ORG_COLUMNS:
            db.ensure_columns(table, {"org_id": f"TEXT NOT NULL DEFAULT '{DEFAULT_ORG}'"})
        db.run("CREATE INDEX IF NOT EXISTS idx_documents_org ON documents(org_id)")
        db.run("CREATE INDEX IF NOT EXISTS idx_sessions_org ON sessions(org_id, owner)")
        db.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(lower(email)) WHERE email IS NOT NULL")
        db.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_name ON roles(COALESCE(org_id,''), lower(name))")
        self._remap_legacy_owners()
        for org in self.list_orgs():
            self._seed_org(org["id"])

    # ------------------------------------------------------------------ migration
    def _seed_roles_and_default_org(self) -> None:
        for rid, spec in rbac.BUILTIN_ROLES.items():
            perms = json.dumps(sorted(spec["permissions"]))
            if self.db.one("SELECT 1 AS x FROM roles WHERE id=?", (rid,)):
                self.db.run("UPDATE roles SET permissions=?, name=? WHERE id=?", (perms, rid, rid))  # keep in sync
            else:
                self.db.run("INSERT INTO roles(id,org_id,name,permissions,builtin,created_at) VALUES(?,NULL,?,?,1,?)",
                            (rid, rid, perms, now()))
        if not self.db.one("SELECT 1 AS x FROM orgs WHERE id=?", (DEFAULT_ORG,)):
            self.db.run("INSERT INTO orgs(id,name,slug,active,created_at) VALUES(?,?,?,1,?)",
                        (DEFAULT_ORG, "Default", "default", now()))

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.db.all(f"PRAGMA table_info({table})")}

    def _rebuild_users_if_legacy(self) -> None:
        """Pre-tenancy users had a globally unique name and one API token. Rebuild into the new shape."""
        if "org_id" in self._columns("users"):
            return
        with self.db.transaction() as c:
            old = [dict(r) for r in c.execute("SELECT * FROM users").fetchall()]
            c.execute("ALTER TABLE users RENAME TO users_legacy")
            c.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL, email TEXT, "
                "password_hash TEXT, role_id TEXT NOT NULL, token_hash TEXT UNIQUE, active INTEGER NOT NULL DEFAULT 1, "
                "is_super INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE(org_id, name))")
            for r in old:
                is_super = r["name"] == "admin" and r["role"] == "admin"  # the old bootstrap admin runs the platform
                role_id = "super_admin" if is_super else _LEGACY_ROLE_IDS.get(r["role"], "member")
                c.execute("INSERT INTO users(id,org_id,name,email,password_hash,role_id,token_hash,active,is_super,created_at) "
                          "VALUES(?,?,?,?,?,?,?,1,?,?)",
                          (r["id"], DEFAULT_ORG, r["name"], None, None, role_id, r["token_hash"], int(is_super), r["created_at"]))
            c.execute("DROP TABLE users_legacy")

    def _rebuild_agents_if_legacy(self) -> None:
        cols = self._columns("agents")
        if "org_id" in cols:
            return
        perm_for = {"admin": "analytics.view", "agent": "handoffs.handle"}
        with self.db.transaction() as c:
            old = [dict(r) for r in c.execute("SELECT * FROM agents").fetchall()]
            c.execute("ALTER TABLE agents RENAME TO agents_legacy")
            c.execute(
                "CREATE TABLE agents (org_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, "
                "description TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]', "
                "document_ids TEXT NOT NULL DEFAULT '[]', builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                "required_permission TEXT NOT NULL DEFAULT 'chat.use', PRIMARY KEY (org_id, id))")
            for r in old:
                c.execute("INSERT INTO agents(org_id,id,name,description,system_prompt,tools,document_ids,builtin,"
                          "created_at,required_permission) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (DEFAULT_ORG, r["id"], r["name"], r["description"], r["system_prompt"], r["tools"],
                           r["document_ids"], r["builtin"], r["created_at"],
                           perm_for.get(r.get("min_role", "user"), "chat.use")))
            c.execute("DROP TABLE agents_legacy")

    def _remap_legacy_owners(self) -> None:
        """Sessions/records used to be owned by a user *name*; ownership is now by user id."""
        pairs = [("sessions", "owner"), ("pending_actions", "owner"), ("tickets", "employee"),
                 ("leave_requests", "employee"), ("followups", "owner")]
        for table, col in pairs:
            rows = self.db.all(
                f"SELECT DISTINCT {col} AS v FROM {table} WHERE org_id=? AND {col} != 'local' "
                "AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id=" + f"{table}.{col})", (DEFAULT_ORG,))
            for r in rows:
                u = self.db.one("SELECT id FROM users WHERE org_id=? AND name=?", (DEFAULT_ORG, r["v"]))
                if u:
                    self.db.run(f"UPDATE {table} SET {col}=? WHERE org_id=? AND {col}=?", (u["id"], DEFAULT_ORG, r["v"]))

    def _seed_org(self, org_id: str) -> None:
        for a in BUILTIN_AGENTS:
            if not self.db.one("SELECT 1 AS x FROM agents WHERE org_id=? AND id=?", (org_id, a["id"])):
                self.db.run(
                    "INSERT INTO agents(org_id,id,name,description,system_prompt,tools,document_ids,builtin,"
                    "created_at,required_permission) VALUES(?,?,?,?,?,?,'[]',1,?,?)",
                    (org_id, a["id"], a["name"], a["description"], a["system_prompt"], json.dumps(a["tools"]), now(),
                     a.get("required_permission", "chat.use")))
        if not self.db.one("SELECT 1 AS x FROM canned WHERE org_id=? LIMIT 1", (org_id,)):
            for title, text in BUILTIN_CANNED:
                self.db.run("INSERT INTO canned(id,title,text,org_id) VALUES(?,?,?,?)", (new_id(), title, text, org_id))

    # ------------------------------------------------------------------ organisations
    def create_org(self, name: str, slug: str) -> dict:
        slug = slug.strip().lower()
        if not _SLUG.match(slug):
            raise AppError("validation_error", "Slug must be 3-40 characters: lowercase letters, digits and dashes.", 422)
        if self.db.one("SELECT 1 AS x FROM orgs WHERE slug=?", (slug,)):
            raise AppError("conflict", "An organization with that slug already exists.", 409)
        org = {"id": new_id(), "name": name.strip(), "slug": slug, "active": 1, "created_at": now()}
        self.db.run("INSERT INTO orgs VALUES(?,?,?,?,?)", tuple(org.values()))
        self._seed_org(org["id"])
        return self._org(org)

    @staticmethod
    def _org(r: dict) -> dict:
        return {**r, "active": bool(r["active"])}

    def get_org(self, org_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM orgs WHERE id=?", (org_id,))
        return self._org(r) if r else None

    def list_orgs(self) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM orgs ORDER BY created_at, id"):
            o = self._org(r)
            o["user_count"] = self.db.one("SELECT COUNT(*) AS c FROM users WHERE org_id=?", (r["id"],))["c"]
            o["document_count"] = self.db.one("SELECT COUNT(*) AS c FROM documents WHERE org_id=?", (r["id"],))["c"]
            out.append(o)
        return out

    def update_org(self, org_id: str, **f) -> None:
        if "active" in f:
            f["active"] = int(f["active"])
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.run(f"UPDATE orgs SET {sets} WHERE id=?", (*f.values(), org_id))

    # ------------------------------------------------------------------ roles
    @staticmethod
    def _role(r: dict) -> dict:
        return {**r, "permissions": json.loads(r["permissions"]), "builtin": bool(r["builtin"])}

    def get_role(self, role_id: str, org_id: str | None = None) -> dict | None:
        """A built-in role, or a custom role of ``org_id``."""
        r = self.db.one("SELECT * FROM roles WHERE id=? AND (org_id IS NULL OR org_id=?)", (role_id, org_id))
        return self._role(r) if r else None

    def role_by_name(self, name: str, org_id: str) -> dict | None:
        name = rbac.LEGACY_ROLE_ALIASES.get(name.lower(), name)
        r = self.db.one("SELECT * FROM roles WHERE lower(name)=lower(?) AND (org_id IS NULL OR org_id=?) "
                        "ORDER BY org_id IS NULL", (name, org_id))
        return self._role(r) if r else None

    def list_roles(self, org_id: str, include_super: bool = False) -> list[dict]:
        rows = self.db.all("SELECT * FROM roles WHERE org_id IS NULL OR org_id=? ORDER BY builtin DESC, name", (org_id,))
        roles = [self._role(r) for r in rows if include_super or r["id"] != "super_admin"]
        for r in roles:
            r["user_count"] = self.db.one("SELECT COUNT(*) AS c FROM users WHERE role_id=? AND org_id=?", (r["id"], org_id))["c"]
        return roles

    def create_role(self, org_id: str, name: str, permissions: list[str]) -> dict:
        if self.role_by_name(name, org_id):
            raise AppError("conflict", "A role with that name already exists.", 409)
        rid = new_id()
        self.db.run("INSERT INTO roles(id,org_id,name,permissions,builtin,created_at) VALUES(?,?,?,?,0,?)",
                    (rid, org_id, name.strip(), json.dumps(sorted(set(permissions))), now()))
        return self.get_role(rid, org_id)

    def update_role(self, role_id: str, org_id: str, name: str, permissions: list[str]) -> dict:
        clash = self.role_by_name(name, org_id)
        if clash and clash["id"] != role_id:
            raise AppError("conflict", "A role with that name already exists.", 409)
        self.db.run("UPDATE roles SET name=?, permissions=? WHERE id=? AND org_id=? AND builtin=0",
                    (name.strip(), json.dumps(sorted(set(permissions))), role_id, org_id))
        return self.get_role(role_id, org_id)

    def delete_role(self, role_id: str, org_id: str) -> bool:
        return self.db.run("DELETE FROM roles WHERE id=? AND org_id=? AND builtin=0", (role_id, org_id)).rowcount > 0

    # ------------------------------------------------------------------ users
    _USER_SQL = ("SELECT u.id, u.org_id, u.name, u.email, u.role_id, u.active, u.is_super, u.created_at, "
                 "u.password_hash IS NOT NULL AS has_password, u.token_hash IS NOT NULL AS has_api_key, "
                 "r.name AS role_name, r.permissions AS permissions, o.name AS org_name, o.active AS org_active "
                 "FROM users u JOIN roles r ON r.id=u.role_id JOIN orgs o ON o.id=u.org_id ")

    @staticmethod
    def _user(r: dict) -> dict:
        return {**r, "permissions": json.loads(r["permissions"]), "active": bool(r["active"]),
                "org_active": bool(r["org_active"]), "has_password": bool(r["has_password"]),
                "has_api_key": bool(r["has_api_key"]), "is_super": bool(r["is_super"])}

    def create_user(self, org_id: str, name: str, role_id: str, *, email: str | None = None,
                    password_hash: str | None = None, issue_token: bool = True,
                    token: str | None = None) -> tuple[dict, str | None]:
        if self.db.one("SELECT 1 AS x FROM users WHERE org_id=? AND name=?", (org_id, name)):
            raise AppError("conflict", "A user with that name already exists in this organization.", 409)
        if email and self.db.one("SELECT 1 AS x FROM users WHERE lower(email)=lower(?)", (email,)):
            raise AppError("conflict", "That email address is already registered.", 409)
        plain = (token or new_token("rag_")) if issue_token else None
        uid = new_id()
        self.db.run("INSERT INTO users(id,org_id,name,email,password_hash,role_id,token_hash,active,is_super,created_at) "
                    "VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (uid, org_id, name, email, password_hash, role_id, token_hash(plain) if plain else None,
                     int(role_id == "super_admin"), now()))
        return self.get_user(uid), plain

    def get_user(self, uid: str) -> dict | None:
        r = self.db.one(self._USER_SQL + "WHERE u.id=?", (uid,))
        return self._user(r) if r else None

    def user_by_token(self, token: str) -> dict | None:
        r = self.db.one(self._USER_SQL + "WHERE u.token_hash=?", (token_hash(token),))
        return self._user(r) if r else None

    def user_by_email(self, email: str) -> dict | None:
        r = self.db.one(self._USER_SQL + "WHERE lower(u.email)=lower(?)", (email,))
        return self._user(r) if r else None

    def password_hash_of(self, uid: str) -> str | None:
        r = self.db.one("SELECT password_hash FROM users WHERE id=?", (uid,))
        return r["password_hash"] if r else None

    def list_users(self, org_id: str) -> list[dict]:
        return [self._user(r) for r in self.db.all(self._USER_SQL + "WHERE u.org_id=? ORDER BY u.created_at, u.name", (org_id,))]

    def update_user(self, uid: str, **f) -> None:
        if not f:
            return
        if "active" in f:
            f["active"] = int(f["active"])
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.run(f"UPDATE users SET {sets} WHERE id=?", (*f.values(), uid))

    def reset_api_key(self, uid: str) -> str:
        token = new_token("rag_")
        self.db.run("UPDATE users SET token_hash=? WHERE id=?", (token_hash(token), uid))
        return token

    def delete_user(self, uid: str) -> bool:
        self.db.run("DELETE FROM auth_sessions WHERE user_id=?", (uid,))
        return self.db.run("DELETE FROM users WHERE id=?", (uid,)).rowcount > 0

    def count_users(self) -> int:
        return self.db.one("SELECT COUNT(*) AS c FROM users")["c"]

    def super_admins(self) -> list[dict]:
        return [self._user(r) for r in self.db.all(self._USER_SQL + "WHERE u.role_id='super_admin' ORDER BY u.created_at")]

    def user_managers(self, org_id: str) -> list[dict]:
        """Active users of an organisation who can manage users (used to stop the last one being removed)."""
        return [u for u in self.list_users(org_id) if u["active"] and "users.manage" in u["permissions"]]

    # ------------------------------------------------------------------ login sessions
    def create_auth_session(self, user_id: str, hours: int) -> tuple[str, str]:
        token = new_token("ses_")
        expires = (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.db.run("INSERT INTO auth_sessions(id,user_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
                    (new_id(), user_id, token_hash(token), now(), expires))
        self.db.run("DELETE FROM auth_sessions WHERE expires_at < ?", (now(),))  # housekeeping
        return token, expires

    def user_by_session(self, token: str) -> dict | None:
        s = self.db.one("SELECT user_id, expires_at FROM auth_sessions WHERE token_hash=?", (token_hash(token),))
        if not s or s["expires_at"] < now():
            return None
        return self.get_user(s["user_id"])

    def delete_auth_session(self, token: str) -> None:
        self.db.run("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token),))

    def delete_user_sessions(self, user_id: str, except_token: str | None = None) -> None:
        if except_token:
            self.db.run("DELETE FROM auth_sessions WHERE user_id=? AND token_hash!=?", (user_id, token_hash(except_token)))
        else:
            self.db.run("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------ embed keys
    @staticmethod
    def _embed(r: dict) -> dict:
        return {**r, "allowed_origins": json.loads(r["allowed_origins"]), "active": bool(r["active"])}

    def create_embed_key(self, org_id: str, name: str, origins: list[str], agent_id: str | None,
                         title: str, welcome: str, color: str) -> tuple[dict, str]:
        token = new_token("pk_")
        kid = new_id()
        self.db.run("INSERT INTO embed_keys(id,org_id,name,token_hash,token,token_prefix,allowed_origins,agent_id,title,"
                    "welcome,color,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)",
                    (kid, org_id, name, token_hash(token), token, token[:9], json.dumps(origins), agent_id, title,
                     welcome, color, now()))
        return self.get_embed_key(kid, org_id), token

    def get_embed_key(self, kid: str, org_id: str | None = None) -> dict | None:
        r = (self.db.one("SELECT * FROM embed_keys WHERE id=? AND org_id=?", (kid, org_id)) if org_id
             else self.db.one("SELECT * FROM embed_keys WHERE id=?", (kid,)))
        return self._embed(r) if r else None

    def embed_key_by_token(self, token: str) -> dict | None:
        r = self.db.one("SELECT * FROM embed_keys WHERE token_hash=?", (token_hash(token),))
        return self._embed(r) if r else None

    def list_embed_keys(self, org_id: str) -> list[dict]:
        return [self._embed(r) for r in self.db.all("SELECT * FROM embed_keys WHERE org_id=? ORDER BY created_at", (org_id,))]

    def update_embed_key(self, kid: str, org_id: str, **f) -> None:
        if "allowed_origins" in f:
            f["allowed_origins"] = json.dumps(f["allowed_origins"])
        if "active" in f:
            f["active"] = int(f["active"])
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.run(f"UPDATE embed_keys SET {sets} WHERE id=? AND org_id=?", (*f.values(), kid, org_id))

    def delete_embed_key(self, kid: str, org_id: str) -> bool:
        return self.db.run("DELETE FROM embed_keys WHERE id=? AND org_id=?", (kid, org_id)).rowcount > 0

    def touch_embed_key(self, kid: str) -> None:
        self.db.run("UPDATE embed_keys SET last_used=? WHERE id=?", (now(), kid))

    def all_embed_origins(self) -> set[str]:
        out: set[str] = set()
        for r in self.db.all("SELECT allowed_origins FROM embed_keys WHERE active=1"):
            out.update(json.loads(r["allowed_origins"]))
        return out

    # ------------------------------------------------------------------ agents
    def list_agents(self, org_id: str) -> list[dict]:
        return [_agent(r) for r in self.db.all(
            "SELECT * FROM agents WHERE org_id=? ORDER BY builtin DESC, created_at", (org_id,))]

    def get_agent(self, org_id: str, agent_id: str | None) -> dict | None:
        if not agent_id:
            return None
        r = self.db.one("SELECT * FROM agents WHERE org_id=? AND id=?", (org_id, agent_id))
        return _agent(r) if r else None

    def save_agent(self, org_id: str, agent_id: str | None, name: str, description: str, system_prompt: str,
                   tools: list[str], document_ids: list[str], required_permission: str = "chat.use") -> dict:
        if agent_id and self.get_agent(org_id, agent_id):
            self.db.run(
                "UPDATE agents SET name=?, description=?, system_prompt=?, tools=?, document_ids=?, "
                "required_permission=? WHERE org_id=? AND id=?",
                (name, description, system_prompt, json.dumps(tools), json.dumps(document_ids),
                 required_permission, org_id, agent_id))
            return self.get_agent(org_id, agent_id)
        agent_id = new_id()
        self.db.run(
            "INSERT INTO agents(org_id,id,name,description,system_prompt,tools,document_ids,builtin,created_at,"
            "required_permission) VALUES(?,?,?,?,?,?,?,0,?,?)",
            (org_id, agent_id, name, description, system_prompt, json.dumps(tools), json.dumps(document_ids),
             now(), required_permission))
        return self.get_agent(org_id, agent_id)

    def delete_agent(self, org_id: str, agent_id: str) -> bool:
        a = self.get_agent(org_id, agent_id)
        if not a or a["builtin"]:
            return False
        self.db.run("DELETE FROM agents WHERE org_id=? AND id=?", (org_id, agent_id))
        return True

    # ------------------------------------------------------------------ handoffs
    def open_handoff(self, session_id: str) -> dict | None:
        return self.db.one(
            "SELECT * FROM handoffs WHERE session_id=? AND status IN ('waiting','active') "
            "ORDER BY created_at DESC LIMIT 1", (session_id,))

    def create_handoff(self, session_id: str, reason: str) -> dict:
        existing = self.open_handoff(session_id)
        if existing:
            return existing
        session = self.db.get_session(session_id) or {}
        owner = session.get("owner")
        open_now = self.db.one(
            "SELECT COUNT(*) AS c FROM handoffs h JOIN sessions s ON s.id=h.session_id "
            "WHERE s.owner=? AND h.status IN ('waiting','active')", (owner,))["c"]
        if open_now >= MAX_OPEN_HANDOFFS_PER_USER:
            raise AppError("too_many_handoffs",
                           f"You already have {open_now} open requests for a human. "
                           "Wait for one to be answered or cancel one first.", 429)
        h = {"id": new_id(), "session_id": session_id, "status": "waiting", "reason": reason[:500],
             "assignee": None, "created_at": now(), "claimed_at": None, "resolved_at": None,
             "org_id": session.get("org_id", DEFAULT_ORG)}
        self.db.run("INSERT INTO handoffs(id,session_id,status,reason,assignee,created_at,claimed_at,resolved_at,org_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", tuple(h.values()))
        return h

    def get_handoff(self, hid: str, org_id: str | None = None) -> dict | None:
        if org_id:
            return self.db.one("SELECT * FROM handoffs WHERE id=? AND org_id=?", (hid, org_id))
        return self.db.one("SELECT * FROM handoffs WHERE id=?", (hid,))

    def list_handoffs(self, org_id: str, only_open: bool = True) -> list[dict]:
        where = "AND h.status IN ('waiting','active')" if only_open else ""
        return self.db.all(
            "SELECT h.*, s.title AS session_title, (SELECT name FROM users WHERE id=h.assignee) AS assignee_name, "
            "(SELECT content FROM messages m WHERE m.session_id=h.session_id AND m.role='user' "
            " ORDER BY m.rowid DESC LIMIT 1) AS last_user_message "
            f"FROM handoffs h JOIN sessions s ON s.id=h.session_id WHERE h.org_id=? {where} "
            "ORDER BY h.created_at ASC LIMIT 200", (org_id,))

    def claim_handoff(self, hid: str, assignee: str) -> bool:
        return self.db.run(
            "UPDATE handoffs SET status='active', assignee=?, claimed_at=? WHERE id=? AND status='waiting'",
            (assignee, now(), hid)).rowcount > 0

    def close_session_records(self, session_id: str) -> None:
        """Called when a session is deleted: nothing may keep pointing at it."""
        self.db.run("UPDATE handoffs SET status='resolved', resolved_at=? WHERE session_id=? "
                    "AND status IN ('waiting','active')", (now(), session_id))
        self.db.run("UPDATE pending_actions SET status='cancelled', resolved_at=? WHERE session_id=? "
                    "AND status='pending'", (now(), session_id))

    def resolve_handoff(self, hid: str) -> bool:
        return self.db.run(
            "UPDATE handoffs SET status='resolved', resolved_at=? WHERE id=? AND status IN ('waiting','active')",
            (now(), hid)).rowcount > 0

    # ------------------------------------------------------------------ pending (user-confirmed) actions
    def create_action(self, session_id: str, owner: str, tool: str, args: dict) -> str:
        aid = new_id()
        org = (self.db.get_session(session_id) or {}).get("org_id", DEFAULT_ORG)
        self.db.run("INSERT INTO pending_actions(id,session_id,owner,tool,args,status,created_at,org_id) "
                    "VALUES(?,?,?,?,?,'pending',?,?)", (aid, session_id, owner, tool, json.dumps(args), now(), org))
        return aid

    def get_action(self, aid: str) -> dict | None:
        r = self.db.one("SELECT * FROM pending_actions WHERE id=?", (aid,))
        if not r:
            return None
        r["args"] = json.loads(r["args"])
        r["result"] = json.loads(r["result"]) if r["result"] else None
        age = datetime.now(UTC) - datetime.fromisoformat(r["created_at"])
        if r["status"] == "pending" and age > timedelta(minutes=ACTION_TTL_MINUTES):
            r["status"] = "expired"
        return r

    def finish_action(self, aid: str, status: str, result: dict | None) -> bool:
        """Atomically move a pending action to its final state; False if it was already handled."""
        return self.db.run(
            "UPDATE pending_actions SET status=?, result=?, resolved_at=? WHERE id=? AND status='pending'",
            (status, json.dumps(result) if result is not None else None, now(), aid)).rowcount > 0

    # ------------------------------------------------------------------ canned responses
    def list_canned(self, org_id: str) -> list[dict]:
        return self.db.all("SELECT id, title, text FROM canned WHERE org_id=? ORDER BY title", (org_id,))

    def add_canned(self, org_id: str, title: str, text: str) -> dict:
        c = {"id": new_id(), "title": title, "text": text}
        self.db.run("INSERT INTO canned(id,title,text,org_id) VALUES(?,?,?,?)", (*c.values(), org_id))
        return c

    def delete_canned(self, org_id: str, cid: str) -> bool:
        return self.db.run("DELETE FROM canned WHERE id=? AND org_id=?", (cid, org_id)).rowcount > 0

    # ------------------------------------------------------------------ connectors
    def list_connectors(self, org_id: str | None = None) -> list[dict]:
        rows = (self.db.all("SELECT * FROM connectors WHERE org_id=? ORDER BY created_at", (org_id,)) if org_id
                else self.db.all("SELECT * FROM connectors ORDER BY created_at"))
        for r in rows:
            r["enabled"] = bool(r["enabled"])
            r["document_count"] = self.db.one(
                "SELECT COUNT(*) AS c FROM documents WHERE connector_id=?", (r["id"],))["c"]
        return rows

    def get_connector(self, cid: str, org_id: str | None = None) -> dict | None:
        if org_id:
            return self.db.one("SELECT * FROM connectors WHERE id=? AND org_id=?", (cid, org_id))
        return self.db.one("SELECT * FROM connectors WHERE id=?", (cid,))

    def add_connector(self, org_id: str, ctype: str, ref: str, interval_minutes: int) -> dict:
        c = {"id": new_id(), "type": ctype, "ref": ref, "interval_minutes": interval_minutes,
             "enabled": 1, "last_sync": None, "last_status": None, "created_at": now(), "org_id": org_id}
        self.db.run("INSERT INTO connectors(id,type,ref,interval_minutes,enabled,last_sync,last_status,created_at,org_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", tuple(c.values()))
        return c

    def update_connector(self, cid: str, **f) -> None:
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.run(f"UPDATE connectors SET {sets} WHERE id=?", (*f.values(), cid))

    def delete_connector(self, cid: str) -> bool:
        return self.db.run("DELETE FROM connectors WHERE id=?", (cid,)).rowcount > 0

    def connector_documents(self, cid: str) -> list[dict]:
        return self.db.all("SELECT * FROM documents WHERE connector_id=?", (cid,))

    # ------------------------------------------------------------------ analytics
    def log_event(self, **e) -> None:
        row = {"id": new_id(), "ts": now(), "session_id": None, "agent_id": None, "question": "",
               "answered": 0, "hit_count": 0, "top_score": None, "latency_ms": None, "tools": [],
               "handoff": 0, "org_id": DEFAULT_ORG, **e}
        row["question"] = (row["question"] or "")[:300]
        row["tools"] = json.dumps(row["tools"])
        row["answered"] = int(row["answered"])
        row["handoff"] = int(row["handoff"])
        cols = ",".join(row)
        self.db.run(f"INSERT INTO events({cols}) VALUES({','.join('?' * len(row))})", tuple(row.values()))

    def set_feedback(self, message_id: str, rating: int, comment: str | None) -> None:
        self.db.run(
            "INSERT INTO feedback(message_id,rating,comment,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment",
            (message_id, rating, comment, now()))

    def get_feedback(self, message_id: str) -> int | None:
        r = self.db.one("SELECT rating FROM feedback WHERE message_id=?", (message_id,))
        return r["rating"] if r else None

    def analytics(self, org_id: str, days: int = 30) -> dict:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        ev = self.db.all("SELECT * FROM events WHERE org_id=? AND ts>=? ORDER BY ts", (org_id, since))
        total = len(ev)
        answered = sum(e["answered"] for e in ev)
        lat = [e["latency_ms"] for e in ev if e["latency_ms"] is not None]
        per_agent: dict[str, int] = {}
        per_day: dict[str, int] = {}
        tool_use: dict[str, int] = {}
        for e in ev:
            per_agent[e["agent_id"] or "general"] = per_agent.get(e["agent_id"] or "general", 0) + 1
            per_day[e["ts"][:10]] = per_day.get(e["ts"][:10], 0) + 1
            for t in json.loads(e["tools"]):
                tool_use[t] = tool_use.get(t, 0) + 1
        unanswered = [
            {"question": e["question"], "ts": e["ts"], "agent_id": e["agent_id"]}
            for e in reversed(ev) if not e["answered"] and e["question"]
        ][:20]
        fb = self.db.all(
            "SELECT f.rating AS r FROM feedback f JOIN messages m ON m.id=f.message_id "
            "JOIN sessions s ON s.id=m.session_id WHERE s.org_id=? AND f.created_at>=?", (org_id, since))
        cited: dict[str, int] = {}
        for m in self.db.all(
                "SELECT m.citations AS citations FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE m.role='assistant' AND s.org_id=? AND m.created_at>=?", (org_id, since)):
            for c in json.loads(m["citations"]):
                if c.get("cited"):
                    cited[c["filename"]] = cited.get(c["filename"], 0) + 1
        handoffs = self.db.one("SELECT COUNT(*) AS c FROM handoffs WHERE org_id=? AND created_at>=?", (org_id, since))["c"]
        return {
            "days": days, "total_questions": total, "answered": answered,
            "answer_rate": round(answered / total, 3) if total else None,
            "avg_latency_ms": round(sum(lat) / len(lat)) if lat else None,
            "handoffs": handoffs,
            "thumbs_up": sum(1 for f in fb if f["r"] > 0), "thumbs_down": sum(1 for f in fb if f["r"] < 0),
            "per_agent": per_agent, "tool_use": tool_use,
            "per_day": [{"day": d, "count": c} for d, c in sorted(per_day.items())],
            "unanswered": unanswered,
            "top_documents": [{"filename": k, "citations": v}
                              for k, v in sorted(cited.items(), key=lambda kv: -kv[1])[:10]],
        }

    def business_summary(self, org_id: str) -> dict:
        one = self.db.one
        week = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
        return {
            "documents_ready": one("SELECT COUNT(*) AS c FROM documents WHERE status='ready' AND org_id=?", (org_id,))["c"],
            "chat_sessions": one("SELECT COUNT(*) AS c FROM sessions WHERE org_id=?", (org_id,))["c"],
            "open_tickets": one("SELECT COUNT(*) AS c FROM tickets WHERE status='open' AND org_id=?", (org_id,))["c"],
            "pending_leave_requests": one("SELECT COUNT(*) AS c FROM leave_requests WHERE status='pending' AND org_id=?", (org_id,))["c"],
            "open_handoffs": one("SELECT COUNT(*) AS c FROM handoffs WHERE status IN ('waiting','active') AND org_id=?", (org_id,))["c"],
            "scheduled_followups": one("SELECT COUNT(*) AS c FROM followups WHERE status='scheduled' AND org_id=?", (org_id,))["c"],
            "unanswered_questions_7d": one(
                "SELECT COUNT(*) AS c FROM events WHERE answered=0 AND org_id=? AND ts>=?", (org_id, week))["c"],
        }


def random_password(n: int = 16) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
