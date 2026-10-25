"""Query embedding, vector search, threshold filtering and light lexical reranking."""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from app.database.db import Database
from app.models.schemas import RuntimeSettings
from app.services.embedding_service import EmbeddingService
from app.services.vector_store import Hit, VectorStore

log = logging.getLogger(__name__)
_WORD = re.compile(r"\w{3,}", re.UNICODE)
OVERFETCH = 3


@dataclass
class RetrievalResult:
    hits: list[Hit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


class RetrievalService:
    def __init__(self, db: Database, vectors: VectorStore, embeddings: EmbeddingService):
        self._db = db
        self._vectors = vectors
        self._embeddings = embeddings

    async def retrieve(self, query: str, settings: RuntimeSettings, org_id: str,
                       document_ids: list[str] | None = None) -> RetrievalResult:
        """Search only ``org_id``'s documents; ids belonging to other organisations are simply ignored."""
        result = RetrievalResult()
        docs = self._db.ready_documents(document_ids, org_id)
        usable = [d["id"] for d in docs if d["embedding_model"] == settings.embedding_model]
        stale = len(docs) - len(usable)
        if stale:
            result.warnings.append(
                f"{stale} document(s) were indexed with a different embedding model and were "
                f"skipped. Re-upload them to search with '{settings.embedding_model}'."
            )
        if not usable:
            return result

        t0 = time.perf_counter()
        qvec = await self._embeddings.embed_query(query, settings)
        raw = await asyncio.to_thread(
            self._vectors.query, qvec, settings.top_k * OVERFETCH, usable,
            settings.embedding_model,
        )
        kept = [h for h in raw if h.score >= settings.similarity_threshold]

        q_terms = _terms(query)

        def blended(h: Hit) -> float:
            overlap = len(q_terms & _terms(h.text)) / len(q_terms) if q_terms else 0.0
            return 0.8 * h.score + 0.2 * overlap

        kept.sort(key=blended, reverse=True)
        result.hits = kept[: settings.top_k]
        log.info(
            "retrieval candidates=%d kept=%d returned=%d docs=%s top_score=%s ms=%d",
            len(raw), len(kept), len(result.hits),
            sorted({h.document_id for h in result.hits}),
            round(result.hits[0].score, 3) if result.hits else None,
            (time.perf_counter() - t0) * 1000,
        )
        return result
