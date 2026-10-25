"""Re-checks after the fixes: lockout behaviour and API latency during a large ingest (cheap fake embeddings)."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe1 import FakeOllama, client, report, up  # noqa: E402

# 1. wrong-token lockout
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
    c = client(t, auth_enabled=True)
    codes = [c.get("/api/auth/me", headers={"Authorization": f"Bearer guess{i}"}).status_code for i in range(60)]
    print("codes:", {k: codes.count(k) for k in set(codes)}, "first 429 at attempt", codes.index(429) + 1 if 429 in codes else None)
    tok = (Path(t) / "admin_token.txt").read_text().strip()
    print("valid token right after:", c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code)
    c.__exit__(None, None, None)


# 2. API latency during a 20 MB ingest, with an embedder that does no CPU work on the loop
class CheapEmbed(FakeOllama):
    async def embed(self, model, inputs):
        await asyncio.sleep(0)
        return [[0.1] * 8 for _ in inputs]


def latency(label, text):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        c = client(t, CheapEmbed(), max_upload_mb=100)
        r = up(c, "prose.txt", text)
        did = r.json()["results"][0]["document"]["id"]
        lat, t0 = [], time.time()
        while c.get(f"/api/documents/{did}").json()["status"] == "processing" and time.time() - t0 < 300:
            s = time.time()
            c.get("/api/health")
            lat.append(time.time() - s)
            time.sleep(0.1)
        lat.sort()
        d = c.get(f"/api/documents/{did}").json()
        report("NOTE", f"{label}: API latency during ingest", f"status={d['status']} chunks={d['chunk_count']} "
               f"total={time.time()-t0:.1f}s samples={len(lat)} median={lat[len(lat)//2]:.3f}s p95={lat[int(len(lat)*.95)]:.3f}s max={lat[-1]:.2f}s")
        c.__exit__(None, None, None)


latency("20MB prose", (b"The quick brown fox jumps over the lazy dog. " * 500_000)[:20_000_000])
latency("5MB single line", b"a" * 5_000_000)
