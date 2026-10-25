"""Exhaustive cross-tenant attack sweep.

Organization ALPHA owns one of every kind of resource. Three different attackers from organization BETA
(an admin, a plain member and an embedded-widget visitor) then call EVERY endpoint in the OpenAPI schema
with ALPHA's ids in every path parameter. Nothing may succeed, and ALPHA's data must be untouched afterwards.
New endpoints are picked up automatically, so a forgotten org check on a future route fails this test.
"""
import itertools
import re
import time

import pytest
from conftest import BREAD, PARIS, World, bearer, make_client, parse_sse, ready

SHOP = "https://shop.beta.example"
SUCCESS = {200, 201, 202, 204}
# endpoints that are legitimately open to any caller and take no tenant data in the path
OPEN_BY_DESIGN = {("GET", "/api/health"), ("POST", "/api/auth/login"), ("GET", "/api/auth/me"),
                  ("POST", "/api/auth/logout"), ("POST", "/api/auth/password")}


@pytest.fixture
def arena(tmp_path, fake):
    roots = tmp_path / "sources"
    with make_client(tmp_path / "data", fake, auth_enabled=True, admin_password="Sup3r-secret-pw!",
                     connector_roots=str(roots), rate_limit_per_minute=100000, auth_failures_per_minute=100000) as c:
        c.sv = c.app.state.services
        w = World(c, (tmp_path / "data" / "admin_token.txt").read_text().strip())
        w.fake = fake
        w.roots = roots
        yield w


def build_alpha(w: World):
    """One of everything, owned by ALPHA. Returns ids keyed by the names used in URL templates."""
    c = w.c
    org, admin = w.org("alpha")
    ids = {"org_id": org["id"]}
    doc = ready(c, admin, "paris.txt", PARIS)
    ids["document_id"] = doc["id"]
    sid = c.post("/api/sessions", json={"agent_id": "it"}, headers=admin).json()["id"]
    ids["session_id"] = sid
    w.fake.tool_script = [[{"name": "create_ticket", "args": {"title": "alpha secret ticket"}}]]
    ev = parse_sse(c.post("/api/chat/stream", json={"question": "Where is the Eiffel Tower? open a ticket", "session_id": sid},
                          headers=admin).text)
    ids["action_id"] = next(d for k, d in ev if k == "tool")["result"]["action_id"]
    ids["message_id"] = c.get(f"/api/sessions/{sid}/messages", headers=admin).json()["items"][-1]["id"]
    sid2 = c.post("/api/sessions", json={}, headers=admin).json()["id"]
    c.post(f"/api/sessions/{sid2}/handoff", headers=admin)
    ids["hid"] = c.get("/api/handoffs", headers=admin).json()["items"][0]["id"]
    ids["canned"] = c.post("/api/canned", json={"title": "alpha canned", "text": "t"}, headers=admin).json()["id"]
    (w.roots / "alpha" / "kb.txt").write_text("alpha only knowledge " * 10)
    ids["connector"] = c.post("/api/connectors", json={"type": "folder", "ref": str(w.roots / "alpha")}, headers=admin).json()["id"]
    ids["agent_id"] = c.post("/api/agents", json={"name": "Alpha bot", "system_prompt": "p", "tools": []}, headers=admin).json()["id"]
    ids["role_id"] = c.post("/api/roles", json={"name": "AlphaRole", "permissions": ["chat.use"]}, headers=admin).json()["id"]
    ids["uid"] = c.post("/api/users", json={"name": "victim", "role": "member"}, headers=admin).json()["id"]
    ids["kid"] = c.post("/api/embed-keys", json={"name": "k", "allowed_origins": ["https://alpha.example"]}, headers=admin).json()["id"]
    # let the connector finish so the snapshot below is stable
    end = time.time() + 20
    while time.time() < end and not c.get("/api/connectors", headers=admin).json()["items"][0]["last_status"]:
        time.sleep(0.1)
    time.sleep(0.8)
    return admin, ids


def snapshot(w: World, admin):
    """Everything ALPHA owns, as its own admin sees it."""
    c = w.c
    docs = c.get("/api/documents", headers=admin).json()["items"]
    out = {
        "docs": sorted((d["id"], d["filename"], d["status"]) for d in docs),
        "vectors": {d["id"]: c.sv.vectors.count(d["id"]) for d in docs},
        "sessions": sorted(s["id"] for s in c.get("/api/sessions", headers=admin).json()["items"]),
        "handoffs": sorted((h["id"], h["status"]) for h in c.get("/api/handoffs", headers=admin).json()["items"]),
        "canned": sorted(x["id"] for x in c.get("/api/canned", headers=admin).json()["items"]),
        "connectors": sorted(x["id"] for x in c.get("/api/connectors", headers=admin).json()["items"]),
        "agents": sorted((a["id"], a["name"]) for a in c.get("/api/agents", headers=admin).json()["items"]),
        "roles": sorted((r["id"], r["name"]) for r in c.get("/api/roles", headers=admin).json()),
        "users": sorted((u["id"], u["active"], u["role_id"]) for u in c.get("/api/users", headers=admin).json()["items"]),
        "keys": sorted((k["id"], k["active"], tuple(k["allowed_origins"])) for k in c.get("/api/embed-keys", headers=admin).json()["items"]),
        "settings": c.get("/api/settings", headers=admin).json()["settings"],
        "tickets": c.sv.db.one("SELECT COUNT(*) AS n FROM tickets")["n"],
        "action": c.sv.store.get_action(next(iter(c.sv.db.all("SELECT id FROM pending_actions")))["id"])["status"],
        "messages": c.sv.db.one("SELECT COUNT(*) AS n FROM messages")["n"],
    }
    return out


def candidates(template: str, ids: dict) -> list[str]:
    """All ways to fill the path parameters of ``template`` with ALPHA's ids."""
    names = re.findall(r"\{(\w+)\}", template)
    if not names:
        return [template]
    pools = []
    for n in names:
        pools.append({"cid": [ids["connector"], ids["canned"]], "org_id": [ids["org_id"]]}.get(n) or [ids[n]])
    return [re.sub(r"\{\w+\}", lambda m, it=iter(combo): next(it), template) for combo in itertools.product(*pools)]


def body_for(path: str):
    if path.endswith("/documents/upload"):
        return {"files": [("files", ("evil.txt", b"attacker data"))]}
    if "transcribe" in path:
        return {"files": {"audio": ("a.webm", b"x")}}
    return {"json": {}}


def run_attack(w: World, attacker_headers: dict, ids: dict, label: str):
    c = w.c
    spec = c.app.openapi()  # the HTTP schema endpoint is switched off when auth is on
    unexpected = []
    tried = 0
    for template, ops in spec["paths"].items():
        for method in ops:
            if method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                continue
            if (method.upper(), template) in OPEN_BY_DESIGN:
                continue
            for url in candidates(template, ids):
                tried += 1
                r = c.request(method.upper(), url, headers=attacker_headers, **body_for(template))
                if r.status_code in SUCCESS:
                    unexpected.append((label, method.upper(), url, r.status_code))
    assert tried > 40, f"sweep looked too small ({tried} requests): did the schema load?"
    return unexpected


# Endpoints a BETA user may legitimately call successfully even with ALPHA's ids in the URL, because the id is
# ignored or refers to their own scope. Anything else returning 2xx is a leak.
ALLOWED_SUCCESS = {
    ("GET", "/api/sessions"), ("GET", "/api/documents"), ("GET", "/api/agents"), ("GET", "/api/analytics"),
    ("GET", "/api/connectors"), ("GET", "/api/handoffs"), ("GET", "/api/canned"), ("GET", "/api/users"),
    ("GET", "/api/roles"), ("GET", "/api/embed-keys"), ("GET", "/api/settings"), ("GET", "/api/models"),
    ("GET", "/api/permissions"), ("GET", "/api/voice/status"), ("GET", "/api/embed/config"),
    ("PUT", "/api/settings"), ("POST", "/api/sessions"), ("POST", "/api/agents"), ("POST", "/api/canned"),
    ("POST", "/api/roles"), ("POST", "/api/users"), ("POST", "/api/embed-keys"), ("POST", "/api/connectors"),
    ("POST", "/api/documents/upload"), ("POST", "/api/chat"), ("POST", "/api/chat/stream"),
    ("POST", "/api/handoffs/paraphrase"),
}


def leaks(found):
    return [f for f in found if (f[1], re.sub(r"/[0-9a-f]{32}", "/{id}", f[2])) not in
            {(m, p) for m, p in ALLOWED_SUCCESS}]


def test_org_admin_of_another_tenant_cannot_touch_alphas_data(arena):
    w = arena
    admin_a, ids = build_alpha(w)
    _, admin_b = w.org("beta")
    before = snapshot(w, admin_a)
    found = run_attack(w, admin_b, ids, "beta admin")
    assert leaks(found) == [], leaks(found)
    assert snapshot(w, admin_a) == before  # nothing in ALPHA changed


def test_plain_member_of_another_tenant_cannot_touch_alphas_data(arena):
    w = arena
    admin_a, ids = build_alpha(w)
    _, admin_b = w.org("beta")
    member_b, _ = w.user(admin_b, "mallory")
    before = snapshot(w, admin_a)
    found = run_attack(w, member_b, ids, "beta member")
    assert leaks(found) == [], leaks(found)
    assert snapshot(w, admin_a) == before


def test_embedded_visitor_of_another_tenant_cannot_touch_alphas_data(arena):
    w = arena
    admin_a, ids = build_alpha(w)
    _, admin_b = w.org("beta")
    key = w.c.post("/api/embed-keys", json={"name": "b", "allowed_origins": [SHOP]}, headers=admin_b).json()
    visitor = {"X-Embed-Key": key["token"], "X-Visitor-Id": "visitor-evil-01", "Origin": SHOP}
    before = snapshot(w, admin_a)
    found = run_attack(w, visitor, ids, "beta visitor")
    assert leaks(found) == [], leaks(found)
    assert snapshot(w, admin_a) == before


def test_alphas_embed_key_cannot_be_used_from_betas_website(arena):
    w = arena
    admin_a, _ = build_alpha(w)
    key = w.c.get("/api/embed-keys", headers=admin_a).json()["items"][0]
    stolen = {"X-Embed-Key": key["token"], "X-Visitor-Id": "visitor-evil-01", "Origin": SHOP}
    assert w.c.get("/api/auth/me", headers=stolen).status_code == 403   # a competitor's site cannot reuse the key


def test_unauthenticated_sweep_reveals_nothing(arena):
    w = arena
    _, ids = build_alpha(w)
    found = run_attack(w, {}, ids, "anonymous")
    assert found == [] or all(f[1] == "GET" and f[2] in ("/widget.js", "/widget-demo") for f in found), found


def test_super_admin_scoping_is_explicit(arena):
    """The platform admin sees an organization's data only when it names that organization."""
    w = arena
    admin_a, ids = build_alpha(w)
    default_view = w.c.get("/api/documents", headers=w.root).json()["items"]
    assert default_view == []                                   # own (default) organization: nothing from alpha
    scoped = w.c.get("/api/documents", headers={**w.root, "X-Org-Id": ids["org_id"]}).json()["items"]
    assert ids["document_id"] in [d["id"] for d in scoped] and len(scoped) >= 1
    assert w.c.get(f"/api/documents/{ids['document_id']}", headers=w.root).status_code == 404  # not without naming the org
    other = bearer(w.root["Authorization"].split()[1])
    assert w.c.get(f"/api/sessions/{ids['session_id']}/messages", headers=other).status_code == 404
    # and what the platform admin does inside an organization stays inside it
    w.c.post("/api/chat", json={"question": "hello"}, headers={**w.root, "X-Org-Id": ids["org_id"]})
    assert snapshot(w, admin_a)["sessions"].count(ids["session_id"]) == 1
    assert BREAD  # (shared constant imported for symmetry with the other tests)
