"""End-to-end over a REAL socket with the REAL model: login, two tenants, embed key, streaming, CORS.

Starts its own uvicorn on a throw-away data directory (never ./data). Needs Ollama running with the configured
chat + embedding models. Run:  backend\\.venv\\Scripts\\python.exe qa\\e2e_tenancy.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PORT = 8021
BASE = f"http://127.0.0.1:{PORT}"
SHOP = "https://shop.example.com"
PW = "correct-horse-1"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(("[OK]  " if ok else "[FAIL]") + f" {name}" + (f" -- {detail}" if detail else ""), flush=True)


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def main():
    tmp = tempfile.mkdtemp(prefix="rag-e2e-")
    env = {**os.environ, "DATA_DIR": tmp, "AUTH_ENABLED": "true", "ADMIN_EMAIL": "root@example.com",
           "ADMIN_PASSWORD": "Sup3r-secret-pw!", "PUBLIC_URL": BASE, "LOG_LEVEL": "WARNING"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                              cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        c = httpx.Client(base_url=BASE, timeout=300)
        for _ in range(60):
            try:
                if c.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        # --- login
        bad = c.post("/api/auth/login", json={"email": "root@example.com", "password": "wrong-password-1"})
        check("wrong password is refused", bad.status_code == 401)
        r = c.post("/api/auth/login", json={"email": "root@example.com", "password": "Sup3r-secret-pw!"})
        root = hdr(r.json()["token"])
        check("platform admin signs in", r.status_code == 200 and r.json()["me"]["is_super"])

        # --- two tenants
        for slug in ("acme", "globex"):
            c.post("/api/orgs", json={"name": slug.title(), "slug": slug, "admin_email": f"a@{slug}.test", "admin_password": PW}, headers=root)
        acme = hdr(c.post("/api/auth/login", json={"email": "a@acme.test", "password": PW}).json()["token"])
        globex = hdr(c.post("/api/auth/login", json={"email": "a@globex.test", "password": PW}).json()["token"])
        text = b"The Zorblax-9 reactor was commissioned in 2041 in the city of Quillham. Its chief engineer is Marta Okonkwo."
        up = c.post("/api/documents/upload", files=[("files", ("facts.txt", text))], headers=acme)
        doc = up.json()["results"][0]["document"]
        for _ in range(120):
            if c.get(f"/api/documents/{doc['id']}", headers=acme).json()["status"] != "processing":
                break
            time.sleep(0.5)
        check("acme document indexed with the real embedding model", c.get(f"/api/documents/{doc['id']}", headers=acme).json()["status"] == "ready")
        check("globex cannot see acme's document", c.get(f"/api/documents/{doc['id']}", headers=globex).status_code == 404
              and c.get("/api/documents", headers=globex).json()["items"] == [])

        # --- embed key + CORS over real HTTP
        key = c.post("/api/embed-keys", json={"name": "shop", "allowed_origins": [SHOP], "agent_id": "general"}, headers=acme).json()
        check("embed snippet uses PUBLIC_URL", key["snippet"].startswith(f'<script src="{BASE}/widget.js"'))
        pre = c.options("/api/chat/stream", headers={"Origin": SHOP, "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "content-type,x-embed-key,x-visitor-id"})
        check("CORS preflight allows the registered website", pre.status_code == 204 and pre.headers.get("access-control-allow-origin") == SHOP)
        pre_bad = c.options("/api/chat/stream", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        check("CORS preflight refuses other websites", pre_bad.status_code == 403 and "access-control-allow-origin" not in pre_bad.headers)
        visitor = {"X-Embed-Key": key["token"], "X-Visitor-Id": "e2e-visitor-0001", "Origin": SHOP}
        check("another website cannot use the key", c.get("/api/auth/me", headers={**visitor, "Origin": "https://evil.example"}).status_code == 403)
        check("visitor is chat-only", c.get("/api/documents", headers=visitor).status_code == 403
              and c.get("/api/users", headers=visitor).status_code == 403)

        # --- real streaming through the middleware stack, real model
        t0 = time.time()
        first = None
        tokens = 0
        answer = ""
        with c.stream("POST", "/api/chat/stream", json={"question": "Who is the chief engineer of the Zorblax-9 reactor?"},
                      headers={**visitor, "Content-Type": "application/json"}) as resp:
            check("stream response carries CORS header", resp.headers.get("access-control-allow-origin") == SHOP)
            event = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event == "token":
                    tokens += 1
                    first = first if first is not None else time.time() - t0
                    answer += __import__("json").loads(line[6:])["text"]
        total = time.time() - t0
        check("answer comes from the tenant's own document", "okonkwo" in answer.lower(), answer[:90])
        check("tokens stream incrementally (not buffered into one burst)",
              tokens > 5 and first is not None and (total - first) > 0.3, f"{tokens} tokens, first after {first:.1f}s, finished after {total:.1f}s")

        # --- the other tenant's visitor gets nothing from acme's documents
        key2 = c.post("/api/embed-keys", json={"name": "g", "allowed_origins": [SHOP]}, headers=globex).json()
        v2 = {"X-Embed-Key": key2["token"], "X-Visitor-Id": "e2e-visitor-0002", "Origin": SHOP}
        r2 = c.post("/api/chat", json={"question": "Who is the chief engineer of the Zorblax-9 reactor?"}, headers=v2)
        check("globex's widget cannot see acme's knowledge", r2.status_code == 200 and r2.json()["message"]["citations"] == []
              and "okonkwo" not in r2.json()["message"]["content"].lower(), r2.json()["message"]["content"][:90])

        # --- oversized body and session expiry over the wire
        big = c.post("/api/chat", content=b'{"question":"' + b"x" * 400_000 + b'"}', headers={**visitor, "Content-Type": "application/json"})
        check("oversized request is refused with CORS headers", big.status_code == 413 and big.headers.get("access-control-allow-origin") == SHOP)
        c.post("/api/auth/logout", headers=acme)
        check("logout kills the session token", c.get("/api/auth/me", headers=acme).status_code == 401)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
