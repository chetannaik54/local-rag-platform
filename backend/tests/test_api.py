from conftest import parse_sse, upload, upload_ready

PARIS = b"The Eiffel Tower is located in Paris France. It was completed in 1889 for the World Fair."
COOK = b"To bake sourdough bread you need flour water salt and an active starter. Proof overnight."


def test_health_ok_and_degraded(client, fake):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["ollama"]["reachable"]
    fake.models = ["other:1b"]
    h = client.get("/api/health").json()
    assert h["status"] == "degraded"
    assert "ollama pull llama3.1:8b" in h["ollama"]["error"]
    fake.down = True
    h = client.get("/api/health").json()
    assert h["ollama"]["reachable"] is False


def test_models_unreachable_gives_503(client, fake):
    fake.down = True
    r = client.get("/api/models")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ollama_unreachable"


def test_upload_index_and_list(client):
    doc = upload_ready(client, "paris.txt", PARIS)
    assert doc["status"] == "ready" and doc["chunk_count"] >= 1
    assert doc["embedding_model"] == "nomic-embed-text"
    assert client.sv.vectors.count(doc["id"]) == doc["chunk_count"]
    lst = client.get("/api/documents").json()
    assert lst["total"] == 1 and lst["items"][0]["filename"] == "paris.txt"
    assert client.get(f"/api/documents/{doc['id']}").json()["id"] == doc["id"]


def test_duplicate_upload_is_not_reingested(client):
    first = upload_ready(client, "paris.txt", PARIS)
    r = upload(client, "copy.txt", PARIS).json()["results"][0]
    assert r["duplicate"] is True and r["document"]["id"] == first["id"]
    assert client.get("/api/documents").json()["total"] == 1


def test_invalid_uploads(client):
    r = upload(client, "evil.exe", b"MZ....")
    assert r.status_code == 400 and "Unsupported" in r.json()["results"][0]["error"]
    r = upload(client, "empty.txt", b"")
    assert r.status_code == 400 and "empty" in r.json()["results"][0]["error"].lower()
    r = upload(client, "bad.pdf", b"this is not a pdf")
    assert r.status_code == 400
    r = upload(client, "../../traversal.txt", PARIS)
    assert r.json()["results"][0]["document"]["filename"] == "traversal.txt"


def test_oversized_upload(client):
    client.sv.ingestion._max = 100
    r = upload(client, "big.txt", b"x" * 500)
    assert r.status_code == 400 and "limit" in r.json()["results"][0]["error"]


def test_unextractable_file_marked_failed(client):
    doc = upload_ready(client, "blank.txt", b"   \n\n  ")
    assert doc["status"] == "failed" and "No extractable text" in doc["error"]


def test_delete_document_removes_vectors_and_file(client):
    doc = upload_ready(client, "paris.txt", PARIS)
    other = upload_ready(client, "bread.txt", COOK)
    from pathlib import Path

    stored = Path(client.sv.db.get_document(doc["id"])["stored_path"])
    assert stored.exists()
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 204
    assert client.sv.vectors.count(doc["id"]) == 0
    assert client.sv.vectors.count(other["id"]) > 0
    assert not stored.exists()
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 404


def test_chat_returns_cited_answer(client, fake):
    paris = upload_ready(client, "paris.txt", PARIS)
    upload_ready(client, "bread.txt", COOK)
    r = client.post("/api/chat", json={"question": "Where is the Eiffel Tower located?"})
    assert r.status_code == 200
    body = r.json()
    cites = body["message"]["citations"]
    assert cites and cites[0]["document_id"] == paris["id"]
    assert cites[0]["filename"] == "paris.txt"
    assert "Eiffel Tower" in cites[0]["excerpt"]
    assert cites[0]["cited"] is True  # fake model answered with [1]
    # the bread document must not be retrieved for this question
    assert all(c["filename"] == "paris.txt" for c in cites)
    # system prompt + untrusted-context framing reached the model
    sent = fake.chat_calls[-1]
    assert sent[0]["role"] == "system" and "untrusted" in sent[0]["content"]
    assert '<source id="1" file="paris.txt"' in sent[-1]["content"]


def test_citation_numbers_are_validated(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    fake.reply = "Claims [1] and a made-up one [7]."
    cites = client.post("/api/chat", json={"question": "Where is the Eiffel Tower?"}).json()["message"]["citations"]
    assert [c["n"] for c in cites] == [1] and cites[0]["cited"] is True


def test_unknown_answer_when_no_evidence(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    r = client.post("/api/chat", json={"question": "zxqv blorptastic quantum yoghurt?"})
    msg = r.json()["message"]
    assert "do not provide enough information" in msg["content"]
    assert msg["citations"] == []
    assert fake.chat_calls == []  # the LLM is never asked to guess


def test_no_documents_gives_unknown_answer(client):
    r = client.post("/api/chat", json={"question": "anything"})
    assert "do not provide enough information" in r.json()["message"]["content"]


def test_document_filter(client):
    upload_ready(client, "paris.txt", PARIS)
    bread = upload_ready(client, "bread.txt", COOK)
    r = client.post("/api/chat", json={"question": "Where is the Eiffel Tower?", "document_ids": [bread["id"]]})
    assert r.json()["message"]["citations"] == []


def test_stream_events_and_persistence(client):
    paris = upload_ready(client, "paris.txt", PARIS)
    r = client.post("/api/chat/stream", json={"question": "Where is the Eiffel Tower located?"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "session" and kinds[1] == "sources" and kinds[-1] == "done"
    assert "token" in kinds
    sid = events[0][1]["session_id"]
    text = "".join(d["text"] for k, d in events if k == "token")
    assert text == events[-1][1]["message"]["content"]
    assert events[1][1]["citations"][0]["document_id"] == paris["id"]
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_stream_reports_missing_model_as_error_event(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    fake.models = ["nomic-embed-text"]
    events = parse_sse(client.post("/api/chat/stream", json={"question": "Eiffel Tower?"}).text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "model_missing" and "ollama pull llama3.1:8b" in events[-1][1]["message"]


def test_chat_missing_model_http_status(client, fake):
    fake.models = []
    r = client.post("/api/chat", json={"question": "hi"})
    assert r.status_code == 424 and r.json()["error"]["code"] == "model_missing"


def test_followup_is_rewritten_with_history(client, fake):
    upload_ready(client, "paris.txt", PARIS)
    sid = client.post("/api/sessions").json()["id"]
    client.post("/api/chat", json={"question": "Where is the Eiffel Tower?", "session_id": sid})
    fake.chat_calls.clear()
    client.post("/api/chat", json={"question": "When was it completed?", "session_id": sid})
    rewrite, answer = fake.chat_calls
    assert "Conversation:" in rewrite[-1]["content"]
    assert any("Eiffel" in m["content"] for m in answer if m["role"] == "assistant" or m["role"] == "user")


def test_validation_errors(client):
    r = client.post("/api/chat", json={"question": "   "})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert client.post("/api/chat", json={}).status_code == 422
    assert client.post("/api/chat", json={"question": "x", "session_id": "nope"}).status_code == 404
    assert client.get("/api/documents?limit=0").status_code == 422


def test_sessions_crud_and_pagination(client):
    ids = [client.post("/api/sessions", json={"title": f"s{i}"}).json()["id"] for i in range(3)]
    page = client.get("/api/sessions?limit=2").json()
    assert page["total"] == 3 and len(page["items"]) == 2
    client.post("/api/chat", json={"question": "hello", "session_id": ids[0]})
    assert len(client.get(f"/api/sessions/{ids[0]}/messages").json()["items"]) == 2
    assert client.delete(f"/api/sessions/{ids[0]}/messages").status_code == 204
    assert client.get(f"/api/sessions/{ids[0]}/messages").json()["items"] == []
    assert client.delete(f"/api/sessions/{ids[1]}").status_code == 204
    assert client.get(f"/api/sessions/{ids[1]}/messages").status_code == 404


def test_settings_roundtrip_validation_and_stale_docs(client):
    cur = client.get("/api/settings").json()["settings"]
    assert client.put("/api/settings", json={**cur, "chunk_overlap": 900}).status_code == 422
    assert client.put("/api/settings", json={**cur, "ollama_url": "ftp://x"}).status_code == 422
    upload_ready(client, "paris.txt", PARIS)
    new = {**cur, "top_k": 3, "embedding_model": "other-embed"}
    r = client.put("/api/settings", json=new).json()
    assert r["settings"]["top_k"] == 3
    assert r["validation"]["embedding_model_available"] is False
    assert r["reindex_required"] is True and r["stale_documents"] == 1
    assert client.get("/api/settings").json()["settings"]["embedding_model"] == "other-embed"


def test_vectors_persist_across_restart(tmp_path, fake):
    from fastapi.testclient import TestClient

    from app.core.config import AppConfig
    from app.main import create_app

    cfg = AppConfig(data_dir=tmp_path, similarity_threshold=0.25, _env_file=None)
    with TestClient(create_app(cfg, ollama=fake)) as c:
        doc = upload_ready(c, "paris.txt", PARIS)
    with TestClient(create_app(cfg, ollama=fake)) as c:
        assert c.app.state.services.vectors.count(doc["id"]) == doc["chunk_count"]
        r = c.post("/api/chat", json={"question": "Where is the Eiffel Tower?"})
        assert r.json()["message"]["citations"]
