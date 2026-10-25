import sys
import tempfile
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from probe1 import FakeOllama, client, parse_sse, report, up, wait_all  # noqa: E402

# 1. which document size breaks ingestion, and why
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = client(t, max_upload_mb=100)
    sv = c.sv
    import logging
    for mb in (3, 5, 6, 8):
        text = (b"The quick brown fox jumps over the lazy dog and keeps running far away. " * 200000)[: mb * 1_000_000]
        r = up(c, f"p{mb}.txt", text)
        doc = r.json()["results"][0]["document"]
        end = time.time() + 120
        while time.time() < end and c.get(f"/api/documents/{doc['id']}").json()["status"] == "processing":
            time.sleep(0.3)
        d = c.get(f"/api/documents/{doc['id']}").json()
        report("NOTE", f"{mb} MB plain text", f"status={d['status']} chunks={d['chunk_count']} err={d['error']}")
    # direct reproduction of the underlying exception
    import chromadb
    col = sv.vectors._col
    print("chroma max_batch_size =", sv.vectors._client.get_max_batch_size())
    try:
        n = sv.vectors._client.get_max_batch_size() + 1
        col.add(ids=[f"x{i}" for i in range(n)], embeddings=[[0.1] * 4] * n)
    except Exception as exc:  # noqa: BLE001
        print("direct add of batch+1 ->", type(exc).__name__, str(exc)[:200])
    c.__exit__(None, None, None)

# 2. poisoned history detail
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    fake = FakeOllama()
    c = client(t, fake)
    sid = c.post("/api/sessions", json={"agent_id": "hr"}).json()["id"]
    fake.tool_script = [[{"name": "request_leave", "args": "notadict"}]]
    ev = parse_sse(c.post("/api/chat/stream", json={"question": "go", "session_id": sid}).text)
    print("stream last event:", ev[-1][0], str(ev[-1][1])[:100])
    r = c.get(f"/api/sessions/{sid}/messages")
    print("GET messages:", r.status_code, r.text[:200])
    c.__exit__(None, None, None)

# 3. long hostname -> 500
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = client(t)
    for host in ["a" * 300 + ".com", "a" * 64 + ".com", "ex ample.com", "xn--" + "a" * 70 + ".com", "..", "a..b"]:
        r = c.post("/api/connectors", json={"type": "url", "ref": f"https://{host}/"})
        print(f"host {host[:20]!r}... ->", r.status_code, r.text[:100])
    c.__exit__(None, None, None)
