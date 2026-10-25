"""Upload handling and the background extract -> chunk -> embed -> store pipeline."""
import asyncio
import hashlib
import logging
import os
from pathlib import Path

from fastapi import UploadFile

from app.core.errors import AppError
from app.database.db import Database, new_id
from app.services import parsers
from app.services.chunking import chunk_pages
from app.services.embedding_service import EmbeddingService
from app.services.vector_store import VectorStore

log = logging.getLogger(__name__)
READ_SIZE = 1024 * 1024


class IngestionService:
    def __init__(self, db: Database, vectors: VectorStore, embeddings: EmbeddingService,
                 upload_dir: Path, max_bytes: int):
        self._db = db
        self._vectors = vectors
        self._embeddings = embeddings
        self._dir = upload_dir
        self._max = max_bytes
        self._dir.mkdir(parents=True, exist_ok=True)
        # One ingestion job at a time keeps Chroma writes and Ollama load predictable.
        self._gate = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # -- upload ------------------------------------------------------------
    async def accept(self, upload: UploadFile, org_id: str) -> tuple[dict, bool]:
        """Validate and store one upload. Returns (document_row, is_duplicate)."""
        filename = parsers.sanitize_filename(upload.filename or "")
        ext = parsers.validate_extension(filename)
        tmp = self._dir / f".{new_id()}.part"
        sha = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as out:
                while chunk := await upload.read(READ_SIZE):
                    size += len(chunk)
                    if size > self._max:
                        raise AppError(
                            "file_too_large",
                            f"File exceeds the {self._max // (1024 * 1024)} MB limit.", 413,
                        )
                    sha.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise AppError("empty_file", "File is empty.", 422)
            return self._finalize(tmp, filename, ext, size, sha.hexdigest(), org_id)
        finally:
            tmp.unlink(missing_ok=True)

    def _finalize(self, tmp: Path, filename: str, ext: str, size: int, sha: str, org_id: str,
                  **source) -> tuple[dict, bool]:
        parsers.validate_signature(tmp, ext)
        existing = self._db.find_by_hash(sha, org_id)
        if existing:
            return existing, True
        doc = self._db.add_document(
            filename=filename, ext=ext, size_bytes=size, sha256=sha, stored_path="", org_id=org_id, **source
        )
        final = self._dir / f"{doc['id']}{ext}"
        os.replace(tmp, final)
        self._db.update_document(doc["id"], stored_path=str(final))
        doc["stored_path"] = str(final)
        task = asyncio.create_task(self.process(doc["id"]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return doc, False

    async def ingest_path(self, src: Path, filename: str, source_type: str, source_ref: str,
                          connector_id: str, org_id: str) -> tuple[dict, bool]:
        """Ingest a file that already exists on disk (connectors). The source is copied, not moved."""
        filename = parsers.sanitize_filename(filename)
        ext = parsers.validate_extension(filename)
        tmp = self._dir / f".{new_id()}.part"
        sha = hashlib.sha256()
        size = 0
        try:
            with src.open("rb") as inp, tmp.open("wb") as out:
                while chunk := await asyncio.to_thread(inp.read, READ_SIZE):
                    size += len(chunk)
                    if size > self._max:
                        raise AppError("file_too_large", "File exceeds the upload size limit.", 413)
                    sha.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise AppError("empty_file", "File is empty.", 422)
            return self._finalize(tmp, filename, ext, size, sha.hexdigest(), org_id, source_type=source_type,
                                  source_ref=source_ref, connector_id=connector_id)
        finally:
            tmp.unlink(missing_ok=True)

    async def wait_idle(self) -> None:
        """Wait for queued ingestion jobs (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- pipeline ----------------------------------------------------------
    async def process(self, doc_id: str) -> None:
        async with self._gate:
            doc = self._db.get_document(doc_id)
            if not doc:
                return
            settings = self._db.get_settings(doc["org_id"])
            try:
                pages = await asyncio.to_thread(
                    parsers.extract_pages, Path(doc["stored_path"]), doc["ext"]
                )
                chunks = await asyncio.to_thread(
                    chunk_pages, pages, settings.chunk_size, settings.chunk_overlap
                )
                if not chunks:
                    raise AppError("empty_file", "No text could be extracted.", 422)
                vectors = await self._embeddings.embed_documents(
                    [c.text for c in chunks], settings
                )
                if not self._db.get_document(doc_id):  # deleted while embedding
                    return
                await asyncio.to_thread(
                    self._vectors.add, doc_id, doc["filename"],
                    settings.embedding_model, chunks, vectors,
                )
                if not self._db.get_document(doc_id):  # deleted during the write
                    await asyncio.to_thread(self._vectors.delete_document, doc_id)
                    return
                numbered = [p.page for p in pages if p.page is not None]
                self._db.update_document(
                    doc_id, status="ready", error=None, chunk_count=len(chunks),
                    page_count=max(numbered) if numbered else None,
                    embedding_model=settings.embedding_model,
                )
                log.info("ingested doc=%s chunks=%d model=%s", doc_id, len(chunks),
                         settings.embedding_model)
            except AppError as exc:
                self._fail(doc_id, exc.message)
            except Exception as exc:
                log.exception("ingestion failed doc=%s", doc_id)
                self._fail(doc_id, f"Unexpected error: {type(exc).__name__}: {str(exc)[:150]}")

    def _fail(self, doc_id: str, message: str) -> None:
        log.warning("ingestion failed doc=%s reason=%s", doc_id, message)
        if self._db.get_document(doc_id):
            self._db.update_document(doc_id, status="failed", error=message)

    # -- deletion ----------------------------------------------------------
    async def delete(self, doc_id: str) -> bool:
        doc = self._db.get_document(doc_id)
        if not doc:
            return False
        await asyncio.to_thread(self._vectors.delete_document, doc_id)
        self._db.delete_document(doc_id)
        if doc["stored_path"]:
            Path(doc["stored_path"]).unlink(missing_ok=True)
        return True
