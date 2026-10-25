"""ChromaDB persistence. All calls are blocking; callers use asyncio.to_thread."""
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma

from app.services.chunking import Chunk

COLLECTION = "documents"
WRITE_BATCH = 250


@dataclass
class Hit:
    id: str
    text: str
    document_id: str
    filename: str
    page: int | None
    chunk_index: int
    score: float  # cosine similarity, 1.0 = identical


class VectorStore:
    def __init__(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self._col = self._client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        # LangChain wrapper over the same collection; used for retrieval. Vectors are
        # computed by EmbeddingService, so no embedding_function is attached.
        self._lc = Chroma(
            client=self._client, collection_name=COLLECTION,
            collection_metadata={"hnsw:space": "cosine"},
        )

    def add(self, document_id: str, filename: str, model: str,
            chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        # Chroma rejects a single write above its max batch size (5461 on common SQLite builds).
        # Its native add() also holds Python's interpreter lock for the whole call (~1.4 s per 2000 vectors),
        # which freezes the web server, so keep batches small: many short pauses beat one long one.
        step = max(1, min(self._client.get_max_batch_size(), WRITE_BATCH))
        try:
            for i in range(0, len(chunks), step):
                part = chunks[i : i + step]
                self._col.add(
                    ids=[f"{document_id}:{c.index}" for c in part],
                    embeddings=embeddings[i : i + step],
                    documents=[c.text for c in part],
                    metadatas=[
                        {
                            "document_id": document_id,
                            "filename": filename,
                            "page": c.page if c.page is not None else 0,
                            "chunk_index": c.index,
                            "embedding_model": model,
                        }
                        for c in part
                    ],
                )
        except Exception:
            self.delete_document(document_id)  # never leave a half-indexed document behind
            raise

    def delete_document(self, document_id: str) -> None:
        self._col.delete(where={"document_id": document_id})

    def indexed_document_ids(self) -> set[str]:
        """Every document id that has vectors (used to prune orphans after a crash)."""
        ids: set[str] = set()
        offset, page = 0, 5000
        while True:
            got = self._col.get(include=["metadatas"], limit=page, offset=offset)["metadatas"]
            ids.update(m["document_id"] for m in got)
            if len(got) < page:
                return ids
            offset += page

    def count(self, document_id: str | None = None) -> int:
        if document_id is None:
            return self._col.count()
        return len(self._col.get(where={"document_id": document_id}, include=[])["ids"])

    def query(self, embedding: list[float], n: int, document_ids: list[str],
              model: str) -> list[Hit]:
        if not document_ids or self._col.count() == 0:
            return []
        where = {
            "$and": [
                {"document_id": {"$in": document_ids}},
                {"embedding_model": model},
            ]
        }
        results = self._lc.similarity_search_by_vector_with_relevance_scores(
            embedding, k=n, filter=where
        )
        hits = []
        for doc, dist in results:  # dist is cosine distance for this collection
            meta = doc.metadata
            hits.append(Hit(
                id=f"{meta['document_id']}:{meta['chunk_index']}", text=doc.page_content,
                document_id=meta["document_id"], filename=meta["filename"],
                page=meta["page"] or None, chunk_index=meta["chunk_index"],
                score=1.0 - float(dist),
            ))
        return hits
