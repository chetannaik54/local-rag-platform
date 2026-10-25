import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe1 import client  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = client(t, auth_enabled=True)
    tok = (Path(t) / "admin_token.txt").read_text().strip()
    for h in [{"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": "bearer " + tok.upper()},
              {"X-API-Key": tok + " "}, {"Authorization": "Bearer " + "A" * 100000}]:
        print(str(h)[:40], c.get("/api/auth/me", headers=h).status_code)
    codes = [c.get("/api/auth/me", headers={"Authorization": f"Bearer guess{i}"}).status_code for i in range(300)]
    print("300 guesses ->", {k: codes.count(k) for k in sorted(set(codes))})
    c.__exit__(None, None, None)
