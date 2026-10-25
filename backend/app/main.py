import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.auth import bootstrap_admin
from app.api.cors import DynamicCORSMiddleware
from app.api.limits import BodyLimitMiddleware, RateLimiter
from app.api.routes import register_handlers, router
from app.api.routes_admin import router as admin_router
from app.api.routes_auth import router as auth_router
from app.api.routes_tenancy import router as tenancy_router
from app.core.config import AppConfig
from app.database.db import Database
from app.database.repos import Store
from app.services.connector_service import ConnectorService
from app.services.embedding_service import EmbeddingService
from app.services.ingestion_service import IngestionService
from app.services.ollama_service import OllamaService
from app.services.rag_service import RagService
from app.services.retrieval_service import RetrievalService
from app.services.vector_store import VectorStore
from app.services.voice_service import VoiceService

log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


@dataclass
class Services:
    config: AppConfig
    db: Database
    store: Store
    vectors: VectorStore
    ollama: OllamaService
    ingestion: IngestionService
    connectors: ConnectorService
    voice: VoiceService
    rag: RagService
    limits: RateLimiter


def build_services(config: AppConfig, ollama=None) -> Services:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / "app.db", config)
    store = Store(db)
    vectors = VectorStore(config.data_dir / "chroma")
    ollama = ollama or OllamaService(db.get_settings, config.ollama_timeout_s)
    embeddings = EmbeddingService(ollama, config.embed_batch_size)
    retrieval = RetrievalService(db, vectors, embeddings)
    ingestion = IngestionService(
        db, vectors, embeddings, config.data_dir / "uploads", config.max_upload_mb * 1024 * 1024
    )
    connectors = ConnectorService(config, store, ingestion)
    rag = RagService(db, store, ollama, retrieval, config.history_messages, config.confirm_actions)
    return Services(config, db, store, vectors, ollama, ingestion, connectors,
                    VoiceService(config.whisper_model), rag, RateLimiter())


async def prune_orphans(sv: Services) -> None:
    """Delete vectors whose document is gone or never finished (e.g. a crash between write and 'ready')."""
    try:
        indexed = await asyncio.to_thread(sv.vectors.indexed_document_ids)
        keep = {d["id"] for d in sv.db.all("SELECT id FROM documents WHERE status IN ('ready','processing')")}
        for doc_id in indexed - keep:
            await asyncio.to_thread(sv.vectors.delete_document, doc_id)
            log.warning("removed orphan vectors for document %s", doc_id)
    except Exception:
        log.exception("orphan vector cleanup failed")


def create_app(config: AppConfig | None = None, ollama=None, run_scheduler: bool = True) -> FastAPI:
    config = config or AppConfig()
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sv = build_services(config, ollama)
        app.state.services = sv
        app.state.tasks = set()
        n = sv.db.fail_interrupted()
        if n:
            log.warning("marked %d interrupted document(s) as failed", n)
        bootstrap_admin(sv)
        prune = asyncio.create_task(prune_orphans(sv))
        app.state.tasks.add(prune)
        prune.add_done_callback(app.state.tasks.discard)
        for root in config.connector_root_list:  # every tenant gets its own private connector folder
            root.mkdir(parents=True, exist_ok=True)
            for org in sv.store.list_orgs():
                (root / org["slug"]).mkdir(parents=True, exist_ok=True)
        app.state.embed_origins = sv.store.all_embed_origins()
        if run_scheduler:
            sv.connectors.start()
        yield
        for task in list(app.state.tasks):  # background work must not outlive the database connection
            task.cancel()
        await asyncio.gather(*app.state.tasks, return_exceptions=True)
        await sv.connectors.stop()
        await sv.ingestion.wait_idle()
        if hasattr(sv.ollama, "aclose"):
            await sv.ollama.aclose()
        sv.db.close()

    # With auth on, don't publish the API schema to anonymous callers.
    docs = {} if not config.auth_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Local RAG", version="2.0.0", lifespan=lifespan, **docs)
    app.add_middleware(
        BodyLimitMiddleware,
        small_bytes=config.max_json_body_kb * 1024,
        large_bytes=max(config.max_upload_mb, 1) * 1024 * 1024 * 20,
        large_paths=("/documents/upload", "/voice/transcribe"),
    )
    app.state.embed_origins = set()
    app.add_middleware(  # added last = outermost, so even 413/429 responses carry CORS headers
        DynamicCORSMiddleware, static_origins=config.cors_origin_list,
        dynamic_origins=lambda: app.state.embed_origins,
    )
    register_handlers(app)
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(auth_router)
    app.include_router(tenancy_router)

    @app.get("/widget.js", include_in_schema=False)
    def widget_js():
        return FileResponse(STATIC / "widget.js", media_type="application/javascript")

    @app.get("/widget-demo", include_in_schema=False)
    def widget_demo():
        return FileResponse(STATIC / "widget-demo.html", media_type="text/html")

    return app


app = create_app()
