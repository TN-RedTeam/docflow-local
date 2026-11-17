"""Point d'entrée FastAPI de DocFlow Local."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from . import __version__
from .config import Settings, get_settings
from .database import build_engine, build_session_factory, init_database
from .llm_client import OllamaClient
from .models import STATUS_PROCESSING, ProcessedDocument
from .pipeline import process_document
from .auth import AuthMiddleware, ensure_admin, router as auth_router
from .routers import documents, meta
from .schema_registry import sync_schemas

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, llm_client: OllamaClient | None = None) -> FastAPI:
    """Fabrique l'application. `settings` et `llm_client` sont injectables pour les tests."""
    settings = settings or get_settings()
    settings.assert_local_llm()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings.database_url)
        init_database(engine, settings.init_sql_path)
        session_factory = build_session_factory(engine)
        with session_factory() as session:
            sync_schemas(session, settings.schemas_dir)

        llm = llm_client or OllamaClient(
            settings.ollama_base_url,
            settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
            max_retries=settings.ollama_max_retries,
            max_input_chars=settings.llm_max_input_chars,
        )
        executor = ThreadPoolExecutor(max_workers=settings.worker_concurrency, thread_name_prefix="docflow-worker")

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.llm = llm
        app.state.executor = executor

        # Reprise après arrêt : les documents restés « en traitement » sont remis en file.
        with session_factory() as session:
            pending = session.scalars(
                select(ProcessedDocument.id).where(ProcessedDocument.status == STATUS_PROCESSING)
            ).all()
        for document_id in pending:
            executor.submit(process_document, session_factory, llm, settings, document_id)
        if pending:
            logger.info("%d document(s) remis en file de traitement", len(pending))

        ensure_admin(session_factory, settings.admin_user, settings.admin_pass)

        yield

        executor.shutdown(wait=True, cancel_futures=True)
        if llm_client is None:
            llm.close()
        engine.dispose()

    app = FastAPI(
        title="DocFlow Local",
        version=__version__,
        description="Extraction, vérification et structuration de documents opérationnels, 100 % en local.",
        lifespan=lifespan,
    )
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "X-Document-Count"],
    )

    @app.exception_handler(OperationalError)
    async def database_unavailable(_request: Request, exc: OperationalError) -> JSONResponse:
        logger.error("Base de données indisponible : %s", exc.orig)
        return JSONResponse(
            status_code=503,
            content={"detail": "Base de données indisponible. Réessayez dans un instant."},
            headers={"Retry-After": "5"},
        )

    app.include_router(auth_router)
    app.include_router(meta.router)
    app.include_router(documents.router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def app_factory() -> FastAPI:
    """Cible uvicorn : `uvicorn app.main:app_factory --factory`."""
    return create_app()
