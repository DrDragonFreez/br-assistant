"""Конфигурация Celery.

В воркере отдельный event loop: Celery синхронный, а весь прикладной код
асинхронный. Вместо ``asyncio.run`` на каждый вызов (это пересоздаёт пулы
соединений к БД и Qdrant при каждой задаче) используется один долгоживущий
loop на процесс воркера — см. :func:`run_async`.

Очереди разделены по характеру нагрузки: индексация занимает GPU на минуты,
генерация документов — секунды, оценка качества — десятки минут. Смешивать
их в одной очереди значит блокировать быстрые задачи медленными.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

T = TypeVar("T")

celery_app = Celery(
    "br_assistant",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks", "app.workers.schedules"],
)

celery_app.conf.update(
    # ── Сериализация ──
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.TZ,
    enable_utc=True,
    # ── Надёжность ──
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=50,
    # ── Таймауты ──
    task_soft_time_limit=1800,
    task_time_limit=2400,
    result_expires=86400,
    # ── Очереди ──
    task_default_queue="default",
    task_routes={
        "app.workers.tasks.index_document": {"queue": "indexing"},
        "app.workers.tasks.reindex_knowledge_base": {"queue": "indexing"},
        "app.workers.tasks.generate_document": {"queue": "documents"},
        "app.workers.tasks.validate_document": {"queue": "documents"},
        "app.workers.tasks.run_quality_eval": {"queue": "eval"},
        "app.workers.tasks.cleanup_expired_documents": {"queue": "default"},
        "app.workers.tasks.refresh_kb_metrics": {"queue": "default"},
        "app.workers.tasks.archive_stale_sessions": {"queue": "default"},
    },
)

_loop: asyncio.AbstractEventLoop | None = None


def get_loop() -> asyncio.AbstractEventLoop:
    """Возвращает event loop процесса воркера.

    Returns:
        Долгоживущий loop, общий для всех задач этого процесса.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Выполняет корутину в event loop воркера.

    Args:
        coro: Асинхронная операция.

    Returns:
        Результат выполнения корутины.
    """
    return get_loop().run_until_complete(coro)


@worker_process_init.connect
def init_worker_process(**_kwargs: Any) -> None:
    """Инициализирует процесс воркера: логирование и каталоги."""
    configure_logging()
    settings.ensure_dirs()
    get_loop()
    logger.info("celery_worker_initialized")


@worker_process_shutdown.connect
def shutdown_worker_process(**_kwargs: Any) -> None:
    """Корректно закрывает соединения при остановке процесса.

    Дополнительно убирает файлы метрик завершившегося процесса: при
    ``worker_max_tasks_per_child`` дочерние процессы регулярно сменяются,
    и без этого их значения продолжали бы попадать в выдачу Prometheus.
    """
    from app.core.metrics import drop_process_metrics
    from app.db.postgres import close_postgres
    from app.db.qdrant_client import close_qdrant
    from app.db.redis import close_redis
    from app.rag.embedder import close_embedder
    from app.rag.generator import close_generator
    from app.rag.reranker import close_reranker

    async def _close_all() -> None:
        """Закрывает все клиенты."""
        await close_generator()
        await close_embedder()
        await close_reranker()
        await close_qdrant()
        await close_redis()
        await close_postgres()

    try:
        run_async(_close_all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker_shutdown_error", error=str(exc))

    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
        _loop = None

    drop_process_metrics()
    logger.info("celery_worker_shutdown")
