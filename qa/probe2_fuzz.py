"""Adversarial probes, batch 2: auth, SSRF, fuzzing, load, large inputs, streaming, context budget."""
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from probe1 import PARIS, FakeOllama, client, parse_sse, report, up, wait_all  # noqa: E402

TD = dict(ignore_cleanup_errors=True)


def poisoned_history_and_loop():
    with tempfile.TemporaryDirectory(**TD) as t:
        fake = FakeOllama()
        c = client(t, fake)
        sid = c.post("/api/sessions", json={"agent_id": "hr"}).json()["id"]
        fake.tool_script = [[{"name": "request_leave", "args": "notadict"}]]
        c.post("/api/chat/stream", json={"question": "go", "session_id": sid})
        r = c.get(f"/api/sessions/{sid}/messages")
        report("BUG" if r.status_code >= 500 else "OK", "session history after a model emitted non-dict tool args",
               f"GET messages -> HTTP {r.status_code} (chat is permanently unreadable if 500)")
        r = c.post("/api/chat", json={"question": "continue", "session_id": sid})
        report("BUG" if r.status_code >= 500 else "OK", "send another message in that session", f"HTTP {r.status_code}")

        # a model that ALWAYS asks for a tool
        class Loop(FakeOllama):
            n = 0

            async def chat_stream_tools(self, model, messages, tools, options):
                Loop.n += 1
                yield {"tool_calls": [{"id": f"c{Loop.n}", "name": "get_leave_balance", "args": {}}]}
        c.__exit__(None, None, None)
    with tempfile.TemporaryDirectory(**TD) as t:
        c = client(t, Loop())
        sid = c.post("/api/sessions", json={"agent_id": "hr"}).json()["id"]
        r = c.post("/api/chat/stream", json={"question": "loop", "session_id": sid})
        ev = parse_sse(r.text)
        calls = sum(1 for k, _ in ev if k == "tool")
        report("OK" if calls <= 5 else "BUG", "always-tool-calling model is stopped", f"{calls} tool calls, last event={ev[-1][0]}")
        c.__exit__(None, None, None)


def auth():
    with tempfile.TemporaryDirectory(**TD) as t:
        c = client(t, auth_enabled=True)
        tok = (Path(t) / "admin_token.txt").read_text().strip()
        adm = {"Authorization": f"Bearer {tok}"}
        # token file ACL
        out = subprocess.run(["icacls", str(Path(t) / "admin_token.txt")], capture_output=True, text=True).stdout
        broad = [x for x in ("Everyone", "BUILTIN\\Users", "Authenticated Users") if x in out]
        report("BUG" if broad else "OK", "admin_token.txt readable by other local accounts", f"broad ACL entries: {broad}")
        # unauthenticated surface
        for path in ["/docs", "/openapi.json", "/api/health", "/widget.js", "/widget-demo", "/api/settings", "/api/agents", "/api/documents"]:
            code = c.get(path).status_code
            if code == 200 and path in ("/docs", "/openapi.json", "/api/health"):
                report("NOTE", f"unauthenticated GET {path}", "200 (exposes API schema / Ollama URL & model names)" if path != "/docs" else "200")
            elif code == 200 and path in ("/api/settings", "/api/agents", "/api/documents"):
                report("BUG", f"unauthenticated GET {path}", "200")
        # token handling
        for h in [{"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": "bearer " + tok.upper()},
                  {"X-API-Key": tok + " "}, {"Authorization": "Bearer " + "A" * 100000}, {"X-API-Key": "é" * 10}]:
            try:
                code = c.get("/api/auth/me", headers=h).status_code
            except Exception as exc:  # noqa: BLE001
                code = f"EXC {type(exc).__name__}"
            report("OK" if code in (401, 200, 431) else "BUG", f"auth header variant {str(h)[:50]}", str(code))
        # brute force: no lockout / rate limit
        t0 = time.time()
        codes = [c.get("/api/auth/me", headers={"Authorization": f"Bearer guess{i}"}).status_code for i in range(300)]
        report("BUG" if set(codes) == {401} else "OK", "300 wrong-token attempts in a row", f"all 401, no lockout/rate limit ({time.time()-t0:.1f}s)")
        # role matrix for lowest role
        u = c.post("/api/users", json={"name": "bob", "role": "user"}, headers=adm).json()["token"]
        ub = {"X-API-Key": u}
        a = c.post("/api/users", json={"name": "amy", "role": "agent"}, headers=adm).json()["token"]
        ab = {"X-API-Key": a}
        matrix = [
            ("GET", "/api/models"), ("PUT", "/api/settings"), ("POST", "/api/documents/upload"), ("DELETE", "/api/documents/x"),
            ("GET", "/api/analytics"), ("GET", "/api/users"), ("POST", "/api/users"), ("GET", "/api/connectors"),
            ("POST", "/api/connectors"), ("POST", "/api/agents"), ("PUT", "/api/agents/hr"), ("DELETE", "/api/agents/hr"),
            ("GET", "/api/handoffs"), ("POST", "/api/handoffs/paraphrase"), ("GET", "/api/canned"), ("POST", "/api/canned"),
        ]
        leaks = []
        for m, p in matrix:
            r = c.request(m, p, headers=ub, json={})
            if r.status_code not in (401, 403, 422):
                leaks.append((m, p, r.status_code))
        report("BUG" if leaks else "OK", "role 'user' blocked on admin/agent endpoints", str(leaks))
        leaks = []
        for m, p in matrix:
            if (m, p) in (("GET", "/api/handoffs"), ("POST", "/api/handoffs/paraphrase"), ("GET", "/api/canned"), ("POST", "/api/canned")):
                continue
            r = c.request(m, p, headers=ab, json={})
            if r.status_code not in (401, 403, 422):
                leaks.append((m, p, r.status_code))
        report("BUG" if leaks else "OK", "role 'agent' blocked on admin endpoints", str(leaks))
        # information exposed to the lowest role
        ag = c.get("/api/agents", headers=ub).json()["items"]
        report("BUG" if any("system_prompt" in x for x in ag) else "OK", "role 'user' can read every assistant's system prompt & document scope",
               f"{len(ag)} agents incl. prompts")
        st = c.get("/api/settings", headers=ub).json()["settings"]
        report("NOTE", "role 'user' can read Ollama URL & model settings", st["ollama_url"])
        # privilege use through assistants: user asks Management assistant for business summary
        sid = c.post("/api/sessions", json={"agent_id": "management"}, headers=ub).json()["id"]
        c.app.state.services.ollama.tool_script = [[{"name": "business_summary"}]]
        ev = parse_sse(c.post("/api/chat/stream", json={"question": "numbers?", "session_id": sid}, headers=ub).text)
        tool = next((d for k, d in ev if k == "tool"), None)
        report("BUG" if tool and tool["ok"] else "OK", "low-privilege user runs admin-level 'business_summary' via the Management assistant",
               str(tool["result"] if tool else None)[:150])
        # token in query string / user enumeration
        r1 = c.post("/api/users", json={"name": "bob", "role": "user"}, headers=adm)
        report("NOTE", "duplicate user name reveals existence", str(r1.status_code))
        # admin can delete last other admin and demote? delete self blocked; delete all others then lockout?
        me = c.get("/api/users", headers=adm).json()["items"]
        # create 2nd admin, delete first via 2nd -> lockout of bootstrap file token is fine; check zero-admin state
        t2 = c.post("/api/users", json={"name": "root2", "role": "admin"}, headers=adm).json()
        r = c.delete(f"/api/users/{[x for x in me if x['name']=='admin'][0]['id']}", headers={"X-API-Key": t2["token"]})
        left = [x["name"] for x in c.get("/api/users", headers={"X-API-Key": t2["token"]}).json()["items"] if x["role"] == "admin"]
        report("NOTE", "admin deletes the bootstrap admin", f"HTTP {r.status_code}, admins left={left}")
        # bootstrap re-creation? restart with users present keeps file stale
        c.__exit__(None, None, None)
        c2 = client(t, auth_enabled=True)
        r = c2.get("/api/auth/me", headers=adm)
        report("OK" if r.status_code == 401 else "BUG", "deleted admin's token is dead after restart", str(r.status_code))
        c2.__exit__(None, None, None)

        # session isolation matrix
    with tempfile.TemporaryDirectory(**TD) as t:
        c = client(t, auth_enabled=True)
        adm = {"Authorization": "Bearer " + (Path(t) / "admin_token.txt").read_text().strip()}
        a = {"X-API-Key": c.post("/api/users", json={"name": "alice", "role": "user"}, headers=adm).json()["token"]}
        b = {"X-API-Key": c.post("/api/users", json={"name": "bob", "role": "user"}, headers=adm).json()["token"]}
        sid = c.post("/api/sessions", json={}, headers=a).json()["id"]
        c.post("/api/chat", json={"question": "my secret plan", "session_id": sid}, headers=a)
        mid = c.get(f"/api/sessions/{sid}/messages", headers=a).json()["items"][-1]["id"]
        probes = [("GET", f"/api/sessions/{sid}/messages"), ("DELETE", f"/api/sessions/{sid}"), ("DELETE", f"/api/sessions/{sid}/messages"),
                  ("POST", f"/api/sessions/{sid}/handoff"), ("DELETE", f"/api/sessions/{sid}/handoff")]
        bad = [(m, p) for m, p in probes if c.request(m, p, headers=b, json={}).status_code not in (404,)]
        r = c.post("/api/chat", json={"question": "hi", "session_id": sid}, headers=b)
        bad += [("POST", "/api/chat")] if r.status_code != 404 else []
        r = c.post(f"/api/messages/{mid}/feedback", json={"rating": 1}, headers=b)
        bad += [("POST", "feedback")] if r.status_code != 404 else []
        report("BUG" if bad else "OK", "bob cannot read/modify/delete alice's session", str(bad))
        c.__exit__(None, None, None)


def ssrf():
    with tempfile.TemporaryDirectory(**TD) as t:
        root = Path(t) / "src"
        root.mkdir()
        c = client(t + "/d", connector_roots=str(root))
        urls = ["http://127.0.0.1/", "http://localhost/", "http://localhost./", "http://[::1]/", "http://0.0.0.0/", "http://2130706433/",
                "http://0x7f.0.0.1/", "http://127.1/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://192.168.1.1/",
                "http://[::ffff:127.0.0.1]/", "http://user:pass@127.0.0.1/", "http://127.0.0.1.nip.io/", "http://metadata.google.internal/",
                "gopher://127.0.0.1/", "ftp://127.0.0.1/", "file:///C:/Windows/win.ini", "//127.0.0.1/", "http://",
                "https://" + "a" * 300 + ".com/"]
        for u in urls:
            r = c.post("/api/connectors", json={"type": "url", "ref": u})
            if r.status_code in (200, 201):
                report("BUG", f"SSRF URL accepted: {u[:50]}", str(r.status_code))
            elif r.status_code >= 500:
                report("BUG", f"URL connector 5xx for {u[:50]}", r.text[:100])
        report("NOTE", "SSRF URL list evaluated", f"{len(urls)} variants")
        folders = [str(root / ".." ), str(Path(t)), "C:\\", "C:\\Windows", "\\\\localhost\\c$", "\\\\?\\C:\\Windows", str(root) + "\\..\\d",
                   "~", "", "..\\..", str(root / "nonexistent"), "con", "nul:", "C:\\Users\\cheta\\.ssh"]
        for f in folders:
            r = c.post("/api/connectors", json={"type": "folder", "ref": f or " "})
            if r.status_code in (200, 201):
                report("BUG", f"folder connector accepted outside root: {f!r}", str(r.status_code))
            elif r.status_code >= 500:
                report("BUG", f"folder connector 5xx for {f!r}", r.text[:100])
        # junction escape: junction inside root pointing outside
        secret = Path(t) / "secret"
        secret.mkdir()
        (secret / "private.txt").write_text("TOP SECRET CONTENT outside the allowed root")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(secret)], capture_output=True)
        (root / "ok.txt").write_text("regular file inside root")
        r = c.post("/api/connectors", json={"type": "folder", "ref": str(root)})
        time.sleep(4)
        names = [d["filename"] for d in wait_all(c)]
        report("BUG" if "private.txt" in names else "OK", "directory junction inside root pointing outside", f"indexed files: {names}")
        c.__exit__(None, None, None)


METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]
JUNK = ["", " ", "0", "-1", "9" * 30, "null", "true", "../../x", "%00", "\x00", "a" * 5000, "<script>", "'; --", "😀", "NaN", "1e999"]


def fuzz():
    with tempfile.TemporaryDirectory(**TD) as t:
        c = client(t)
        up(c, "paris.txt", PARIS)
        wait_all(c)
        spec = c.get("/openapi.json").json()
        rnd = random.Random(7)
        bad = {}
        n = 0
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                for _ in range(25):
                    p = path
                    for seg in __import__("re").findall(r"\{(\w+)\}", path):
                        p = p.replace("{" + seg + "}", __import__("urllib.parse").parse.quote(rnd.choice(JUNK) or "x", safe=""))
                    params = {}
                    for prm in op.get("parameters", []):
                        if prm["in"] == "query":
                            params[prm["name"]] = rnd.choice(JUNK)
                    body = rnd.choice([None, {}, [], "str", 1, {"question": rnd.choice(JUNK)},
                                       {"text": rnd.choice(JUNK), "tone": rnd.choice(JUNK)}, {"name": rnd.choice(JUNK), "role": rnd.choice(JUNK)},
                                       {"type": rnd.choice(["folder", "url", JUNK[3]]), "ref": rnd.choice(JUNK), "interval_minutes": rnd.choice([-1, 10**12, "x"])},
                                       {"rating": rnd.choice([0, 1, -1, 99, "x"])}, {"title": rnd.choice(JUNK), "text": rnd.choice(JUNK)},
                                       {"question": "hi", "session_id": rnd.choice(JUNK), "document_ids": rnd.choice([[], ["x"], None, "x", [1]])}])
                    try:
                        if method == "post" and "upload" in path:
                            r = c.post(p, files=[("files", (rnd.choice(JUNK) or "a", rnd.choice([b"", b"x", os.urandom(300)])))])
                        elif "transcribe" in path:
                            r = c.post(p, files={"audio": ("a.webm", rnd.choice([b"", b"junk", os.urandom(500)]))}, params=params)
                        else:
                            r = c.request(method.upper(), p, params=params, json=body if method in ("post", "put", "patch") else None)
                    except Exception as exc:  # noqa: BLE001
                        bad.setdefault((method.upper(), path), []).append(f"client exc {type(exc).__name__}: {str(exc)[:80]}")
                        continue
                    n += 1
                    if r.status_code >= 500:
                        bad.setdefault((method.upper(), path), []).append(f"HTTP {r.status_code}")
                    elif r.headers.get("content-type", "").startswith("application/json") and r.status_code >= 400:
                        j = r.json()
                        if "error" not in j:
                            bad.setdefault((method.upper(), path), []).append(f"non-envelope {r.status_code}: {str(j)[:80]}")
        report("NOTE", "fuzz requests sent", str(n))
        for k, v in bad.items():
            report("BUG", f"fuzz {k[0]} {k[1]}", f"{len(v)} bad responses, e.g. {v[0]}")
        if not bad:
            report("OK", "fuzz: no 5xx and every error uses the JSON envelope")
        c.__exit__(None, None, None)


def load():
    with tempfile.TemporaryDirectory(**TD) as t:
        fake = FakeOllama()
        c = client(t, fake)
        for i in range(10):
            up(c, f"d{i}.txt", f"Document {i} describes topic number {i} with unique token zz{i}qq and Paris tower.".encode())
        wait_all(c)
        errs = []
        lat = []

        def chat(i):
            t0 = time.time()
            try:
                r = c.post("/api/chat/stream", json={"question": f"Tell me about topic number {i % 10} zz{i%10}qq"})
                ev = parse_sse(r.text)
                if ev[-1][0] != "done":
                    errs.append(("last", ev[-1]))
            except Exception as exc:  # noqa: BLE001
                errs.append(("exc", repr(exc)[:100]))
            lat.append(time.time() - t0)

        def upload(i):
            r = up(c, f"load{i}.txt", f"parallel upload {i} content xx{i}".encode())
            if r.status_code != 202:
                errs.append(("upload", r.status_code))
        t0 = time.time()
        with ThreadPoolExecutor(32) as ex:
            list(ex.map(chat, range(80)))
            list(ex.map(upload, range(40)))
            futs = [ex.submit(chat, i) for i in range(60)] + [ex.submit(upload, 100 + i) for i in range(30)]
            [f.result() for f in futs]
        wall = time.time() - t0
        docs = wait_all(c, 90)
        rows = c.sv.db.all("SELECT status, COUNT(*) c FROM documents GROUP BY status")
        sess = c.sv.db.all("SELECT COUNT(*) c FROM sessions")[0]["c"]
        report("BUG" if errs else "OK", "170 parallel chats + 70 parallel uploads (fake model)",
               f"errors={errs[:3]} wall={wall:.1f}s p95chat={sorted(lat)[int(len(lat)*.95)]:.2f}s docs={rows} sessions={sess}")
        report("NOTE", "documents list hides rows beyond the page size", f"API page returned {len(docs)} of {c.sv.db.all('SELECT COUNT(*) c FROM documents')[0]['c']} (UI requests limit=200 only)")
        c.__exit__(None, None, None)


def big_inputs():
    with tempfile.TemporaryDirectory(**TD) as t:
        c = client(t, max_upload_mb=100)
        cases = {
            "5MB single line no spaces": b"a" * 5_000_000,
            "20MB prose": (b"The quick brown fox jumps over the lazy dog. " * 500_000)[:20_000_000],
            "2MB csv 40k rows": b"id,name,note\n" + b"".join(f"{i},name{i},some note text {i}\n".encode() for i in range(40000)),
        }
        for k, data in cases.items():
            t0 = time.time()
            r = up(c, "big.txt" if "csv" not in k else "big.csv", data)
            if r.status_code != 202:
                report("NOTE", f"big input {k}", f"HTTP {r.status_code} {r.text[:80]}")
                continue
            doc = r.json()["results"][0]["document"]
            end = time.time() + 180
            while time.time() < end and c.get(f"/api/documents/{doc['id']}").json()["status"] == "processing":
                time.sleep(0.3)
            d = c.get(f"/api/documents/{doc['id']}").json()
            dt = time.time() - t0
            report("BUG" if dt > 120 or d["status"] == "processing" else "NOTE", f"ingest {k}", f"status={d['status']} chunks={d['chunk_count']} {dt:.1f}s err={d['error']}")
            c.delete(f"/api/documents/{doc['id']}")
        # event loop blocked during ingestion? measure health latency while a big doc ingests
        up(c, "prose.txt", (b"The quick brown fox jumps over the lazy dog. " * 500_000)[:20_000_000])
        lat = []
        for _ in range(15):
            t0 = time.time()
            c.get("/api/documents")
            lat.append(time.time() - t0)
            time.sleep(0.2)
        report("BUG" if max(lat) > 2 else "OK", "API latency while a 20MB document is being chunked/indexed", f"max={max(lat):.2f}s median={sorted(lat)[len(lat)//2]:.2f}s")
        # huge JSON body
        r = c.post("/api/chat", content=b'{"question":"' + b"x" * 60_000_000 + b'"}', headers={"content-type": "application/json"})
        report("BUG" if r.status_code not in (413, 422) else "OK", "60MB JSON body to /api/chat", f"HTTP {r.status_code} (no body-size limit; whole body parsed in memory)")
        c.__exit__(None, None, None)


def streaming_and_budget():
    with tempfile.TemporaryDirectory(**TD) as t:
        fake = FakeOllama()
        c = client(t, fake, similarity_threshold=0.0)
        # context budget: chunk 8000 chars, top_k 20
        cur = c.get("/api/settings").json()["settings"]
        c.put("/api/settings", json={**cur, "chunk_size": 8000, "chunk_overlap": 100, "top_k": 20, "similarity_threshold": 0.0})
        big = " ".join(f"paris tower fact number {i} eiffel" for i in range(20000)).encode()
        up(c, "big.txt", big)
        wait_all(c)
        c.post("/api/chat", json={"question": "eiffel tower paris facts"})
        sent = fake.chat_calls[-1]
        chars = sum(len(m["content"]) for m in sent)
        ctx_chars = cur["num_ctx"] * 4
        report("BUG" if chars > ctx_chars else "OK", "prompt size vs num_ctx (top_k=20, chunk=8000 allowed by validation)",
               f"prompt={chars} chars (~{chars//4} tokens) vs num_ctx={cur['num_ctx']} tokens; Ollama silently truncates the OLDEST text = the system prompt")
        # history growth
        sid = c.post("/api/sessions", json={}).json()["id"]
        for i in range(8):
            c.post("/api/chat", json={"question": "eiffel " * 1000, "session_id": sid})
        sent = fake.chat_calls[-1]
        report("NOTE", "history window", f"messages sent in last call={len(sent)} total chars={sum(len(m['content']) for m in sent)}")
        c.__exit__(None, None, None)

    # disconnect mid-stream: drive the generator directly and close it early
    with tempfile.TemporaryDirectory(**TD) as t:
        fake = FakeOllama()
        fake.reply = "word " * 400
        c = client(t, fake)
        up(c, "paris.txt", PARIS)
        wait_all(c)
        sid = c.post("/api/sessions", json={}).json()["id"]

        async def run():
            gen = c.sv.rag.stream_answer("Where is the Eiffel Tower located?", sid)
            n = 0
            async for ev in gen:
                if ev["type"] == "token":
                    n += 1
                    if n == 5:
                        break
            await gen.aclose()
        asyncio.run(run())
        msgs = c.sv.db.list_messages(sid)
        ev = c.sv.db.all("SELECT answered FROM events")
        report("OK" if [m["role"] for m in msgs] == ["user", "assistant"] else "BUG", "client disconnects mid-answer",
               f"saved roles={[m['role'] for m in msgs]} partial_chars={len(msgs[-1]['content']) if len(msgs)>1 else 0} events={ev}")
        c.__exit__(None, None, None)

    # model returning garbage / empty / exceptions mid-stream
    with tempfile.TemporaryDirectory(**TD) as t:
        class Bad(FakeOllama):
            mode = "empty"

            async def chat_stream(self, model, messages, options):
                if Bad.mode == "empty":
                    return
                    yield
                if Bad.mode == "boom":
                    yield "partial answer "
                    raise RuntimeError("model crashed")
        fake = Bad()
        c = client(t, fake)
        up(c, "paris.txt", PARIS)
        wait_all(c)
        for mode in ("empty", "boom"):
            Bad.mode = mode
            ev = parse_sse(c.post("/api/chat/stream", json={"question": "Where is the Eiffel Tower?"}).text)
            sid = ev[0][1]["session_id"]
            msgs = c.get(f"/api/sessions/{sid}/messages").json()["items"]
            report("NOTE", f"model {mode} mid-stream", f"final event={ev[-1][0]} {str(ev[-1][1])[:90]} saved={[ (m['role'], len(m['content'])) for m in msgs]}")
        c.__exit__(None, None, None)


if __name__ == "__main__":
    for fn in (fuzz,):
        print(f"\n===== {fn.__name__} =====", flush=True)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            report("BUG", f"probe {fn.__name__} crashed", f"{type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
