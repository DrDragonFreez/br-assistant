"""Точка входа приложения.

Порядок middleware существенен и задан снизу вверх по обработке запроса:
контекст логирования ставится первым, чтобы идентификатор запроса попал
во все последующие записи, включая обработчики ошибок.

Подключения к внешним сервисам инициализируются при старте и закрываются
при остановке: создавать их на каждый запрос значило бы терять пул
соединений и время на установление TLS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.deps import limiter
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Управляет жизненным циклом внешних подключений.

    Args:
        _app: Экземпляр приложения (не используется).

    Yields:
        Ничего; управление передаётся приложению между инициализацией
        и освобождением ресурсов.
    """
    from app.agents.graph import get_graph
    from app.db.postgres import close_postgres, lifespan_postgres
    from app.db.qdrant_client import close_qdrant, lifespan_qdrant
    from app.db.redis import close_redis, lifespan_redis
    from app.rag.bm25 import get_bm25_encoder
    from app.rag.embedder import close_embedder
    from app.rag.generator import close_generator
    from app.rag.reranker import close_reranker

    configure_logging()
    settings.ensure_dirs()

    logger.info(
        "application_starting",
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
        model=settings.OLLAMA_MODEL_ACTIVE,
    )

    await lifespan_postgres()
    await lifespan_redis()
    await lifespan_qdrant()

    encoder = get_bm25_encoder()
    if not encoder.is_fitted:
        logger.warning("bm25_vocabulary_empty", hint="Выполните индексацию: make index")

    get_graph()

    logger.info("application_started")

    yield

    logger.info("application_stopping")
    await close_generator()
    await close_embedder()
    await close_reranker()
    await close_qdrant()
    await close_redis()
    await close_postgres()
    logger.info("application_stopped")


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Формирует ответ при превышении частоты запросов.

    Args:
        request: HTTP-запрос.
        exc: Исключение ограничителя.

    Returns:
        Ответ со статусом 429.
    """
    request_id = getattr(request.state, "request_id", "")
    logger.warning("rate_limit_exceeded", path=request.url.path, limit=str(exc.detail))
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Слишком много запросов. Повторите через несколько секунд.",
            "code": "rate_limit_exceeded",
            "request_id": request_id,
        },
        headers={"Retry-After": "30"},
    )


def create_app() -> FastAPI:
    """Создаёт и настраивает приложение FastAPI.

    Returns:
        Настроенный экземпляр приложения.
    """
    app = FastAPI(
        title=settings.APP_NAME,
        description=(
            "Интеллектуальный помощник по внутренним регламентам и документам. "
            "Отвечает на вопросы со ссылками на конкретные положения инструкции, "
            "формирует черновики документов по типовым бланкам и проверяет "
            "готовые проекты на соответствие требованиям."
        ),
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url=f"{settings.API_V1_PREFIX}/docs",
        redoc_url=f"{settings.API_V1_PREFIX}/redoc",
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        root_path=settings.API_ROOT_PATH,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)  # type: ignore[arg-type]

    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=["backend", "localhost", "127.0.0.1", "nginx", "*.local"],
        )

    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    app.include_router(health_router)
    app.include_router(metrics_router)

    return app


app = create_app()
