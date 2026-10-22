"""FastAPI application factory with request IDs and structured logs."""

import asyncio
import json
from contextlib import asynccontextmanager
import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.api.workbench_management import router as management_router
from app.api.workbench_datasets import router as datasets_router
from app.api.workbench_routes import router as workbench_router
from app.api.workbench_session import router as workbench_session_router
from starlette.exceptions import HTTPException

from app.agent.provider import CompatibleAgentModel, UnconfiguredAgentModel
from app.agent.service import AgentService
from app.db.agent import AgentRepository
from app.api.agent_routes import router as agent_router
from app.api.routes import router
from app.core.config import Settings
from app.core.lifecycle import ChatAdmission
from app.core.errors import AppError
from app.db.conversations import ConversationRepository
from app.generation.provider import GenerationClient, make_generation_client
from app.generation.service import ChatService
from app.core.embedding import make_embedding_client
from app.db.vectors import VectorRepository
from app.retrieval.service import VectorService
from app.retrieval.pipeline import RetrievalService
from app.ingestion.service import DocumentService
from app.db.ingestion import IngestionRepository
from app.db.chunks import ChunkRepository
from app.db.documents import DocumentRepository
from app.ingestion.worker import IngestionWorker


logger = logging.getLogger("knowledgeops")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def error_response(code: str, message: str, request_id: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )


def create_app(settings: Settings | None = None,
               generation_client: GenerationClient | None = None, agent_model=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    repository = IngestionRepository(settings.database_url)
    chunk_repository = ChunkRepository(repository)
    conversations = ConversationRepository(repository)
    vector_repository = (VectorRepository(settings.vector_database_url, settings.embedding)
                         if settings.vector_database_url else None)
    vector_service = (VectorService(chunk_repository, vector_repository,
                                   make_embedding_client(settings.embedding), settings.embedding)
                      if vector_repository else None)
    worker = IngestionWorker(repository, settings.ingestion_lease_seconds,
                             settings.ingestion_max_attempts, settings.ingestion_poll_seconds)

    from app.retrieval.planning import QueryPlanner
    generation_client = generation_client or make_generation_client(settings.generation)
    retrieval_service = (RetrievalService(vector_service, settings.retrieval,
        planner=QueryPlanner(generation_client, settings.generation)) if vector_service else None)

    agent_repository = AgentRepository(settings.agent) if settings.agent.database_url else None
    persistent_agent = (AgentService(agent_repository, agent_model or (
        CompatibleAgentModel(settings.agent) if settings.agent.provider == "compatible" else UnconfiguredAgentModel()),
        settings.agent, retrieval_service) if agent_repository else None)

    from app.db.accounts import AccountRepository
    accounts = AccountRepository(settings.auth_database_url or settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(accounts.initialize)
        await asyncio.to_thread(repository.initialize)
        await asyncio.to_thread(chunk_repository.initialize)
        await asyncio.to_thread(conversations.initialize)
        if vector_repository:
            await asyncio.to_thread(vector_repository.initialize)
        if agent_repository:
            await asyncio.to_thread(agent_repository.initialize)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop)) if settings.ingestion_worker_enabled else None
        app.state.ingestion_stop = stop
        app.state.ingestion_task = task
        app.state.index_stop = asyncio.Event()
        app.state.index_task = asyncio.create_task(app.state.index_worker.run(app.state.index_stop)) if settings.index_worker_enabled else None
        try:
            yield
        finally:
            app.state.index_stop.set()
            if app.state.index_task:
                app.state.index_task.cancel()
                await asyncio.gather(app.state.index_task,return_exceptions=True)
            if app.state.persistent_agent:
                await app.state.persistent_agent.shutdown()
            app.state.ingestion_stop.set()
            if app.state.ingestion_task is not None:
                await app.state.ingestion_task

    app = FastAPI(title="KnowledgeOps Copilot", version="0.7.0", lifespan=lifespan)
    app.state.management_active_requests = 0
    app.state.management_changing = False
    app.state.chat_admission = ChatAdmission(settings.agent.concurrency, settings.agent.requests_per_minute)
    app.state.persistent_agent = persistent_agent
    app.state.vector_service = vector_service
    app.state.retrieval_service = retrieval_service
    app.state.vector_repository = vector_repository
    app.state.settings = settings
    app.state.accounts = accounts
    from app.db.index_jobs import IndexJobs
    from app.ingestion.index_worker import IndexWorker
    app.state.index_jobs = IndexJobs(repository)
    app.state.index_worker = IndexWorker(app)
    app.state.ingestion_repository = repository
    app.state.chunk_repository = chunk_repository
    app.state.document_repository = DocumentRepository(repository,accounts if settings.auth_mode=="accounts" else None)
    app.state.ingestion_worker = worker
    app.state.documents = DocumentService(settings.max_document_bytes, repository)
    app.state.conversations = conversations
    app.state.chat = ChatService(conversations, retrieval_service,
                                generation_client or make_generation_client(settings.generation),
                                settings.generation)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        if app.state.management_changing:
            return error_response('WORKSPACE_BUSY', '正在切换连接，请稍后重试。', request_id, 409)
        expected_workspace = request.headers.get('X-Knowledge-Workspace')
        if expected_workspace:
            from app.evals.imported import runtime_fingerprint
            if expected_workspace != runtime_fingerprint(app):
                return error_response('WORKSPACE_CHANGED', '连接或模型配置已变化，请刷新知识库后重新开始。', request_id, 409)
        app.state.management_active_requests += 1
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(json.dumps({"event": "request.failed", "request_id": request_id}))
            response = error_response("INTERNAL_ERROR", "Internal server error", request_id, 500)
        finally:
            app.state.management_active_requests -= 1
        response.headers["X-Request-ID"] = request_id
        if request.url.path != "/health":
            response.headers["Cache-Control"] = "no-store"
        logger.info(json.dumps({
            "event": "request.completed",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "user_id": getattr(request.state, "user_id", None),
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }, ensure_ascii=False))
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.code, exc.message, request.state.request_id, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response("VALIDATION_ERROR", "Invalid request", request.state.request_id, 422)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        message = "Not found" if exc.status_code == 404 else str(exc.detail)
        return error_response(code, message, request.state.request_id, exc.status_code)

    from app.api.document_versions import router as versions_router
    from app.api.auth import router as auth_router
    from app.api.index_jobs import router as index_router
    app.include_router(index_router)
    app.include_router(auth_router)
    app.include_router(versions_router)
    from app.api.document_reviews import router as reviews_router
    app.include_router(reviews_router)
    app.include_router(router)
    app.include_router(agent_router)
    app.include_router(workbench_router)
    app.include_router(datasets_router)
    app.include_router(management_router)
    app.include_router(workbench_session_router)
    app.mount("/workbench/assets", StaticFiles(directory=Path(__file__).parent / "web"), name="workbench-assets")
    return app


app = create_app()
