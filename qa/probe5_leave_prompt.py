"""Does the HR assistant ask for missing dates instead of inventing them? (real model, a few phrasings)"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe3_llm import ask, live  # noqa: E402

PHRASES = ["Book leave for me.", "I want to take some time off.", "Please submit a leave request for next week.",
           "Take leave from 2027-03-01 to 2027-03-03."]
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = live(t)
    for q in PHRASES:
        sid = c.post("/api/sessions", json={"agent_id": "hr"}).json()["id"]
        msg, err, dt = ask(c, q, agent="hr", sid=sid)
        tools = [(x["name"], x["args"], x["ok"]) for x in (msg or {}).get("tools", [])]
        print(f"Q={q!r}\n  tools={tools}\n  answer={(msg or {}).get('content', '')[:200]!r} ({dt:.0f}s)", flush=True)
    c.__exit__(None, None, None)
