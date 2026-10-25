"""Login, multi-tenancy (organization isolation), RBAC and migration of pre-tenancy databases."""
import time

import pytest
from conftest import BREAD, PARIS, PW, World, bearer, make_client, parse_sse, ready


# ============================================================================== login
def test_login_flow_and_logout(world):
    c = world.c
    org, _ = world.org("acme")
    r = c.post("/api/auth/login", json={"email": "ADMIN@acme.test", "password": PW})  # email is case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("ses_") and body["me"]["org_id"] == org["id"] and body["me"]["role"] == "admin"
    assert "users.manage" in body["me"]["permissions"] and "platform.manage" not in body["me"]["permissions"]
    h = bearer(body["token"])
    assert c.get("/api/auth/me", headers=h).json()["email"] == "admin@acme.test"
    assert c.post("/api/auth/logout", headers=h).status_code == 204
    assert c.get("/api/auth/me", headers=h).status_code == 401  # the session token is dead


def test_wrong_credentials_are_indistinguishable(world):
    world.org("acme")
    wrong_pw = world.c.post("/api/auth/login", json={"email": "admin@acme.test", "password": "nope-nope-nope"})
    no_user = world.c.post("/api/auth/login", json={"email": "ghost@acme.test", "password": "nope-nope-nope"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()  # no account enumeration


def test_login_is_unavailable_when_auth_is_off(tmp_path, fake):
    with make_client(tmp_path, fake) as c:
        r = c.post("/api/auth/login", json={"email": "a@b.co", "password": "x"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "auth_disabled"
        me = c.get("/api/auth/me").json()
        assert me["kind"] == "local" and me["is_super"] and not me["auth_enabled"]


def test_login_lockout_after_repeated_failures(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True, admin_password="Sup3r-secret-pw!",
                     login_failures_per_15min=3, auth_failures_per_minute=100) as c:
        codes = [c.post("/api/auth/login", json={"email": "root@localhost", "password": f"bad-{i}"}).status_code
                 for i in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        ok = c.post("/api/auth/login", json={"email": "root@localhost", "password": "Sup3r-secret-pw!"})
        assert ok.status_code == 429  # even the right password waits out the lockout


def test_disabled_user_and_expired_session_cannot_get_in(world):
    c = world.c
    _, admin = world.org("acme")
    bob, user = world.user(admin, "bob")
    assert c.put(f"/api/users/{user['id']}", json={"active": False}, headers=admin).status_code == 200
    assert c.get("/api/auth/me", headers=bob).status_code == 401 or c.get("/api/auth/me", headers=bob).status_code == 403
    assert c.post("/api/auth/login", json={"email": "bob@x.test", "password": PW}).status_code == 401

    c.put(f"/api/users/{user['id']}", json={"active": True}, headers=admin)
    bob = world.login("bob@x.test")
    assert c.get("/api/auth/me", headers=bob).status_code == 200
    c.sv.db.run("UPDATE auth_sessions SET expires_at='2020-01-01T00:00:00+00:00'")
    assert c.get("/api/auth/me", headers=bob).status_code == 401


def test_change_password(world):
    c = world.c
    _, admin = world.org("acme")
    bob, _ = world.user(admin, "bob")
    other = world.login("bob@x.test")  # a second device
    bad = c.post("/api/auth/password", json={"current_password": "wrong-wrong", "new_password": "brand-new-pass-9"}, headers=bob)
    assert bad.status_code == 401
    weak = c.post("/api/auth/password", json={"current_password": PW, "new_password": "short"}, headers=bob)
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"
    assert c.post("/api/auth/password", json={"current_password": PW, "new_password": "brand-new-pass-9"}, headers=bob).status_code == 204
    assert c.get("/api/auth/me", headers=bob).status_code == 200      # this session survives
    assert c.get("/api/auth/me", headers=other).status_code == 401    # other devices are signed out
    assert c.post("/api/auth/login", json={"email": "bob@x.test", "password": PW}).status_code == 401
    world.login("bob@x.test", "brand-new-pass-9")


def test_passwords_are_hashed_and_policy_enforced(world):
    c = world.c
    _, admin = world.org("acme")
    r = c.post("/api/users", json={"name": "weak", "email": "w@x.test", "password": "short", "role": "member"}, headers=admin)
    assert r.status_code == 422
    world.user(admin, "good")
    stored = c.sv.db.one("SELECT password_hash FROM users WHERE email='good@x.test'")["password_hash"]
    assert stored.startswith("scrypt$") and PW not in stored


# ============================================================================== tenant isolation
def test_each_organization_only_sees_its_own_documents_and_answers(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    doc_a = ready(c, admin_a, "paris.txt", PARIS)
    doc_b = ready(c, admin_b, "bread.txt", BREAD)
    assert doc_a["id"] != doc_b["id"]
    assert [d["filename"] for d in c.get("/api/documents", headers=admin_a).json()["items"]] == ["paris.txt"]
    assert [d["filename"] for d in c.get("/api/documents", headers=admin_b).json()["items"]] == ["bread.txt"]
    # direct access by id across the boundary: always a 404, never a 403 (no existence leak)
    assert c.get(f"/api/documents/{doc_b['id']}", headers=admin_a).status_code == 404
    assert c.delete(f"/api/documents/{doc_b['id']}", headers=admin_a).status_code == 404
    assert c.get(f"/api/documents/{doc_b['id']}", headers=admin_b).status_code == 200

    # answers never use the other tenant's documents, even when asked about them or when their ids are supplied
    cites = c.post("/api/chat", json={"question": "How do I bake sourdough bread starter?"}, headers=admin_a).json()["message"]["citations"]
    assert cites == []
    forced = c.post("/api/chat", json={"question": "How do I bake sourdough bread starter?",
                                       "document_ids": [doc_b["id"]]}, headers=admin_a).json()["message"]["citations"]
    assert forced == []
    own = c.post("/api/chat", json={"question": "Where is the Eiffel Tower?"}, headers=admin_a).json()["message"]["citations"]
    assert own and {x["document_id"] for x in own} == {doc_a["id"]}


def test_identical_files_are_stored_separately_per_organization(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    da, db = ready(c, admin_a, "policy.txt", PARIS), ready(c, admin_b, "policy.txt", PARIS)
    assert da["id"] != db["id"]  # no cross-tenant deduplication (that would leak that the file exists elsewhere)
    r = c.post("/api/documents/upload", files=[("files", ("copy.txt", PARIS))], headers=admin_a)
    assert r.json()["results"][0]["duplicate"] is True  # but duplicates inside one tenant are still caught
    c.delete(f"/api/documents/{da['id']}", headers=admin_a)
    assert c.sv.vectors.count(db["id"]) > 0  # deleting A's copy does not touch B's vectors


def test_sessions_messages_and_actions_do_not_cross_organizations(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    sid = c.post("/api/sessions", json={"agent_id": "it"}, headers=admin_a).json()["id"]
    world.fake.tool_script = [[{"name": "create_ticket", "args": {"title": "VPN down"}}]]
    ev = parse_sse(c.post("/api/chat/stream", json={"question": "open a ticket", "session_id": sid}, headers=admin_a).text)
    action = next(d for k, d in ev if k == "tool")["result"]["action_id"]
    mid = c.get(f"/api/sessions/{sid}/messages", headers=admin_a).json()["items"][-1]["id"]

    for method, path in [("GET", f"/api/sessions/{sid}/messages"), ("DELETE", f"/api/sessions/{sid}"),
                         ("DELETE", f"/api/sessions/{sid}/messages"), ("POST", f"/api/sessions/{sid}/handoff"),
                         ("POST", f"/api/actions/{action}/confirm"), ("POST", f"/api/actions/{action}/reject")]:
        assert c.request(method, path, headers=admin_b, json={}).status_code == 404, (method, path)
    assert c.post(f"/api/messages/{mid}/feedback", json={"rating": 1}, headers=admin_b).status_code == 404
    assert c.post("/api/chat", json={"question": "hi", "session_id": sid}, headers=admin_b).status_code == 404
    assert c.get("/api/sessions", headers=admin_b).json()["total"] == 0
    assert c.post(f"/api/actions/{action}/confirm", headers=admin_a).status_code == 200  # the owner still can


def test_tool_records_are_scoped_to_the_organization(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    for admin, title in ((admin_a, "alpha ticket"), (admin_b, "beta ticket")):
        sid = c.post("/api/sessions", json={"agent_id": "it"}, headers=admin).json()["id"]
        world.fake.tool_script = [[{"name": "create_ticket", "args": {"title": title}}]]
        ev = parse_sse(c.post("/api/chat/stream", json={"question": "ticket please", "session_id": sid}, headers=admin).text)
        c.post(f"/api/actions/{next(d for k, d in ev if k == 'tool')['result']['action_id']}/confirm", headers=admin)
    rows = c.sv.db.all("SELECT title, org_id FROM tickets ORDER BY title")
    assert [r["title"] for r in rows] == ["alpha ticket", "beta ticket"] and len({r["org_id"] for r in rows}) == 2
    sid = c.post("/api/sessions", json={"agent_id": "management"}, headers=admin_a).json()["id"]
    world.fake.tool_script = [[{"name": "business_summary"}]]
    ev = parse_sse(c.post("/api/chat/stream", json={"question": "numbers?", "session_id": sid}, headers=admin_a).text)
    assert next(d for k, d in ev if k == "tool")["result"]["open_tickets"] == 1  # only this tenant's ticket


def test_assistants_canned_replies_and_settings_are_per_organization(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    mine = c.post("/api/agents", json={"name": "Alpha bot", "system_prompt": "x", "tools": []}, headers=admin_a).json()
    names_b = [a["name"] for a in c.get("/api/agents", headers=admin_b).json()["items"]]
    assert "Alpha bot" not in names_b and "HR assistant" in names_b  # every tenant gets its own built-in set
    assert c.put(f"/api/agents/{mine['id']}", json={"name": "x", "system_prompt": "y", "tools": []}, headers=admin_b).status_code == 404
    assert c.delete(f"/api/agents/{mine['id']}", headers=admin_b).status_code == 404
    assert c.post("/api/sessions", json={"agent_id": mine["id"]}, headers=admin_b).status_code == 404
    # editing a built-in assistant in one tenant does not change it in another
    c.put("/api/agents/hr", json={"name": "Alpha HR", "system_prompt": "alpha only", "tools": []}, headers=admin_a)
    hr_b = next(a for a in c.get("/api/agents", headers=admin_b).json()["items"] if a["id"] == "hr")
    assert hr_b["name"] == "HR assistant" and hr_b["system_prompt"] != "alpha only"

    created = c.post("/api/canned", json={"title": "Alpha only", "text": "t"}, headers=admin_a).json()
    assert "Alpha only" not in [x["title"] for x in c.get("/api/canned", headers=admin_b).json()["items"]]
    assert c.delete(f"/api/canned/{created['id']}", headers=admin_b).status_code == 404

    cur = c.get("/api/settings", headers=admin_a).json()["settings"]
    assert c.put("/api/settings", json={**cur, "top_k": 9, "temperature": 0.9}, headers=admin_a).status_code == 200
    assert c.get("/api/settings", headers=admin_b).json()["settings"]["top_k"] == cur["top_k"]
    assert c.get("/api/settings", headers=admin_a).json()["settings"]["top_k"] == 9


def test_org_admins_cannot_change_or_see_the_platform_ollama_url(world):
    c = world.c
    _, admin = world.org("alpha")
    shown = c.get("/api/settings", headers=admin).json()["settings"]
    assert shown["ollama_url"].endswith(".invalid")  # masked
    real = c.get("/api/platform/settings", headers=world.root).json()["ollama_url"]
    c.put("/api/settings", json={**shown, "ollama_url": "http://evil.example:11434", "top_k": 4}, headers=admin)
    assert c.get("/api/platform/settings", headers=world.root).json()["ollama_url"] == real  # ignored
    assert c.get("/api/platform/settings", headers=admin).status_code == 403
    assert c.put("/api/platform/settings", json={**shown, "ollama_url": "http://evil.example:11434"}, headers=admin).status_code == 403
    assert c.get("/api/health", headers=admin).json()["ollama"]["url"] == ""  # not even through health
    r = c.put("/api/platform/settings", json={**c.get("/api/platform/settings", headers=world.root).json(),
                                              "ollama_url": "http://10.1.2.3:11434"}, headers=world.root)
    assert r.status_code == 200 and r.json()["ollama_url"] == "http://10.1.2.3:11434"


def test_handoffs_connectors_analytics_users_roles_are_per_organization(world, tmp_path):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    # handoffs
    sid = c.post("/api/sessions", json={}, headers=admin_a).json()["id"]
    c.post(f"/api/sessions/{sid}/handoff", headers=admin_a)
    hid = c.get("/api/handoffs", headers=admin_a).json()["items"][0]["id"]
    assert c.get("/api/handoffs", headers=admin_b).json()["items"] == []
    for action in ("claim", "resolve"):
        assert c.post(f"/api/handoffs/{hid}/{action}", headers=admin_b).status_code == 404
    assert c.post(f"/api/handoffs/{hid}/reply", json={"text": "x"}, headers=admin_b).status_code == 404
    # analytics
    c.post("/api/chat", json={"question": "hello there"}, headers=admin_a)
    assert c.get("/api/analytics", headers=admin_a).json()["total_questions"] == 1
    assert c.get("/api/analytics", headers=admin_b).json()["total_questions"] == 0
    # users: the same display name may exist in two organizations; emails are globally unique
    assert c.post("/api/users", json={"name": "sam", "role": "member"}, headers=admin_a).status_code == 201
    assert c.post("/api/users", json={"name": "sam", "role": "member"}, headers=admin_b).status_code == 201
    assert c.post("/api/users", json={"name": "sam", "role": "member"}, headers=admin_a).status_code == 409
    assert c.post("/api/users", json={"name": "x1", "email": "admin@alpha.test", "password": PW, "role": "member"},
                  headers=admin_b).status_code == 409
    uid_a = next(u["id"] for u in c.get("/api/users", headers=admin_a).json()["items"] if u["name"] == "sam")
    assert uid_a not in [u["id"] for u in c.get("/api/users", headers=admin_b).json()["items"]]
    for method, path in [("PUT", f"/api/users/{uid_a}"), ("DELETE", f"/api/users/{uid_a}"), ("POST", f"/api/users/{uid_a}/api-key")]:
        assert c.request(method, path, json={"active": False}, headers=admin_b).status_code == 404
    # custom roles are private to their organization
    role = c.post("/api/roles", json={"name": "Analyst", "permissions": ["chat.use", "analytics.view"]}, headers=admin_a).json()
    assert role["id"] not in [r["id"] for r in c.get("/api/roles", headers=admin_b).json()]
    assert c.post("/api/users", json={"name": "z", "role": role["id"]}, headers=admin_b).status_code == 422
    assert c.put(f"/api/roles/{role['id']}", json={"name": "Hacked", "permissions": ["chat.use"]}, headers=admin_b).status_code == 404
    assert c.delete(f"/api/roles/{role['id']}", headers=admin_b).status_code == 404


def test_connector_folders_are_confined_to_the_tenants_own_directory(tmp_path, fake):
    root = tmp_path / "sources"
    with make_client(tmp_path / "d", fake, auth_enabled=True, admin_password="Sup3r-secret-pw!",
                     connector_roots=str(root)) as c:
        w = World(c, (tmp_path / "d" / "admin_token.txt").read_text().strip())
        (_, admin_a), (_, admin_b) = w.org("alpha"), w.org("beta")
        assert (root / "alpha").is_dir() and (root / "beta").is_dir()  # created with the organization
        (root / "alpha" / "kb.txt").write_text("alpha private knowledge about quantum gears " * 5)
        (root / "beta" / "kb.txt").write_text("beta private knowledge about sourdough yeast " * 5)
        own = c.post("/api/connectors", json={"type": "folder", "ref": str(root / "alpha")}, headers=admin_a)
        assert own.status_code == 201
        assert c.post("/api/connectors", json={"type": "folder", "ref": str(root / "beta")}, headers=admin_a).status_code == 403
        assert c.post("/api/connectors", json={"type": "folder", "ref": str(root)}, headers=admin_a).status_code == 403
        assert c.post("/api/connectors", json={"type": "folder", "ref": str(root / "alpha" / ".." / "beta")},
                      headers=admin_a).status_code == 403
        assert c.post("/api/connectors", json={"type": "folder", "ref": str(root)}, headers=w.root).status_code == 201
        cid = own.json()["id"]
        assert c.get("/api/connectors", headers=admin_b).json()["items"] == []
        assert c.post(f"/api/connectors/{cid}/sync", headers=admin_b).status_code == 404
        assert c.delete(f"/api/connectors/{cid}", headers=admin_b).status_code == 404
        end = time.time() + 20
        while time.time() < end and not c.get("/api/documents", headers=admin_a).json()["items"]:
            time.sleep(0.2)
        assert [d["filename"] for d in c.get("/api/documents", headers=admin_a).json()["items"]] == ["kb.txt"]
        assert c.get("/api/documents", headers=admin_b).json()["items"] == []


def test_platform_endpoints_and_org_switching(world):
    c = world.c
    org_a, admin_a = world.org("alpha")
    org_b, admin_b = world.org("beta")
    ready(c, admin_b, "bread.txt", BREAD)
    for method, path in [("GET", "/api/orgs"), ("POST", "/api/orgs"), ("PUT", f"/api/orgs/{org_b['id']}")]:
        assert c.request(method, path, json={}, headers=admin_a).status_code == 403
    # an organization admin cannot act inside another organization
    assert c.get("/api/documents", headers={**admin_a, "X-Org-Id": org_b["id"]}).status_code == 403
    # the platform admin can, and sees that organization's data
    as_b = c.get("/api/documents", headers={**world.root, "X-Org-Id": org_b["id"]}).json()["items"]
    assert [d["filename"] for d in as_b] == ["bread.txt"]
    assert c.get("/api/documents", headers={**world.root, "X-Org-Id": org_a["id"]}).json()["items"] == []
    assert c.get("/api/documents", headers={**world.root, "X-Org-Id": "nope"}).status_code == 404
    assert c.get("/api/auth/me", headers={**world.root, "X-Org-Id": org_b["id"]}).json()["org_id"] == org_b["id"]


def test_disabling_an_organization_locks_everyone_out_immediately(world):
    c = world.c
    org, admin = world.org("alpha")
    bob, _ = world.user(admin, "bob")
    assert c.put(f"/api/orgs/{org['id']}", json={"active": False}, headers=world.root).status_code == 200
    assert c.get("/api/auth/me", headers=bob).status_code == 401  # sessions were revoked
    assert c.get("/api/auth/me", headers=admin).status_code == 401
    r = c.post("/api/auth/login", json={"email": "admin@alpha.test", "password": PW})
    assert r.status_code == 403 and r.json()["error"]["code"] == "org_disabled"
    c.put(f"/api/orgs/{org['id']}", json={"active": True}, headers=world.root)
    world.login("admin@alpha.test")


def test_org_creation_validation(world):
    c = world.c
    body = {"name": "Acme", "slug": "acme", "admin_email": "a@acme.test", "admin_password": PW}
    assert c.post("/api/orgs", json={**body, "slug": "Bad Slug!"}, headers=world.root).status_code == 422
    assert c.post("/api/orgs", json={**body, "admin_password": "short"}, headers=world.root).status_code == 422
    assert c.post("/api/orgs", json={**body, "admin_email": "not-an-email"}, headers=world.root).status_code == 422
    assert c.post("/api/orgs", json=body, headers=world.root).status_code == 201
    assert c.post("/api/orgs", json={**body, "admin_email": "other@acme.test"}, headers=world.root).status_code == 409  # slug
    assert c.post("/api/orgs", json={**body, "slug": "acme2"}, headers=world.root).status_code == 409                  # email
    listing = c.get("/api/orgs", headers=world.root).json()["items"]
    assert {o["slug"] for o in listing} == {"default", "acme"}
    assert next(o for o in listing if o["slug"] == "acme")["user_count"] == 1


# ============================================================================== RBAC
MATRIX = [  # (method, path, body, permission needed)
    ("GET", "/api/documents", None, "documents.read"),
    ("POST", "/api/documents/upload", "upload", "documents.write"),
    ("GET", "/api/settings", None, "settings.read"),
    ("GET", "/api/models", None, "settings.write"),
    ("POST", "/api/agents", {"name": "n", "system_prompt": "p", "tools": []}, "agents.manage"),
    ("GET", "/api/connectors", None, "connectors.manage"),
    ("GET", "/api/analytics", None, "analytics.view"),
    ("GET", "/api/handoffs", None, "handoffs.handle"),
    ("GET", "/api/canned", None, "handoffs.handle"),
    ("GET", "/api/users", None, "users.manage"),
    ("POST", "/api/roles", {"name": "rr", "permissions": ["chat.use"]}, "roles.manage"),
    ("GET", "/api/embed-keys", None, "embed.manage"),
    ("GET", "/api/orgs", None, "platform.manage"),
    ("GET", "/api/sessions", None, "chat.use"),
]


def call(c, headers, method, path, body):
    if body == "upload":
        return c.post(path, files=[("files", ("n.txt", b"hello world document text"))], headers=headers)
    return c.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize("role", ["member", "agent", "admin"])
def test_builtin_role_matrix(world, role):
    c = world.c
    _, admin = world.org("alpha")
    headers = admin if role == "admin" else world.user(admin, f"u-{role}", role)[0]
    from app.core import rbac
    granted = rbac.BUILTIN_ROLES[role]["permissions"]
    for method, path, body, perm in MATRIX:
        status = call(c, headers, method, path, body).status_code
        if perm in granted:
            assert status not in (401, 403), (role, method, path, status)
        else:
            assert status == 403, (role, method, path, status)


def test_unauthenticated_requests_are_rejected_everywhere(world):
    for method, path, body, _ in MATRIX:
        assert call(world.c, {}, method, path, body).status_code == 401, path


def test_custom_roles_grant_exactly_their_permissions(world):
    c = world.c
    _, admin = world.org("alpha")
    role = c.post("/api/roles", json={"name": "Analyst", "permissions": ["chat.use", "analytics.view"]}, headers=admin).json()
    ana, _ = world.user(admin, "ana", role["id"])
    assert c.get("/api/analytics", headers=ana).status_code == 200
    assert c.post("/api/sessions", json={}, headers=ana).status_code == 201
    assert c.get("/api/documents", headers=ana).status_code == 403   # not granted
    assert c.get("/api/users", headers=ana).status_code == 403
    # permissions change immediately for existing logins
    c.put(f"/api/roles/{role['id']}", json={"name": "Analyst", "permissions": ["chat.use"]}, headers=admin)
    assert c.get("/api/analytics", headers=ana).status_code == 403
    me = c.get("/api/auth/me", headers=ana).json()
    assert me["role"] == "Analyst" and me["permissions"] == ["chat.use"]


def test_role_catalog_rules(world):
    c = world.c
    _, admin = world.org("alpha")
    cat = c.get("/api/permissions", headers=admin).json()
    assert "analytics.view" in cat["permissions"] and "chat.use" in cat["assistant_choices"]
    assert c.post("/api/roles", json={"name": "Bad", "permissions": ["platform.manage"]}, headers=admin).status_code == 422
    assert c.post("/api/roles", json={"name": "Bad", "permissions": ["nope.nope"]}, headers=admin).status_code == 422
    assert c.post("/api/roles", json={"name": "admin", "permissions": ["chat.use"]}, headers=admin).status_code == 409
    roles = {r["id"]: r for r in c.get("/api/roles", headers=admin).json()}
    assert {"admin", "agent", "member"} <= set(roles) and "super_admin" not in roles
    for rid in ("admin", "agent", "member"):
        assert c.put(f"/api/roles/{rid}", json={"name": "xx", "permissions": ["chat.use"]}, headers=admin).status_code == 404
        assert c.delete(f"/api/roles/{rid}", headers=admin).status_code == 404
    role = c.post("/api/roles", json={"name": "Temp", "permissions": ["chat.use"]}, headers=admin).json()
    world.user(admin, "tmp", role["id"])
    assert c.delete(f"/api/roles/{role['id']}", headers=admin).status_code == 409  # still assigned


def test_nobody_can_grant_permissions_they_do_not_hold(world):
    c = world.c
    _, admin = world.org("alpha")
    maker = c.post("/api/roles", json={"name": "RoleMaker", "permissions": ["chat.use", "roles.manage", "users.manage"]},
                   headers=admin).json()
    rm, _ = world.user(admin, "rm", maker["id"])
    # may create roles using only permissions they hold ...
    assert c.post("/api/roles", json={"name": "Small", "permissions": ["chat.use"]}, headers=rm).status_code == 201
    # ... but not powers they lack
    r = c.post("/api/roles", json={"name": "Big", "permissions": ["chat.use", "documents.write"]}, headers=rm)
    assert r.status_code == 403 and "documents.write" in r.json()["error"]["message"]
    # cannot assign the built-in admin role, nor touch an admin, nor promote themselves
    assert c.post("/api/users", json={"name": "evil", "role": "admin"}, headers=rm).status_code == 403
    admin_id = next(u["id"] for u in c.get("/api/users", headers=admin).json()["items"] if u["email"] == "admin@alpha.test")
    assert c.put(f"/api/users/{admin_id}", json={"active": False}, headers=rm).status_code == 403
    assert c.delete(f"/api/users/{admin_id}", headers=rm).status_code == 403
    assert c.post(f"/api/users/{admin_id}/api-key", headers=rm).status_code == 403
    me_id = c.get("/api/auth/me", headers=rm).json()["id"]
    assert c.put(f"/api/users/{me_id}", json={"role": "admin"}, headers=rm).status_code in (403, 409)
    # an org admin can never mint a platform super admin
    assert c.post("/api/users", json={"name": "root", "role": "super_admin"}, headers=admin).status_code in (403, 422)


def test_user_management_safety_rails(world):
    c = world.c
    org, admin = world.org("alpha")
    me_id = c.get("/api/auth/me", headers=admin).json()["id"]
    assert c.delete(f"/api/users/{me_id}", headers=admin).status_code == 409
    assert c.put(f"/api/users/{me_id}", json={"active": False}, headers=admin).status_code == 409
    assert c.put(f"/api/users/{me_id}", json={"role": "member"}, headers=admin).status_code == 409
    # editing the only role that carries users.manage must not lock the organization out
    mgr = c.post("/api/roles", json={"name": "UserMgr", "permissions": ["chat.use", "users.manage", "roles.manage"]}, headers=admin).json()
    boss, _ = world.user(admin, "boss", mgr["id"])
    assert c.put(f"/api/users/{me_id}", json={"role": "member"}, headers=boss).status_code == 403  # cannot touch a more powerful user
    c.put(f"/api/users/{me_id}", json={"role": "member"}, headers={**world.root, "X-Org-Id": org["id"]})  # the platform can
    r = c.put(f"/api/roles/{mgr['id']}", json={"name": "UserMgr", "permissions": ["chat.use", "roles.manage"]}, headers=boss)
    assert r.status_code == 409 and "manage users" in r.json()["error"]["message"]
    # deactivating a user revokes their sessions at once; resetting a password does too
    chatter = c.post("/api/roles", json={"name": "Chatter", "permissions": ["chat.use"]}, headers=boss).json()
    victim, vu = world.user(boss, "victim", chatter["id"])
    assert c.get("/api/auth/me", headers=victim).status_code == 200
    c.put(f"/api/users/{vu['id']}", json={"password": "another-pass-77"}, headers=boss)
    assert c.get("/api/auth/me", headers=victim).status_code == 401
    world.login("victim@x.test", "another-pass-77")


def test_api_keys_work_and_can_be_rotated(world):
    c = world.c
    _, admin = world.org("alpha")
    created = c.post("/api/users", json={"name": "svc", "role": "member"}, headers=admin).json()
    assert created["token"].startswith("rag_") and created["has_api_key"] and not created["has_password"]
    key = {"X-API-Key": created["token"]}
    assert c.get("/api/auth/me", headers=key).json()["role"] == "member"
    new = c.post(f"/api/users/{created['id']}/api-key", headers=admin).json()["token"]
    assert c.get("/api/auth/me", headers=key).status_code == 401 and c.get("/api/auth/me", headers={"X-API-Key": new}).status_code == 200
    assert c.post("/api/users", json={"name": "nothing", "role": "member", "api_key": False}, headers=admin).status_code == 422


def test_assistants_can_require_a_permission(world):
    c = world.c
    _, admin = world.org("alpha")
    bob, _ = world.user(admin, "bob")
    # Management insights needs analytics.view
    assert "management" not in {a["id"] for a in c.get("/api/agents", headers=bob).json()["items"]}
    assert c.post("/api/sessions", json={"agent_id": "management"}, headers=bob).status_code == 403
    assert c.post("/api/sessions", json={"agent_id": "management"}, headers=admin).status_code == 201
    # an admin can require a permission for their own assistant, but not one they lack
    ok = c.post("/api/agents", json={"name": "Staff", "system_prompt": "x", "tools": [], "required_permission": "handoffs.handle"}, headers=admin)
    assert ok.status_code == 201
    assert c.post("/api/agents", json={"name": "Bad", "system_prompt": "x", "tools": [], "required_permission": "platform.manage"}, headers=admin).status_code == 422
    # non-admins never see prompts
    hr = next(a for a in c.get("/api/agents", headers=bob).json()["items"] if a["id"] == "hr")
    assert hr["system_prompt"] == "" and hr["document_ids"] == []
