"""Embedding the chat on third-party websites: publishable keys, origin allow-lists, visitors, dynamic CORS."""
import pytest
from conftest import BREAD, PARIS, World, make_client, parse_sse, ready

SHOP = "https://shop.example.com"


def make_key(world, admin, origins=(SHOP,), agent=None, **extra):
    body = {"name": "Shop", "allowed_origins": list(origins), **extra}
    if agent:
        body["agent_id"] = agent
    r = world.c.post("/api/embed-keys", json=body, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def vh(key, origin=SHOP, visitor="visitor-0001"):
    h = {"X-Embed-Key": key["token"], "X-Visitor-Id": visitor}
    if origin:
        h["Origin"] = origin
    return h


# ------------------------------------------------------------------ managing keys
def test_key_lifecycle_and_snippet(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin, ["https://shop.example.com/", "HTTPS://Blog.Example.com", "https://*.example.org"],
                   agent="general", title="Help", welcome="Hello!", color="#112233")
    assert key["token"].startswith("pk_") and key["wildcard"] is False
    assert key["allowed_origins"] == ["https://shop.example.com", "https://blog.example.com", "https://*.example.org"]
    assert f'data-key="{key["token"]}"' in key["snippet"] and key["snippet"].startswith("<script src=")
    assert key["snippet"].count("/widget.js") == 1 and 'data-agent="general"' in key["snippet"]

    listed = c.get("/api/embed-keys", headers=admin).json()["items"]
    assert [k["id"] for k in listed] == [key["id"]] and listed[0]["token"] == key["token"]  # snippet can be re-copied later

    upd = c.put(f"/api/embed-keys/{key['id']}", json={"allowed_origins": ["*"], "title": "New", "active": False}, headers=admin).json()
    assert upd["wildcard"] is True and upd["title"] == "New" and upd["active"] is False
    assert c.put(f"/api/embed-keys/{key['id']}", json={"clear_agent": True}, headers=admin).json()["agent_id"] is None
    assert c.delete(f"/api/embed-keys/{key['id']}", headers=admin).status_code == 204
    assert c.get("/api/embed-keys", headers=admin).json()["items"] == []


def test_key_validation(world):
    c = world.c
    _, admin = world.org("alpha")
    post = lambda **b: c.post("/api/embed-keys", json={"name": "k", **b}, headers=admin).status_code
    assert post(allowed_origins=[]) == 422                                     # a key nobody can use is a mistake
    assert post() == 422
    for bad in ("example.com", "https://example.com/path", "javascript:alert(1)", "https://", "https://a b.com",
                "https://*.*.example.com", "ftp://example.com"):
        assert post(allowed_origins=[bad]) == 422, bad
    assert post(allowed_origins=[SHOP], color="red") == 422
    assert post(allowed_origins=[SHOP], agent_id="nope") == 404
    assert post(allowed_origins=[SHOP], agent_id="management") == 422          # needs permissions visitors cannot have
    assert post(allowed_origins=[SHOP], agent_id="hr") == 201
    assert post(allowed_origins=["http://localhost:3000", "https://*.example.com", "*"]) == 201


def test_keys_are_private_to_their_organization(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    key = make_key(world, admin_a)
    assert c.get("/api/embed-keys", headers=admin_b).json()["items"] == []
    assert c.put(f"/api/embed-keys/{key['id']}", json={"active": False}, headers=admin_b).status_code == 404
    assert c.delete(f"/api/embed-keys/{key['id']}", headers=admin_b).status_code == 404
    member, _ = world.user(admin_a, "bob")
    assert c.get("/api/embed-keys", headers=member).status_code == 403
    assert c.post("/api/embed-keys", json={"name": "k", "allowed_origins": [SHOP]}, headers=member).status_code == 403


# ------------------------------------------------------------------ visitors
def test_visitor_can_chat_and_is_pinned_to_the_keys_assistant(world):
    c = world.c
    _, admin = world.org("alpha")
    doc = ready(c, admin, "paris.txt", PARIS)
    key = make_key(world, admin, agent="general", title="Paris help")
    r = c.post("/api/chat", json={"question": "Where is the Eiffel Tower?", "agent_id": "management"}, headers=vh(key))
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert msg["citations"][0]["document_id"] == doc["id"]
    session = c.get("/api/sessions", headers=vh(key)).json()["items"][0]
    assert session["agent_id"] == "general"  # the request asked for another assistant; the key decides

    ev = parse_sse(c.post("/api/chat/stream", json={"question": "Where is the Eiffel Tower?"}, headers=vh(key)).text)
    assert next(k for k, _ in ev) == "session" and ev[-1][0] == "done"


def test_visitor_is_locked_down_to_chat_only(world):
    c = world.c
    _, admin = world.org("alpha")
    ready(c, admin, "paris.txt", PARIS)
    key = make_key(world, admin, agent="hr")
    h = vh(key)
    for method, path in [("GET", "/api/documents"), ("POST", "/api/documents/upload"), ("GET", "/api/settings"),
                         ("PUT", "/api/settings"), ("GET", "/api/users"), ("POST", "/api/users"), ("GET", "/api/roles"),
                         ("GET", "/api/analytics"), ("GET", "/api/handoffs"), ("GET", "/api/canned"),
                         ("GET", "/api/connectors"), ("GET", "/api/embed-keys"), ("POST", "/api/embed-keys"),
                         ("GET", "/api/orgs"), ("GET", "/api/models"), ("POST", "/api/agents"), ("GET", "/api/platform/settings")]:
        r = c.request(method, path, json={}, headers=h)
        assert r.status_code == 403, (method, path, r.status_code)
    agents = c.get("/api/agents", headers=h).json()["items"]
    assert [a["id"] for a in agents] == ["hr"] and agents[0]["system_prompt"] == ""  # only its own assistant, no config
    assert c.post("/api/voice/transcribe", files={"audio": ("a.webm", b"x")}, headers=h).status_code == 403
    assert c.get("/api/voice/status", headers=h).json()["stt_available"] is False
    me = c.get("/api/auth/me", headers=h).json()
    assert me["kind"] == "visitor" and me["permissions"] == ["chat.use"] and not me["is_super"]
    health = c.get("/api/health", headers=h).json()["ollama"]
    assert health["url"] == "" and health["chat_model"] == ""


def test_visitors_cannot_see_each_others_conversations(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin)
    c.post("/api/chat", json={"question": "my private question"}, headers=vh(key, visitor="visitor-AAAA"))
    sid = c.get("/api/sessions", headers=vh(key, visitor="visitor-AAAA")).json()["items"][0]["id"]
    other = vh(key, visitor="visitor-BBBB")
    assert c.get("/api/sessions", headers=other).json()["total"] == 0
    assert c.get(f"/api/sessions/{sid}/messages", headers=other).status_code == 404
    assert c.delete(f"/api/sessions/{sid}", headers=other).status_code == 404
    assert c.post("/api/chat", json={"question": "hi", "session_id": sid}, headers=other).status_code == 404
    assert c.get(f"/api/sessions/{sid}/messages", headers=vh(key, visitor="visitor-AAAA")).status_code == 200
    plain, _ = world.user(admin, "plain")
    assert c.get(f"/api/sessions/{sid}/messages", headers=plain).status_code == 404  # colleagues without support rights
    assert c.get(f"/api/sessions/{sid}/messages", headers=admin).status_code == 200   # support staff can read it


def test_support_staff_can_take_over_a_visitor_chat(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin)
    h = vh(key)
    sid = c.post("/api/sessions", json={}, headers=h).json()["id"]
    assert c.post(f"/api/sessions/{sid}/handoff", headers=h).status_code == 201
    queue = c.get("/api/handoffs", headers=admin).json()["items"]
    assert len(queue) == 1
    hid = queue[0]["id"]
    assert c.post(f"/api/handoffs/{hid}/reply", json={"text": "Hello visitor"}, headers=admin).status_code == 201
    msgs = c.get(f"/api/sessions/{sid}/messages", headers=h).json()
    assert msgs["items"][-1]["role"] == "human" and msgs["handoff_status"] == "active"


def test_visitor_can_confirm_an_action_in_their_own_chat_only(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin, agent="it")
    world.fake.tool_script = [[{"name": "create_ticket", "args": {"title": "Printer"}}]]
    ev = parse_sse(c.post("/api/chat/stream", json={"question": "open a ticket"}, headers=vh(key, visitor="visitor-AAAA")).text)
    action = next(d for k, d in ev if k == "tool")["result"]["action_id"]
    assert c.post(f"/api/actions/{action}/confirm", headers=vh(key, visitor="visitor-BBBB")).status_code == 404
    assert c.post(f"/api/actions/{action}/confirm", headers=vh(key, visitor="visitor-AAAA")).status_code == 200


# ------------------------------------------------------------------ origin / key checks
def test_requests_must_come_from_an_allowed_origin(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin, ["https://shop.example.com", "https://*.partner.org"])
    ok = lambda origin: c.get("/api/auth/me", headers=vh(key, origin)).status_code
    assert ok("https://shop.example.com") == 200
    assert ok("https://SHOP.example.com") == 200                       # case-insensitive
    assert ok("https://a.partner.org") == 200 and ok("https://a.b.partner.org") == 200
    for bad in ("https://evil.com", "http://shop.example.com", "https://shop.example.com.evil.com",
                "https://partner.org", "https://evilpartner.org", "https://shop.example.com:8443",
                "https://x.partner.org.evil.com", "null", None):
        assert ok(bad) == 403, bad
    assert c.get("/api/auth/me", headers=vh(key, "https://evil.com")).json()["error"]["code"] == "origin_not_allowed"


def test_wildcard_key_allows_any_site_including_no_origin(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin, ["*"])
    assert c.get("/api/auth/me", headers=vh(key, "https://anything.example")).status_code == 200
    assert c.get("/api/auth/me", headers=vh(key, None)).status_code == 200


def test_bad_disabled_and_wrong_org_keys(world):
    c = world.c
    org, admin = world.org("alpha")
    key = make_key(world, admin)
    assert c.get("/api/auth/me", headers=vh({"token": "pk_doesnotexist"})).status_code == 401
    for bad_id in ("short", "has spaces here", "x" * 65, "semi;colon-id", ""):
        assert c.get("/api/auth/me", headers=vh(key, visitor=bad_id)).status_code == 422, bad_id
    assert c.get("/api/auth/me", headers={"X-Embed-Key": key["token"], "Origin": SHOP}).status_code == 422  # no visitor id

    c.put(f"/api/embed-keys/{key['id']}", json={"active": False}, headers=admin)
    r = c.get("/api/auth/me", headers=vh(key))
    assert r.status_code == 403 and r.json()["error"]["code"] == "embed_disabled"
    c.put(f"/api/embed-keys/{key['id']}", json={"active": True}, headers=admin)
    assert c.get("/api/auth/me", headers=vh(key)).status_code == 200
    c.put(f"/api/orgs/{org['id']}", json={"active": False}, headers=world.root)
    assert c.get("/api/auth/me", headers=vh(key)).status_code == 403  # a disabled customer's widgets go dark too
    c.put(f"/api/orgs/{org['id']}", json={"active": True}, headers=world.root)
    admin = world.login("admin@alpha.test")  # disabling the organization signed everyone out
    c.delete(f"/api/embed-keys/{key['id']}", headers=admin)
    assert c.get("/api/auth/me", headers=vh(key)).status_code == 401


def test_a_key_only_reaches_its_own_organization(world):
    c = world.c
    (_, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    ready(c, admin_a, "paris.txt", PARIS)
    ready(c, admin_b, "bread.txt", BREAD)
    key_b = make_key(world, admin_b)
    r = c.post("/api/chat", json={"question": "Where is the Eiffel Tower?"}, headers=vh(key_b))
    assert r.json()["message"]["citations"] == []  # tenant B's widget cannot read tenant A's documents
    r = c.post("/api/chat", json={"question": "How do I bake sourdough bread starter?"}, headers=vh(key_b))
    assert r.json()["message"]["citations"]
    # an embed key is not a user credential: it is rejected in the Authorization header
    assert c.get("/api/documents", headers={"Authorization": f"Bearer {key_b['token']}"}).status_code == 401


def test_embed_key_cannot_be_used_to_switch_organizations(world):
    c = world.c
    (org_a, admin_a), (_, admin_b) = world.org("alpha"), world.org("beta")
    key_b = make_key(world, admin_b)
    h = {**vh(key_b), "X-Org-Id": org_a["id"]}
    assert c.get("/api/auth/me", headers=h).json()["org_id"] != org_a["id"]
    assert c.get("/api/embed/config", headers=admin_a).status_code == 404  # config is for visitors only


def test_embed_config_endpoint(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin, agent="hr", title="HR help", welcome="Ask me!", color="#aabbcc")
    cfg = c.get("/api/embed/config", headers=vh(key)).json()
    assert cfg == {"title": "HR help", "welcome": "Ask me!", "color": "#aabbcc", "agent_id": "hr"}


# ------------------------------------------------------------------ throttling
def test_visitors_are_throttled_individually(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True, admin_password="Sup3r-secret-pw!", rate_limit_per_minute=3) as c:
        w = World(c, (tmp_path / "admin_token.txt").read_text().strip())
        _, admin = w.org("alpha")
        key = make_key(w, admin)
        codes = [c.post("/api/chat", json={"question": "hi"}, headers=vh(key, visitor="visitor-AAAA")).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        assert c.post("/api/chat", json={"question": "hi"}, headers=vh(key, visitor="visitor-BBBB")).status_code == 200


def test_bad_embed_keys_count_as_failed_authentication(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True, admin_password="Sup3r-secret-pw!", auth_failures_per_minute=4) as c:
        codes = [c.get("/api/auth/me", headers=vh({"token": f"pk_guess{i}"})).status_code for i in range(7)]
        assert codes[:4] == [401] * 4 and set(codes[4:]) == {429}


# ------------------------------------------------------------------ CORS
def test_cors_follows_the_registered_origins(world):
    c = world.c
    _, admin = world.org("alpha")
    pre = lambda origin: c.options("/api/chat/stream", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-embed-key,x-visitor-id"})
    assert pre(SHOP).status_code == 403                                  # nothing registered yet
    key = make_key(world, admin, [SHOP, "https://*.example.org"])
    r = pre(SHOP)
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == SHOP
    assert "x-embed-key" in r.headers["access-control-allow-headers"]
    assert "POST" in r.headers["access-control-allow-methods"] and r.headers["vary"] == "Origin"
    assert pre("https://app.example.org").status_code == 204             # wildcard subdomain
    assert pre("https://evil.com").status_code == 403
    assert "access-control-allow-origin" not in pre("https://evil.com").headers
    assert pre("http://localhost:5173").status_code == 204               # the dev frontend stays allowed

    # real responses carry the header for allowed origins only
    ok = c.get("/api/health", headers={"Origin": SHOP})
    assert ok.headers["access-control-allow-origin"] == SHOP
    assert "access-control-allow-origin" not in c.get("/api/health", headers={"Origin": "https://evil.com"}).headers
    assert "access-control-allow-origin" not in c.get("/api/health").headers

    # removing the key (or deactivating it) removes the origin immediately
    c.put(f"/api/embed-keys/{key['id']}", json={"active": False}, headers=admin)
    assert pre(SHOP).status_code == 403
    c.put(f"/api/embed-keys/{key['id']}", json={"active": True}, headers=admin)
    assert pre(SHOP).status_code == 204
    c.delete(f"/api/embed-keys/{key['id']}", headers=admin)
    assert pre(SHOP).status_code == 403


def test_error_responses_also_carry_cors_headers(world):
    c = world.c
    _, admin = world.org("alpha")
    key = make_key(world, admin)
    r = c.post("/api/chat", content=b"x" * 400_000, headers={**vh(key), "content-type": "application/json"})
    assert r.status_code == 413 and r.headers["access-control-allow-origin"] == SHOP  # the widget can read the error


# ------------------------------------------------------------------ the widget script
def test_widget_script_is_isolated_and_safe(world):
    js = world.c.get("/widget.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    text = js.text
    for needle in ("attachShadow", "X-Embed-Key", "X-Visitor-Id", "/api/embed/config", "data-key", "/api/chat/stream"):
        assert needle in text, needle
    assert "innerHTML" not in text and "document.write" not in text and "eval(" not in text
    assert "data-api-key" not in text  # user API keys must never be put in web pages
    assert world.c.get("/widget-demo").status_code == 200


@pytest.mark.parametrize("path", ["/widget.js", "/widget-demo"])
def test_widget_assets_are_public_even_with_auth_on(world, path):
    assert world.c.get(path).status_code == 200
