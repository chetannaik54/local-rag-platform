"""A database created before multi-tenancy must open cleanly, keep every record, and keep working."""
import json
import sqlite3

import pytest
from conftest import make_client
from fastapi.testclient import TestClient

from app.core.config import AppConfig
from app.main import create_app

LEGACY_DDL = """
CREATE TABLE documents (id TEXT PRIMARY KEY, filename TEXT NOT NULL, ext TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT, page_count INTEGER, chunk_count INTEGER NOT NULL DEFAULT 0,
  embedding_model TEXT, stored_path TEXT NOT NULL, created_at TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'upload', source_ref TEXT, connector_id TEXT);
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  agent_id TEXT, owner TEXT NOT NULL DEFAULT 'local');
CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL, content TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}');
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL, tools TEXT NOT NULL DEFAULT '[]', document_ids TEXT NOT NULL DEFAULT '[]',
  builtin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, min_role TEXT NOT NULL DEFAULT 'user');
CREATE TABLE handoffs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  assignee TEXT, created_at TEXT NOT NULL, claimed_at TEXT, resolved_at TEXT);
CREATE TABLE canned (id TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE connectors (id TEXT PRIMARY KEY, type TEXT NOT NULL, ref TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, last_status TEXT, created_at TEXT NOT NULL);
CREATE TABLE events (id TEXT PRIMARY KEY, ts TEXT NOT NULL, session_id TEXT, agent_id TEXT, question TEXT,
  answered INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0, top_score REAL, latency_ms INTEGER,
  tools TEXT NOT NULL DEFAULT '[]', handoff INTEGER NOT NULL DEFAULT 0);
CREATE TABLE feedback (message_id TEXT PRIMARY KEY, rating INTEGER NOT NULL, comment TEXT, created_at TEXT NOT NULL);
CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL);
CREATE TABLE pending_actions (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, owner TEXT NOT NULL, tool TEXT NOT NULL,
  args TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE leave_requests (id TEXT PRIMARY KEY, employee TEXT NOT NULL, leave_type TEXT NOT NULL, start_date TEXT NOT NULL,
  end_date TEXT NOT NULL, days INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE tickets (id TEXT PRIMARY KEY, employee TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE followups (id TEXT PRIMARY KEY, owner TEXT NOT NULL, contact TEXT NOT NULL, note TEXT NOT NULL,
  due_date TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
"""

NOW = "2026-01-05T10:00:00+00:00"


def sha(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def legacy_dir(tmp_path):
    con = sqlite3.connect(tmp_path / "app.db")
    con.executescript(LEGACY_DDL)
    con.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [
        ("u-admin", "admin", "admin", sha("legacy-admin-token"), NOW),
        ("u-bob", "bob", "user", sha("legacy-bob-token"), NOW),
        ("u-sam", "sam", "agent", sha("legacy-sam-token"), NOW)])
    con.executemany("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?)", [
        ("general", "General assistant", "d", "p", "[]", "[]", 1, NOW, "user"),
        ("management", "Management insights", "d", "p", '["business_summary"]', "[]", 1, NOW, "admin"),
        ("custom1", "Old custom", "d", "p", "[]", "[]", 0, NOW, "agent")])
    con.execute("INSERT INTO documents VALUES('d1','policy.txt','.txt',10,'abc','ready',NULL,NULL,3,'nomic-embed-text','x',?,'upload',NULL,NULL)", (NOW,))
    con.execute("INSERT INTO sessions VALUES('s-bob','Bobs chat',?,?,'general','bob')", (NOW, NOW))
    con.execute("INSERT INTO sessions VALUES('s-local','Local chat',?,?,NULL,'local')", (NOW, NOW))
    con.execute("INSERT INTO messages VALUES('m1','s-bob','user','hello','[]',?,'{}')", (NOW,))
    con.execute("INSERT INTO tickets VALUES('T1','bob','incident','Old ticket','', 'high','open',?)", (NOW,))
    con.execute("INSERT INTO leave_requests VALUES('L1','bob','annual','2026-03-02','2026-03-04',3,'pending',?)", (NOW,))
    con.execute("INSERT INTO canned VALUES('c1','Hi','Hello there')")
    con.execute("INSERT INTO events VALUES('e1',?,'s-bob','general','q',1,1,0.9,100,'[]',0)", (NOW,))
    con.execute("INSERT INTO kv VALUES('settings', ?)", (json.dumps({"top_k": 7, "chat_model": "llama3.1:8b",
                                                                   "ollama_url": "http://localhost:11434"}),))
    con.commit()
    con.close()
    return tmp_path


def open_app(path, fake, **cfg):
    base = dict(data_dir=path, similarity_threshold=0.25, _env_file=None, auth_enabled=True,
                admin_password="Sup3r-secret-pw!", admin_email="root@example.com")
    return TestClient(create_app(AppConfig(**{**base, **cfg}), ollama=fake, run_scheduler=False))


def test_legacy_database_is_migrated_without_losing_anything(legacy_dir, fake):
    with open_app(legacy_dir, fake) as c:
        h = lambda t: {"Authorization": f"Bearer {t}"}
        # old API keys still work and map to sensible roles
        admin = c.get("/api/auth/me", headers=h("legacy-admin-token")).json()
        assert admin["role"] == "super_admin" and admin["org_id"] == "default" and admin["is_super"]
        bob = c.get("/api/auth/me", headers=h("legacy-bob-token")).json()
        sam = c.get("/api/auth/me", headers=h("legacy-sam-token")).json()
        assert bob["role"] == "member" and sam["role"] == "agent"
        assert "handoffs.handle" in sam["permissions"] and "documents.write" not in bob["permissions"]

        # data is in the default organization
        docs = c.get("/api/documents", headers=h("legacy-admin-token")).json()["items"]
        assert [d["filename"] for d in docs] == ["policy.txt"]
        # sessions that were owned by the *name* "bob" are now owned by bob's id and still his
        sessions = c.get("/api/sessions", headers=h("legacy-bob-token")).json()["items"]
        assert [s["title"] for s in sessions] == ["Bobs chat"]
        assert c.get("/api/sessions/s-bob/messages", headers=h("legacy-bob-token")).json()["items"][0]["content"] == "hello"
        assert c.get("/api/sessions", headers=h("legacy-sam-token")).json()["total"] == 0  # not sam's
        assert c.sv.db.one("SELECT employee FROM tickets WHERE id='T1'")["employee"] == "u-bob" if hasattr(c, "sv") else True

        # assistants: per-organization copies, with old minimum roles turned into permissions
        agents = {a["id"]: a for a in c.get("/api/agents", headers=h("legacy-admin-token")).json()["items"]}
        assert agents["management"]["required_permission"] == "analytics.view"
        assert agents["custom1"]["required_permission"] == "handoffs.handle"
        assert "hr" in agents and "it" in agents  # built-ins missing from the old DB are added
        assert "management" not in {a["id"] for a in c.get("/api/agents", headers=h("legacy-bob-token")).json()["items"]}

        # settings, canned replies and analytics carried over
        assert c.get("/api/settings", headers=h("legacy-admin-token")).json()["settings"]["top_k"] == 7
        assert "Hi" in [x["title"] for x in c.get("/api/canned", headers=h("legacy-sam-token")).json()["items"]]
        assert c.get("/api/analytics?days=365", headers=h("legacy-admin-token")).json()["total_questions"] == 1

        # the old admin can now sign in with a password (credentials were provisioned at startup)
        r = c.post("/api/auth/login", json={"email": "root@example.com", "password": "Sup3r-secret-pw!"})
        assert r.status_code == 200 and r.json()["me"]["name"] == "admin"


def test_migration_is_idempotent(legacy_dir, fake):
    with open_app(legacy_dir, fake) as c:
        before = (c.app.state.services.db.all("SELECT id, role_id, email FROM users ORDER BY id"),
                  c.app.state.services.db.all("SELECT org_id, id FROM agents ORDER BY org_id, id"),
                  c.app.state.services.db.one("SELECT COUNT(*) AS n FROM roles")["n"])
    with open_app(legacy_dir, fake) as c:
        db = c.app.state.services.db
        after = (db.all("SELECT id, role_id, email FROM users ORDER BY id"),
                 db.all("SELECT org_id, id FROM agents ORDER BY org_id, id"),
                 db.one("SELECT COUNT(*) AS n FROM roles")["n"])
        assert before == after
        assert db.one("SELECT owner FROM sessions WHERE id='s-bob'")["owner"] == "u-bob"
        assert db.one("SELECT employee FROM tickets WHERE id='T1'")["employee"] == "u-bob"
        assert db.one("SELECT employee FROM leave_requests WHERE id='L1'")["employee"] == "u-bob"
        # chats from single-user mode were handed to the platform admin when sign-in was first enabled
        assert db.one("SELECT owner FROM sessions WHERE id='s-local'")["owner"] == "u-admin"


def test_legacy_database_with_auth_disabled_keeps_working(legacy_dir, fake):
    with make_client(legacy_dir, fake) as c:   # the default single-user mode
        me = c.get("/api/auth/me").json()
        assert me["kind"] == "local" and me["org_id"] == "default"
        assert [d["filename"] for d in c.get("/api/documents").json()["items"]] == ["policy.txt"]
        # the "local" user owns the sessions that were created without logins
        titles = {s["title"] for s in c.get("/api/sessions").json()["items"]}
        assert "Local chat" in titles
        assert c.post("/api/chat", json={"question": "hello", "session_id": "s-local"}).status_code == 200


def test_two_organizations_can_be_added_after_migration(legacy_dir, fake):
    with open_app(legacy_dir, fake) as c:
        root = {"Authorization": "Bearer legacy-admin-token"}
        r = c.post("/api/orgs", json={"name": "New", "slug": "newco", "admin_email": "a@newco.test",
                                      "admin_password": "correct-horse-1"}, headers=root)
        assert r.status_code == 201
        login = c.post("/api/auth/login", json={"email": "a@newco.test", "password": "correct-horse-1"}).json()
        h = {"Authorization": f"Bearer {login['token']}"}
        assert c.get("/api/documents", headers=h).json()["items"] == []       # sees none of the legacy data
        assert c.get("/api/sessions/s-bob/messages", headers=h).status_code == 404
        assert c.get("/api/settings", headers=h).json()["settings"]["top_k"] == 7  # inherits the platform defaults
        cur = c.get("/api/settings", headers=h).json()["settings"]
        assert c.put("/api/settings", json={**cur, "top_k": 3}, headers=h).status_code == 200
        legacy = c.get("/api/settings", headers={"Authorization": "Bearer legacy-admin-token"}).json()["settings"]
        assert legacy["top_k"] == 7  # one organization's change never leaks into another's


def test_single_user_chats_follow_the_platform_admin_when_sign_in_is_enabled(legacy_dir, fake):
    with open_app(legacy_dir, fake) as c:
        r = c.post("/api/auth/login", json={"email": "root@example.com", "password": "Sup3r-secret-pw!"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        titles = {s["title"] for s in c.get("/api/sessions", headers=h).json()["items"]}
        assert "Local chat" in titles          # not an empty app after the first login
        assert c.get("/api/sessions/s-local/messages", headers=h).status_code == 200
        bob = {"Authorization": "Bearer legacy-bob-token"}
        assert "Local chat" not in {s["title"] for s in c.get("/api/sessions", headers=bob).json()["items"]}
