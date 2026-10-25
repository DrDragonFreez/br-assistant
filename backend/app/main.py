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
from typing import Any

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging, get_logger

logger = get_logger(__name__)

# Эндпоинты, ответ которых уходит по мере формирования. Сжимать их
# нельзя: получатель разбирает события по одному, а сжатый поток
# читается только целиком.
STREAMING_PATHS = ("/chat/stream",)


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


class SkipCompressionMiddleware:
    """Снимает согласование сжатия с потоковых эндпоинтов.

    ``GZipMiddleware`` решает, сжимать ли ответ, по заголовку запроса —
    ещё до того, как известен тип содержимого. Для потока событий это
    решение всегда неверное: Starlette сжимает поток кусками, не
    сбрасывая буфер дефлятора, и получатель не может разобрать событие
    до накопления данных. Браузер показывает оборвавшийся ответ, хотя
    сервис продолжает присылать токены.

    Поэтому у таких запросов заголовок ``Accept-Encoding`` убирается
    до того, как его увидит ``GZipMiddleware``. Отключать сжатие
    целиком нельзя: на обычных ответах API оно полезно.

    Middleware добавляется ПОСЛЕ ``GZipMiddleware``: Starlette
    оборачивает приложение снаружи внутрь, и добавленный последним
    отрабатывает первым.
    """

    def __init__(self, app: Any, paths: tuple[str, ...]) -> None:
        """Сохраняет вложенное приложение и список путей.

        Args:
            app: Вложенное ASGI-приложение.
            paths: Окончания путей, для которых сжатие не применяется.
        """
        self.app = app
        self.paths = paths

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Пропускает запрос дальше, при необходимости сняв сжатие.

        Args:
            scope: Область запроса ASGI.
            receive: Канал получения событий.
            send: Канал отправки событий.
        """
        if scope.get("type") == "http" and scope.get("path", "").endswith(self.paths):
            scope = dict(scope)
            scope["headers"] = [
                (name, value)
                for name, value in scope["headers"]
                if name.lower() != b"accept-encoding"
            ]
        await self.app(scope, receive, send)


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


    # Ограничение частоты запросов целиком вынесено в Nginx: зоны
    # auth (5/мин), chat (20/мин), upload (10/мин) и общая api (60/мин).
    #
    # Библиотека slowapi из контура убрана. Её хранилище счётчиков —
    # синхронный клиент Redis внутри async-обработчика, что запрещено
    # правилами проекта. Кроме того, при недоступном Redis она отвечала
    # отказом на каждый запрос, а её обёртка над обработчиком ломала
    # разрешение аннотаций: FastAPI искал модели запроса в пространстве
    # имён slowapi и приложение не поднималось вовсе. Фильтрация на
    # границе контура надёжнее: она не зависит от состояния сервиса.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Добавляется после GZip, поэтому отрабатывает раньше него и успевает
    # снять согласование сжатия с потока событий.
    app.add_middleware(SkipCompressionMiddleware, paths=STREAMING_PATHS)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    # Фильтр заголовка Host настраивается, а по умолчанию выключен («*»).
    #
    # Жёсткий список имён в коде — ловушка для демонстрации: стоит открыть
    # сервис по адресу машины в локальной сети (http://192.168.1.5/), и
    # каждый запрос получает 400 «Invalid host header», хотя всё работает.
    # Ограничивать Host уместно на прокси, где известны реальные имена
    # контура; здесь список задаётся через TRUSTED_HOSTS, когда он нужен.
    trusted_hosts = settings.trusted_hosts
    if trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    app.include_router(health_router)
    app.include_router(metrics_router)

    return app


app = create_app()
