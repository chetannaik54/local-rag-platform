import functools
import http.server
import threading
import time
from datetime import date, timedelta

import pytest
from conftest import make_client, parse_sse, upload_ready

PARIS = b"The Eiffel Tower is located in Paris France. It was completed in 1889 for the World Fair."


def monday_in_future(days=40) -> date:
    d = date.today() + timedelta(days=days)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def new_session(client, agent):
    return client.post("/api/sessions", json={"agent_id": agent}).json()["id"]


def stream(client, sid, question, **kw):
    r = client.post("/api/chat/stream", json={"question": question, "session_id": sid, **kw}, **client.hdr)
    return parse_sse(r.text)


def confirm(client, tool_event, hdr=None):
    """Press 'Confirm' on the action card for a tool event; returns the action outcome."""
    assert tool_event["result"]["status"] == "awaiting_user_confirmation", tool_event
    r = client.post(f"/api/actions/{tool_event['result']['action_id']}/confirm", headers=hdr or {})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def client(tmp_path, fake):
    with make_client(tmp_path, fake) as c:
        c.sv = c.app.state.services
        c.hdr = {}
        yield c


# -- agents & tools ----------------------------------------------------------------
def test_builtin_agents_and_tool_catalog(client):
    body = client.get("/api/agents").json()
    assert {a["id"] for a in body["items"]} >= {"general", "hr", "it", "sales", "management"}
    assert "request_leave" in {t["name"] for t in body["available_tools"]}


def test_hr_agent_checks_leave_balance_via_tool(client, fake):
    sid = new_session(client, "hr")
    fake.tool_script = [[{"name": "get_leave_balance"}]]
    events = stream(client, sid, "How many leave days do I have?")
    kinds = [k for k, _ in events]
    assert "tool" in kinds and kinds[-1] == "done"
    tool = next(d for k, d in events if k == "tool")
    assert tool["ok"] and tool["result"]["remaining_days"] == 20
    assert fake.tool_specs and "get_leave_balance" in {t["function"]["name"] for t in fake.tool_specs}
    done = events[-1][1]["message"]
    assert done["tools"][0]["name"] == "get_leave_balance"
    assert "remaining_days" in done["content"]  # the fake echoes the tool result back


def test_request_leave_updates_balance_and_validates(client, fake):
    sid = new_session(client, "hr")
    start = monday_in_future()
    fake.tool_script = [[{"name": "request_leave", "args": {
        "start_date": start.isoformat(), "end_date": (start + timedelta(days=4)).isoformat()}}]]
    tool = next(d for k, d in stream(client, sid, "Book leave next month") if k == "tool")
    assert tool["ok"] and tool["result"]["status"] == "awaiting_user_confirmation"  # nothing is written yet
    assert client.sv.db.all("SELECT * FROM leave_requests") == []
    outcome = confirm(client, tool)
    assert outcome["ok"] and outcome["result"]["working_days"] == 5 and outcome["result"]["status"] == "pending"

    fake.tool_script = [[{"name": "get_leave_balance"}]]
    bal = next(d for k, d in stream(client, sid, "balance?") if k == "tool")
    assert bal["result"]["pending_days"] == 5 and bal["result"]["remaining_days"] == 15

    fake.tool_script = [[{"name": "request_leave", "args": {"start_date": "2020-01-06", "end_date": "2020-01-07"}}]]
    bad = next(d for k, d in stream(client, sid, "leave on 2020-01-06") if k == "tool")
    assert bad["ok"] is False and "past" in bad["result"]["error"]

    fake.tool_script = [[{"name": "request_leave", "args": {"start_date": "not-a-date", "end_date": "x"}}]]
    bad = next(d for k, d in stream(client, sid, "leave on 99/99") if k == "tool")
    assert bad["ok"] is False and "ISO date" in bad["result"]["error"]


def test_agent_cannot_use_tools_outside_its_set(client, fake):
    sid = new_session(client, "hr")
    fake.tool_script = [[{"name": "create_ticket", "args": {"title": "x"}}]]
    tool = next(d for k, d in stream(client, sid, "make a ticket") if k == "tool")
    assert tool["ok"] is False and "unavailable" in tool["result"]["error"]
    assert client.sv.db.all("SELECT * FROM tickets") == []


def test_it_agent_creates_and_looks_up_ticket(client, fake):
    sid = new_session(client, "it")
    fake.tool_script = [[{"name": "create_ticket", "args": {"title": "VPN down", "priority": "high"}}]]
    proposed = next(d for k, d in stream(client, sid, "VPN is down, open a ticket") if k == "tool")
    created = confirm(client, proposed)["result"]
    fake.tool_script = [[{"name": "get_ticket_status", "args": {"ticket_id": created["ticket_id"]}}]]
    status = next(d for k, d in stream(client, sid, "status?") if k == "tool")["result"]
    assert status["status"] == "open" and status["title"] == "VPN down"
    summary_sid = new_session(client, "management")  # auth is off, so the caller is the local admin
    fake.tool_script = [[{"name": "business_summary"}]]
    summary = next(d for k, d in stream(client, summary_sid, "how are we doing?") if k == "tool")["result"]
    assert summary["open_tickets"] == 1


def test_agent_without_docs_or_tools_gives_unknown_answer(client, fake):
    r = client.post("/api/chat", json={"question": "anything", "agent_id": "general"})
    assert "do not provide enough information" in r.json()["message"]["content"]
    assert fake.chat_calls == []


def test_tool_agent_can_act_without_documents(client, fake):
    fake.tool_script = [[{"name": "get_leave_balance"}]]
    sid = new_session(client, "hr")
    events = stream(client, sid, "my balance")
    assert events[-1][0] == "done" and any(k == "tool" for k, _ in events)


def test_custom_agent_scoped_to_documents(client):
    paris = upload_ready(client, "paris.txt", PARIS)
    bread = upload_ready(client, "bread.txt", b"Sourdough bread needs flour water salt and starter. Proof overnight.")
    r = client.post("/api/agents", json={"name": "Bread bot", "system_prompt": "Talk bread.",
                                        "tools": [], "document_ids": [bread["id"]]})
    assert r.status_code == 201
    agent_id = r.json()["id"]
    msg = client.post("/api/chat", json={"question": "Where is the Eiffel Tower?", "agent_id": agent_id}).json()["message"]
    assert msg["citations"] == []  # paris doc is outside this agent's scope
    msg = client.post("/api/chat", json={"question": "How do I bake sourdough bread?", "agent_id": agent_id}).json()["message"]
    assert msg["citations"] and msg["citations"][0]["document_id"] == bread["id"]
    assert paris["id"] != bread["id"]
    assert client.post("/api/agents", json={"name": "x", "system_prompt": "y", "tools": ["nope"]}).status_code == 422
    assert client.delete("/api/agents/hr").status_code == 404  # built-ins are protected
    assert client.delete(f"/api/agents/{agent_id}").status_code == 204


def test_response_language_directive(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    client.post("/api/chat", json={"question": "Where is the Eiffel Tower?"})
    assert "same language as the user" in fake.chat_calls[-1][0]["content"]
    cur = client.get("/api/settings").json()["settings"]
    client.put("/api/settings", json={**cur, "response_language": "French"})
    client.post("/api/chat", json={"question": "Where is the Eiffel Tower?"})
    assert "Always reply in French" in fake.chat_calls[-1][0]["content"]


# -- handoff ------------------------------------------------------------------------
def test_handoff_full_flow(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    sid = new_session(client, "general")
    h = client.post(f"/api/sessions/{sid}/handoff", json={"reason": "wants a person"})
    assert h.status_code == 201 and h.json()["status"] == "waiting"
    assert client.get("/api/sessions").json()["items"][0]["handoff_status"] == "waiting"

    fake.chat_calls.clear()
    events = stream(client, sid, "Are you there?")
    assert [k for k, _ in events] == ["session", "handoff", "done"]
    assert fake.chat_calls == []  # the model stays out of a handed-off chat

    queue = client.get("/api/handoffs").json()["items"]
    assert queue[0]["last_user_message"] == "Are you there?"
    hid = queue[0]["id"]
    assert client.post(f"/api/handoffs/{hid}/claim").json()["status"] == "active"
    assert client.post(f"/api/handoffs/{hid}/claim").status_code == 409

    p = client.post("/api/handoffs/paraphrase", json={"text": "give me a sec", "tone": "friendly"})
    assert p.status_code == 200 and p.json()["text"]
    assert client.post(f"/api/handoffs/{hid}/reply", json={"text": "Hi, I'm Sam."}).status_code == 201
    msgs = client.get(f"/api/sessions/{sid}/messages").json()
    assert msgs["handoff_status"] == "active"
    assert msgs["items"][-1]["role"] == "human" and msgs["items"][-1]["content"] == "Hi, I'm Sam."

    assert client.post(f"/api/handoffs/{hid}/resolve").json()["status"] == "resolved"
    assert client.get("/api/handoffs").json()["items"] == []
    r = client.post("/api/chat", json={"question": "Where is the Eiffel Tower?", "session_id": sid})
    assert r.json()["message"]["citations"]  # bot is back
    assert client.post(f"/api/handoffs/{hid}/reply", json={"text": "late"}).status_code == 409


def test_model_can_escalate_to_human(client, fake):
    sid = new_session(client, "it")
    fake.tool_script = [[{"name": "request_human_agent", "args": {"reason": "user asked"}}]]
    events = stream(client, sid, "let me talk to a person")
    assert ("handoff", {"status": "waiting"}) in events
    assert len(client.get("/api/handoffs").json()["items"]) == 1


def test_user_can_cancel_handoff_and_canned_responses(client):
    sid = new_session(client, "general")
    client.post(f"/api/sessions/{sid}/handoff")
    client.post(f"/api/sessions/{sid}/handoff")  # idempotent
    assert len(client.get("/api/handoffs").json()["items"]) == 1
    assert client.delete(f"/api/sessions/{sid}/handoff").status_code == 204
    assert client.get("/api/handoffs").json()["items"] == []
    assert len(client.get("/api/canned").json()["items"]) >= 3
    c = client.post("/api/canned", json={"title": "Thanks", "text": "Thanks for waiting."}).json()
    assert client.delete(f"/api/canned/{c['id']}").status_code == 204


# -- analytics & feedback -------------------------------------------------------------
def test_analytics_and_feedback(client):
    upload_ready(client, "paris.txt", PARIS)
    ok = client.post("/api/chat", json={"question": "Where is the Eiffel Tower?"}).json()
    client.post("/api/chat", json={"question": "zxqv blorptastic quantum yoghurt?"})
    mid = ok["message"]["id"]
    assert client.post(f"/api/messages/{mid}/feedback", json={"rating": 1}).status_code == 204
    assert client.post(f"/api/messages/{mid}/feedback", json={"rating": 5}).status_code == 422
    assert client.post("/api/messages/nope/feedback", json={"rating": 1}).status_code == 404
    msgs = client.get(f"/api/sessions/{ok['session_id']}/messages").json()["items"]
    assert [m["feedback"] for m in msgs if m["role"] == "assistant"] == [1]

    a = client.get("/api/analytics?days=7").json()
    assert a["total_questions"] == 2 and a["answered"] == 1 and a["answer_rate"] == 0.5
    assert a["unanswered"][0]["question"].startswith("zxqv")
    assert a["thumbs_up"] == 1 and a["top_documents"][0]["filename"] == "paris.txt"
    assert a["avg_latency_ms"] is not None and a["per_agent"]["general"] == 2


# -- connectors ---------------------------------------------------------------------------
def wait_connector(client, cid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        c = next(x for x in client.get("/api/connectors").json()["items"] if x["id"] == cid)
        if c["last_status"]:
            return c
        time.sleep(0.1)
    raise AssertionError("connector did not sync")


def wait_docs_ready(client, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        docs = client.get("/api/documents").json()["items"]
        if docs and all(d["status"] != "processing" for d in docs):
            return docs
        time.sleep(0.1)
    raise AssertionError("documents still processing")


def test_folder_connector_sync_update_and_remove(tmp_path, fake):
    root = tmp_path / "sources"
    folder = root / "kb"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("Alpha document about quantum widgets and gears.")
    (folder / "b.md").write_text("# Beta\nBeta document about sourdough bread starter.")
    with make_client(tmp_path / "d", fake, connector_roots=str(root)) as client:
        client.hdr = {}
        cid = client.post("/api/connectors", json={"type": "folder", "ref": str(folder)}).json()["id"]
        c = wait_connector(client, cid)
        assert c["last_status"].startswith("ok") and "2 added" in c["last_status"]
        docs = wait_docs_ready(client)
        assert {d["filename"] for d in docs} == {"a.txt", "b.md"} and all(d["source_type"] == "folder" for d in docs)

        (folder / "a.txt").write_text("Alpha document REWRITTEN about turbines.")
        (folder / "b.md").unlink()
        client.post(f"/api/connectors/{cid}/sync")
        deadline = time.time() + 20
        while time.time() < deadline:
            names = {d["filename"] for d in client.get("/api/documents").json()["items"]}
            if names == {"a.txt"}:
                break
            time.sleep(0.2)
        assert names == {"a.txt"}
        wait_docs_ready(client)
        ans = client.post("/api/chat", json={"question": "Tell me about turbines"}).json()["message"]
        assert ans["citations"] and "turbines" in ans["citations"][0]["excerpt"]

        assert client.delete(f"/api/connectors/{cid}").status_code == 204
        assert client.get("/api/documents").json()["total"] == 0


def test_connector_rejects_paths_outside_roots_and_private_urls(tmp_path, fake):
    root = tmp_path / "sources"
    root.mkdir()
    with make_client(tmp_path / "d", fake, connector_roots=str(root)) as client:
        r = client.post("/api/connectors", json={"type": "folder", "ref": str(tmp_path)})
        assert r.status_code == 403 and r.json()["error"]["code"] == "path_not_allowed"
        r = client.post("/api/connectors", json={"type": "folder", "ref": str(root / ".." / "..")})
        assert r.status_code == 403
        r = client.post("/api/connectors", json={"type": "url", "ref": "http://127.0.0.1:9/x"})
        assert r.status_code == 403 and "private" in r.json()["error"]["message"]
        r = client.post("/api/connectors", json={"type": "url", "ref": "file:///etc/passwd"})
        assert r.status_code == 422


def test_url_connector_ingests_html_text(tmp_path, fake):
    site = tmp_path / "site"
    site.mkdir()
    (site / "page.html").write_text(
        "<html><head><title>Warranty Policy</title><script>evil()</script></head>"
        "<body><nav>MENU</nav><p>Our warranty covers manufacturing defects for twelve months.</p></body></html>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with make_client(tmp_path / "d", fake, allow_private_urls=True) as client:
            client.hdr = {}
            url = f"http://127.0.0.1:{srv.server_port}/page.html"
            cid = client.post("/api/connectors", json={"type": "url", "ref": url}).json()["id"]
            assert wait_connector(client, cid)["last_status"].startswith("ok")
            doc = wait_docs_ready(client)[0]
            assert doc["source_type"] == "url" and doc["status"] == "ready" and doc["filename"].startswith("Warranty")
            ans = client.post("/api/chat", json={"question": "What does the warranty cover?"}).json()["message"]
            text = ans["citations"][0]["excerpt"]
            assert "manufacturing defects" in text and "evil()" not in text and "MENU" not in text
    finally:
        srv.shutdown()


# -- auth & roles ---------------------------------------------------------------------------
def test_auth_roles_and_session_isolation(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True) as c:
        assert c.get("/api/health").status_code == 200  # health stays open
        assert c.get("/api/documents").status_code == 401
        token = (tmp_path / "admin_token.txt").read_text().strip()
        admin = {"Authorization": f"Bearer {token}"}
        me = c.get("/api/auth/me", headers=admin).json()
        assert (me["name"], me["role"], me["org_id"], me["auth_enabled"]) == ("admin", "super_admin", "default", True)
        assert "platform.manage" in me["permissions"]
        assert c.get("/api/documents", headers={"Authorization": "Bearer wrong"}).status_code == 401

        mk = lambda name, role: c.post("/api/users", json={"name": name, "role": role}, headers=admin).json()["token"]
        alice, bob, sam = mk("alice", "user"), mk("bob", "user"), mk("sam", "agent")
        a, b, s = ({"X-API-Key": t} for t in (alice, bob, sam))

        assert c.post("/api/documents/upload", files=[("files", ("x.txt", PARIS))], headers=a).status_code == 403
        current = c.get("/api/settings", headers=admin).json()["settings"]
        assert c.get("/api/settings", headers=a).status_code == 403  # plain members cannot even read tuning settings
        assert c.put("/api/settings", json=current, headers=a).status_code == 403
        assert c.get("/api/analytics", headers=s).status_code == 403
        assert c.get("/api/users", headers=a).status_code == 403
        assert c.get("/api/handoffs", headers=a).status_code == 403

        sid = c.post("/api/sessions", json={}, headers=a).json()["id"]
        assert c.get(f"/api/sessions/{sid}/messages", headers=b).status_code == 404  # no existence leak
        assert c.get("/api/sessions", headers=b).json()["total"] == 0
        assert c.get(f"/api/sessions/{sid}/messages", headers=s).status_code == 200  # agents may read
        assert c.delete(f"/api/sessions/{sid}", headers=b).status_code == 404

        c.post(f"/api/sessions/{sid}/handoff", headers=a)
        assert len(c.get("/api/handoffs", headers=s).json()["items"]) == 1
        assert c.delete(f"/api/users/{c.get('/api/users', headers=admin).json()['items'][0]['id']}", headers=admin).status_code == 409
        assert c.post("/api/users", json={"name": "alice", "role": "user"}, headers=admin).status_code == 409


def test_fixed_admin_token_from_config(tmp_path, fake):
    with make_client(tmp_path, fake, auth_enabled=True, admin_token="my-secret-token") as c:
        assert c.get("/api/users", headers={"Authorization": "Bearer my-secret-token"}).status_code == 200
        assert not (tmp_path / "admin_token.txt").exists()


# -- widget & voice ------------------------------------------------------------------------------
def test_widget_assets_served(client):
    js = client.get("/widget.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "/api/chat/stream" in js.text and "innerHTML" not in js.text  # no HTML injection sink
    assert client.get("/widget-demo").status_code == 200


def test_widget_origin_allowed_by_cors(tmp_path, fake):
    with make_client(tmp_path, fake, widget_origins="https://shop.example.com") as c:
        r = c.options("/api/chat/stream", headers={"Origin": "https://shop.example.com",
                                                   "Access-Control-Request-Method": "POST"})
        assert r.headers.get("access-control-allow-origin") == "https://shop.example.com"
        r = c.options("/api/chat/stream", headers={"Origin": "https://evil.example.com",
                                                   "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in r.headers


def test_voice_endpoint_validation(client):
    assert "stt_available" in client.get("/api/voice/status").json()
    r = client.post("/api/voice/transcribe", files={"audio": ("a.webm", b"")})
    assert r.status_code == 422

