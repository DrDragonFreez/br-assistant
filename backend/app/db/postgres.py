"""Асинхронное подключение к PostgreSQL.

Движок создаётся один раз на процесс. В FastAPI сессия отдаётся через
зависимость :func:`get_session`, в Celery-задачах — через контекстный
менеджер :func:`session_scope`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Возвращает (и при необходимости создаёт) движок SQLAlchemy.

    Returns:
        Асинхронный движок, привязанный к ``DATABASE_URL``.
    """
    global _engine
    if _engine is None:
        assert settings.DATABASE_URL, "DATABASE_URL не сконфигурирован"
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=settings.DB_ECHO,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
            connect_args={"timeout": 10, "command_timeout": 60},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Возвращает фабрику сессий.

    Returns:
        Фабрика, создающая сессии без автосброса и без истечения объектов
        после commit (иначе Pydantic не сможет прочитать поля ORM-записи).
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: сессия БД на время обработки запроса.

    Транзакция фиксируется автоматически при успешном завершении обработчика
    и откатывается при любом исключении.

    Yields:
        Активная сессия.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер сессии для воркеров и скриптов.

    Yields:
        Активная сессия с автоматическим commit/rollback.

    Example:
        >>> async with session_scope() as session:
        ...     session.add(record)
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_connection() -> bool:
    """Проверяет доступность БД для health-эндпоинта.

    Returns:
        True, если простой запрос выполнился успешно.
    """
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 — любая ошибка означает недоступность
        logger.warning("postgres_healthcheck_failed", error=str(exc))
        return False


async def lifespan_postgres() -> None:
    """Инициализирует пул соединений при старте приложения.

    Raises:
        RuntimeError: Если БД недоступна — лучше упасть на старте, чем
            отдавать 500 на каждый запрос.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    logger.info("postgres_connected", pool_size=settings.DB_POOL_SIZE)


async def close_postgres() -> None:
    """Закрывает пул соединений при остановке приложения."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("postgres_disconnected")
