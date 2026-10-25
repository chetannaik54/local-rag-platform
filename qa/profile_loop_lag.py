"""Which ingestion stage stalls the asyncio event loop? Measures max loop lag around each stage."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import parsers  # noqa: E402
from app.services.chunking import chunk_pages  # noqa: E402
from app.services.chunking import Chunk  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402

TEXT = (b"The quick brown fox jumps over the lazy dog. " * 500_000)[:20_000_000]


async def lag_during(label, fn, *args):
    worst, stop = 0.0, False

    async def monitor():
        nonlocal worst
        while not stop:
            t = time.perf_counter()
            await asyncio.sleep(0.02)
            worst = max(worst, time.perf_counter() - t - 0.02)

    m = asyncio.create_task(monitor())
    t0 = time.perf_counter()
    out = await asyncio.to_thread(fn, *args)
    stop = True
    await m
    print(f"{label:<28} took {time.perf_counter()-t0:5.1f}s   worst loop lag {worst:5.2f}s", flush=True)
    return out


async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
        p = Path(t) / "big.txt"
        p.write_bytes(TEXT)
        pages = await lag_during("parse (read+decode)", parsers.extract_pages, p, ".txt")
        chunks = await lag_during("chunk (clean+split)", chunk_pages, pages, 1000, 150)
        print("chunks:", len(chunks))
        vs = VectorStore(Path(t) / "chroma")
        vecs = [[0.1] * 8 for _ in chunks]
        await lag_during("chroma add (batched)", vs.add, "doc1", "big.txt", "m", chunks, vecs)
        small = [Chunk(text=c.text, page=None, index=i) for i, c in enumerate(chunks[:2000])]
        await lag_during("chroma add 1 batch of 2000", vs.add, "doc2", "b.txt", "m", small, vecs[:2000])


asyncio.run(main())
