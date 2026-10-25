"""Adversarial probes, batch 1. Run with the backend venv. Prints one line per probe: [OK]/[BUG]/[NOTE]."""
import asyncio
import io
import sys
import tempfile
import threading
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend" / "tests"))

from conftest import FakeOllama, parse_sse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import AppConfig  # noqa: E402
from app.main import create_app  # noqa: E402

PARIS = b"The Eiffel Tower is located in Paris France. It was completed in 1889 for the World Fair."


def report(tag, name, detail=""):
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def client(tmp, fake=None, **cfg):
    fake = fake or FakeOllama()
    base = dict(data_dir=Path(tmp), similarity_threshold=0.25, _env_file=None)
    c = TestClient(create_app(AppConfig(**{**base, **cfg}), ollama=fake, run_scheduler=False), raise_server_exceptions=False)
    c.__enter__()
    c.sv = c.app.state.services
    c.fake = fake
    return c


def wait_all(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        docs = c.get("/api/documents?limit=200").json()["items"]
        if all(d["status"] != "processing" for d in docs):
            return docs
        time.sleep(0.1)
    return docs


def up(c, name, data, ctype="application/octet-stream"):
    return c.post("/api/documents/upload", files=[("files", (name, data, ctype))])


# ---------------------------------------------------------------- uploads
def uploads():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        c = client(t)
        # filenames
        for name in ["../../../etc/passwd.txt", "..\\..\\win.ini.txt", "a" * 400 + ".txt", "x\x00y.txt",
                     "CON.txt", "..txt", ".txt", "doc.TXT", "doc.txt.exe", "emoji\U0001F600.txt", "文档.txt",
                     "name with ; semi, comma.txt", "<script>alert(1)</script>.txt"]:
            r = up(c, name, b"hello world " + name.encode("utf8", "ignore"))
            if r.status_code >= 500:
                report("BUG", f"upload filename {name!r}", f"HTTP {r.status_code}")
            else:
                res = r.json().get("results", [{}])[0]
                shown = (res.get("document") or {}).get("filename") or res.get("error")
                report("OK", f"upload filename {name[:30]!r}", f"{r.status_code} -> {str(shown)[:60]!r}")
        # content
        cases = {
            "binary-as-txt": ("a.txt", bytes(range(256)) * 50),
            "nul-in-head": ("a.txt", b"\x00\x01\x02text"),
            "fake-pdf": ("a.pdf", b"%PDF-1.4\n" + b"garbage" * 100),
            "fake-docx": ("a.docx", b"PK\x03\x04" + b"junk" * 50),
            "html-as-md": ("a.md", b"<script>alert(1)</script># hi"),
            "utf16-txt": ("a.txt", "héllo wörld ünïcode ".encode("utf-16")),
            "latin1-txt": ("a.txt", "caf\xe9 cr\xe8me br\xfbl\xe9e".encode("latin-1") * 5),
            "csv-ragged": ("a.csv", b"a,b,c\n1\n1,2,3,4,5,6\n,,\n"),
            "csv-only-header": ("a.csv", b"a,b,c\n"),
            "csv-huge-field": ("a.csv", b"a,b\n" + b"x" * 200000 + b",1\n"),
            "single-char": ("a.txt", b"x"),
            "only-whitespace": ("a.txt", b" \n\t\n "),
        }
        for k, (n, d) in cases.items():
            r = up(c, n, d)
            if r.status_code >= 500:
                report("BUG", f"upload content {k}", f"HTTP {r.status_code} {r.text[:120]}")
        docs = wait_all(c)
        for d in docs:
            pass
        st = {}
        for d in docs:
            st.setdefault(d["status"], []).append((d["filename"], (d["error"] or "")[:50]))
        report("NOTE", "statuses after content probes", str({k: len(v) for k, v in st.items()}))
        for f, e in st.get("failed", []):
            report("NOTE", f"failed doc {f}", e)
        # zip bomb docx (declared sizes honest)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"0" * (350 * 1024 * 1024))
        r = up(c, "bomb.docx", buf.getvalue())
        d = wait_all(c)
        bomb = [x for x in d if x["filename"] == "bomb.docx"]
        report("OK" if bomb and bomb[0]["status"] == "failed" else "BUG", "docx zip-bomb (350MB inflated)",
               str(bomb[0]["error"] if bomb else r.text[:100]))
        # extension case + upper-case magic
        r = up(c, "REPORT.PDF", b"not a pdf")
        report("OK" if r.status_code == 400 else "BUG", "upper-case .PDF with junk body", str(r.status_code))
        # no files / wrong field
        r = c.post("/api/documents/upload")
        report("OK" if r.status_code == 422 else "BUG", "upload with no files", str(r.status_code))
        r = c.post("/api/documents/upload", files=[("wrong", ("a.txt", b"x"))])
        report("OK" if r.status_code == 422 else "BUG", "upload wrong field name", str(r.status_code))
        # 600 tiny files in one request
        files = [("files", (f"f{i}.txt", f"unique file number {i} about topic {i*7919}".encode())) for i in range(300)]
        t0 = time.time()
        r = c.post("/api/documents/upload", files=files)
        report("OK" if r.status_code == 202 else "BUG", "300 files in one request", f"{r.status_code} in {time.time()-t0:.1f}s")
        t0 = time.time()
        docs = wait_all(c, 120)
        report("NOTE", "300 tiny files fully indexed", f"{time.time()-t0:.1f}s, ready={sum(d['status']=='ready' for d in docs)}")
        c.__exit__(None, None, None)


# ---------------------------------------------------------------- races
def races():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        c = client(t)
        # same content uploaded 10x concurrently
        results = []

        def go(i):
            r = up(c, f"same{i}.txt", PARIS)
            results.append(r.json()["results"][0])
        th = [threading.Thread(target=go, args=(i,)) for i in range(10)]
        [x.start() for x in th]
        [x.join() for x in th]
        wait_all(c)
        total = c.get("/api/documents").json()["total"]
        report("OK" if total == 1 else "BUG", "10 concurrent identical uploads dedupe", f"documents={total}")

        # delete immediately after upload (while embedding)
        class SlowEmbed(FakeOllama):
            async def embed(self, model, inputs):
                await asyncio.sleep(1.0)
                return await super().embed(model, inputs)
        c2 = client(t + "_2", SlowEmbed()) if False else None
        c.__exit__(None, None, None)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        slow = FakeOllama()
        orig = slow.embed

        async def slow_embed(model, inputs):
            await asyncio.sleep(1.5)
            return await orig(model, inputs)
        slow.embed = slow_embed
        c = client(t, slow)
        r = up(c, "slow.txt", PARIS)
        did = r.json()["results"][0]["document"]["id"]
        time.sleep(0.3)
        d = c.delete(f"/api/documents/{did}")
        time.sleep(3)
        orphan = c.sv.vectors.count()
        files = list((Path(t) / "uploads").glob("*"))
        rows = c.sv.db.all("SELECT id, status FROM documents")
        report("OK" if orphan == 0 and not rows else "BUG", "delete while embedding",
               f"delete={d.status_code} vectors_left={orphan} rows={rows} files={[f.name for f in files]}")
        # delete during chroma write window is hard; do 30 rapid upload+delete cycles
        for i in range(30):
            rr = up(c, f"r{i}.txt", f"rapid cycle document {i} unique".encode())
            did = rr.json()["results"][0]["document"]["id"]
            c.delete(f"/api/documents/{did}")
        time.sleep(6)
        report("OK" if c.sv.vectors.count() == 0 else "BUG", "30 rapid upload+delete cycles leave no vectors",
               f"vectors={c.sv.vectors.count()} rows={c.sv.db.all('SELECT COUNT(*) c FROM documents')[0]['c']}")
        c.__exit__(None, None, None)


# ---------------------------------------------------------------- sessions / handoff
def sessions():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        c = client(t)
        sid = c.post("/api/sessions", json={"agent_id": "it"}).json()["id"]
        c.post(f"/api/sessions/{sid}/handoff")
        hid = c.get("/api/handoffs").json()["items"][0]["id"]
        c.delete(f"/api/sessions/{sid}")  # user deletes chat while waiting for a human
        q = c.get("/api/handoffs").json()["items"]
        report("BUG" if q else "OK", "handoff queue after the user deletes the session",
               f"{len(q)} orphan item(s): {[(h['session_title'], h['status']) for h in q]}")
        r = c.post(f"/api/handoffs/{hid}/reply", json={"text": "hello?"})
        report("BUG" if r.status_code >= 500 else "OK", "agent replies into deleted session", f"HTTP {r.status_code} {r.text[:100]}")
        r = c.get(f"/api/sessions/{sid}/messages")
        report("NOTE", "agent reads messages of deleted session", str(r.status_code))
        # clear messages during handoff
        sid2 = c.post("/api/sessions", json={}).json()["id"]
        c.post(f"/api/sessions/{sid2}/handoff")
        c.delete(f"/api/sessions/{sid2}/messages")
        report("NOTE", "clear conversation during an open handoff", f"handoff still open={bool(c.sv.store.open_handoff(sid2))}")
        # handoff flood
        for _ in range(200):
            s = c.post("/api/sessions", json={}).json()["id"]
            c.post(f"/api/sessions/{s}/handoff", json={"reason": "x" * 500})
        n = len(c.get("/api/handoffs").json()["items"])
        report("BUG" if n >= 200 else "OK", "no limit on handoff requests (queue flooding)", f"queue size={n}")
        # unknown agent id, deleted agent bound to a session
        a = c.post("/api/agents", json={"name": "Tmp", "system_prompt": "x", "tools": ["business_summary"]}).json()["id"]
        s = c.post("/api/sessions", json={"agent_id": a}).json()["id"]
        c.delete(f"/api/agents/{a}")
        r = c.post("/api/chat", json={"question": "hi", "session_id": s})
        report("OK" if r.status_code < 500 else "BUG", "chat in a session whose agent was deleted", f"HTTP {r.status_code}")
        c.__exit__(None, None, None)


# ---------------------------------------------------------------- tools
def tools():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        c = client(t)
        fake = c.fake

        def call(agent, name, args):
            sid = c.post("/api/sessions", json={"agent_id": agent}).json()["id"]
            fake.tool_script = [[{"name": name, "args": args}]]
            r = c.post("/api/chat/stream", json={"question": "go", "session_id": sid})
            ev = parse_sse(r.text)
            tool = next((d for k, d in ev if k == "tool"), None)
            err = next((d for k, d in ev if k == "error"), None)
            return tool, err, r.status_code

        # wrong types
        for args in [{"start_date": 20260101, "end_date": 20260102}, {"start_date": None, "end_date": None},
                     {"start_date": ["a"], "end_date": {"x": 1}}, {"start_date": "2026-13-45", "end_date": "2026-01-01"},
                     {"start_date": "２０２６-０１-０１", "end_date": "2026-01-02"}, "notadict", [], {"leave_type": {"a": 1}}]:
            tool, err, code = call("hr", "request_leave", args)
            bad = err is not None or code >= 500 or tool is None
            report("BUG" if bad else "OK", f"request_leave args={str(args)[:45]}", f"ok={tool and tool['ok']} err={err}")
        # absurd range -> CPU
        start = date.today() + timedelta(days=1)
        t0 = time.time()
        tool, err, code = call("hr", "request_leave", {"start_date": start.isoformat(), "end_date": "9999-12-31", "leave_type": "sick"})
        dt = time.time() - t0
        row = c.sv.db.all("SELECT days, status FROM leave_requests ORDER BY created_at DESC LIMIT 1")
        report("BUG" if tool and tool["ok"] else "OK", "leave request spanning to year 9999 (sick leave)",
               f"accepted={tool and tool['ok']} days={row[0]['days'] if row else None} took={dt:.1f}s")
        # annual leave over-booking via next-year dates
        ny = date(date.today().year + 1, 1, 5)
        booked = 0
        for i in range(6):
            s = ny + timedelta(days=i * 30)
            while s.weekday() != 0:
                s += timedelta(days=1)
            tool, _, _ = call("hr", "request_leave", {"start_date": s.isoformat(), "end_date": (s + timedelta(days=18)).isoformat()})
            if tool and tool["ok"]:
                booked += tool["result"]["working_days"]
        report("BUG" if booked > 20 else "OK", "annual leave entitlement (20 days) enforced across next-year requests", f"booked {booked} annual days")
        # duplicate / overlapping requests
        s = date.today() + timedelta(days=200)
        while s.weekday() != 0:
            s += timedelta(days=1)
        ok = 0
        for _ in range(3):
            tool, _, _ = call("hr", "request_leave", {"start_date": s.isoformat(), "end_date": s.isoformat(), "leave_type": "unpaid"})
            ok += bool(tool and tool["ok"])
        report("BUG" if ok > 1 else "OK", "identical leave request submitted 3 times", f"{ok} accepted")
        # ticket injection / lengths
        tool, err, _ = call("it", "create_ticket", {"title": "x" * 100000, "description": "y" * 100000, "priority": "high"})
        row = c.sv.db.all("SELECT length(title) a, length(description) b FROM tickets ORDER BY created_at DESC LIMIT 1")
        report("OK", "oversized ticket fields truncated", str(row))
        tool, _, _ = call("it", "create_ticket", {"title": "'; DROP TABLE tickets;--", "priority": "high"})
        report("OK" if c.sv.db.all("SELECT COUNT(*) c FROM tickets")[0]["c"] >= 1 else "BUG", "SQL injection via ticket title", "table intact")
        # IDOR on tickets: ticket from 'local' readable by other user? (auth off => single user) skip
        # tool the agent doesn't own, tool name junk
        for nm in ["__import__", "", "get_leave_balance\x00", "business_summary"]:
            tool, err, code = call("sales", nm, {})
            report("OK" if (tool is None or not tool["ok"]) and code < 500 else "BUG",
                   f"sales agent invoking tool {nm!r}", f"ok={tool and tool['ok']} {tool and tool['result']}")
        # model loops calling tools forever
        sid = c.post("/api/sessions", json={"agent_id": "hr"}).json()["id"]
        fake.tool_script = [[{"name": "get_leave_balance"}] for _ in range(50)]
        r = c.post("/api/chat/stream", json={"question": "loop", "session_id": sid})
        ev = parse_sse(r.text)
        n = sum(1 for k, _ in ev if k == "tool")
        report("OK" if n <= 6 and ev[-1][0] in ("done", "error") else "BUG", "runaway tool loop is capped", f"{n} tool calls, last event {ev[-1][0]}")
        c.__exit__(None, None, None)


if __name__ == "__main__":
    for fn in (uploads, races, sessions, tools):
        print(f"\n===== {fn.__name__} =====", flush=True)
        try:
            fn()
        except Exception as exc:  # a probe crashing is itself a finding
            import traceback
            report("BUG", f"probe {fn.__name__} crashed", f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
