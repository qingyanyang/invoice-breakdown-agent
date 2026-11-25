import logging
from contextlib import asynccontextmanager

from alembic.util import CommandError
from anyio import CapacityLimiter
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app.allocations import router as allocation_router
from app.classifications import router as classification_router
from app.config import settings
from app.database import (
    DatabaseConfigurationError,
    create_application_engine,
    database_heads,
    repository_heads,
)
from app.delivery import router as delivery_router
from app.execution_api import router as execution_router
from app.extractions import router as extraction_router
from app.operators import operator_credentials
from app.qa_engine import QAEngine
from app.reconciliations import router as reconciliation_router
from app.source_storage import create_output_storage, create_source_storage
from app.tasks import router as task_router
from app.worker_io import BlockingWorker, SourceReadWorker
from app.workbooks import router as workbook_router
from app.workflows import router as workflow_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = None
    try:
        engine = create_application_engine(settings)
    except DatabaseConfigurationError as error:
        logger.warning("Database readiness unavailable: %s", error)

    app.state.database_engine = engine
    app.state.extraction_limiter = CapacityLimiter(1)
    app.state.extraction_source_reader = SourceReadWorker()
    app.state.storage_worker = BlockingWorker("workbook-storage", capacity=2)
    app.state.upload_validator = BlockingWorker("workbook-validation")
    app.state.qa_worker = BlockingWorker("workbook-qa")
    app.state.classification_limiter = CapacityLimiter(1)
    app.state.source_storage = None
    app.state.output_storage = None
    app.state.workbook_limiter = CapacityLimiter(1)
    app.state.qa_limiter = CapacityLimiter(1)
    app.state.qa_engine = QAEngine(settings.qa_docker_runtime, settings.qa_worker_image)
    try:
        app.state.operator_credentials = operator_credentials(settings.operator_tokens)
    except ValueError as error:
        logger.warning("Operator authentication unavailable: %s", error)
        app.state.operator_credentials = {}
    try:
        app.state.source_storage = create_source_storage(settings)
    except ValueError:
        logger.warning("Source storage configuration is invalid")
    try:
        app.state.output_storage = create_output_storage(settings)
    except ValueError:
        logger.warning("Workbook storage configuration is invalid")
    try:
        yield
    finally:
        await app.state.extraction_source_reader.close()
        await app.state.storage_worker.close()
        await app.state.upload_validator.close()
        await app.state.qa_worker.close()
        if app.state.source_storage is not None:
            app.state.source_storage.close()
        if app.state.output_storage is not None:
            app.state.output_storage.close()
        if engine is not None:
            await engine.dispose()


app = FastAPI(
    title="AFLUX Invoice Breakdown Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(task_router)
app.include_router(execution_router)
app.include_router(workflow_router)
app.include_router(extraction_router)
app.include_router(reconciliation_router)
app.include_router(classification_router)
app.include_router(allocation_router)
app.include_router(workbook_router)
app.include_router(delivery_router)

origins = [
    origin.strip()
    for origin in settings.cors_allowed_origins.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "knowledge-intelligence-api",
        "name": app.title,
        "environment": settings.environment,
    }


@app.get("/ready", responses={503: {"description": "Database readiness unavailable"}})
async def ready(request: Request, response: Response) -> dict[str, str]:
    engine = request.app.state.database_engine
    if engine is not None:
        try:
            expected = repository_heads()
            actual = await database_heads(engine, settings.postgres_schema)
        except CommandError:
            logger.warning(
                "Database readiness unavailable: repository migration heads could not be read"
            )
        except TimeoutError:
            logger.warning("Database readiness check timed out")
        except SQLAlchemyError as error:
            logger.warning("Database readiness check failed: %s", type(error).__name__)
        else:
            if expected and actual == expected:
                return {"status": "ready"}
            logger.warning(
                "Database readiness unavailable: migration revisions do not match repository heads"
            )

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready"}
