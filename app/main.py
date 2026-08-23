import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.commissioning import CommissioningService
from app.compliance import ComplianceService
from app.embeddings import FastEmbedder
from app.procurement import ProcurementRiskService
from app.schedule import ScheduleService
from app.database import check_database, create_database_engine, create_session_factory, initialize_database
from app.graph import GraphStore
from app.ingestion import IngestionError
from app.impact_chain import ImpactChainService
from app.workflow import KnowledgeService, build_workflow

logger = logging.getLogger("atlas")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    app.state.db_engine = create_database_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    if settings.auto_create_schema:
        await initialize_database(app.state.db_engine)
    app.state.qdrant = AsyncQdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key, check_compatibility=False
    )
    app.state.settings = settings
    app.state.embedder = FastEmbedder(
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_cache_dir,
    )
    app.state.graph_store = GraphStore(settings.graph_dir)
    app.state.knowledge_service = KnowledgeService(settings, app.state.qdrant, app.state.embedder)
    app.state.compliance_service = ComplianceService(settings)
    app.state.schedule_service = ScheduleService(settings)
    app.state.commissioning_service = CommissioningService(settings)
    app.state.impact_chain_service = ImpactChainService(
        app.state.schedule_service, app.state.commissioning_service
    )
    app.state.procurement_service = ProcurementRiskService()
    app.state.workflow = build_workflow()
    logger.info("startup environment=%s", settings.environment)
    try:
        yield
    finally:
        await app.state.qdrant.close()
        await app.state.db_engine.dispose()
        logger.info("shutdown")


app = FastAPI(title="Project Atlas", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def error_response(status_code: int, code: str, message: str, details: object | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(422, "validation_error", "Request validation failed", exc.errors())


@app.exception_handler(IngestionError)
async def ingestion_error(_: Request, exc: IngestionError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, getattr(exc, "details", None))


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error type=%s", type(exc).__name__)
    return error_response(500, "internal_error", "An unexpected error occurred")


async def check_qdrant(client: AsyncQdrantClient) -> None:
    await client.get_collections()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "components": {"api": "ok"}}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks = await asyncio.gather(
        check_database(request.app.state.db_engine),
        check_qdrant(request.app.state.qdrant),
        return_exceptions=True,
    )
    components = {
        "api": "ok",
        "database": "ok" if not isinstance(checks[0], Exception) else "error",
        "qdrant": "ok" if not isinstance(checks[1], Exception) else "error",
    }
    healthy = all(value == "ok" for value in components.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "components": components},
    )


from app.api import benchmark_router, evaluation_router, mitigation_router, router as project_router

app.include_router(project_router)
app.include_router(evaluation_router)
app.include_router(mitigation_router)
app.include_router(benchmark_router)
