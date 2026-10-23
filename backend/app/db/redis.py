"""Асинхронный клиент Redis.

Redis используется для трёх задач: кеш ответов на повторяющиеся вопросы,
denylist отозванных JWT и хранение состояния незавершённых диалогов
(заполнение шаблона по шагам).
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import revocation_key

logger = get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None

CACHE_PREFIX = "cache:answer:"
SLOT_PREFIX = "slots:"


def get_redis() -> Redis:
    """Возвращает singleton-клиент Redis.

    Returns:
        Клиент, работающий поверх общего пула соединений.
    """
    global _pool, _client
    if _client is None:
        assert settings.REDIS_URL, "REDIS_URL не сконфигурирован"
        _pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=32,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
    return _client


async def check_connection() -> bool:
    """Проверяет доступность Redis.

    Returns:
        True, если сервер ответил на PING.
    """
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_healthcheck_failed", error=str(exc))
        return False


# ─────────────────────────── КЕШ ОТВЕТОВ ───────────────────────────
async def cache_get(key: str) -> dict[str, Any] | None:
    """Читает значение из кеша.

    Args:
        key: Ключ без префикса.

    Returns:
        Распакованный JSON или None, если записи нет либо Redis недоступен.
    """
    try:
        raw = await get_redis().get(f"{CACHE_PREFIX}{key}")
    except Exception as exc:  # noqa: BLE001 — кеш не критичен для работы
        logger.warning("cache_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return None


async def cache_set(key: str, value: dict[str, Any], ttl: int | None = None) -> None:
    """Кладёт значение в кеш.

    Args:
        key: Ключ без префикса.
        value: Сериализуемый в JSON объект.
        ttl: Время жизни в секундах; по умолчанию ``CACHE_TTL_SECONDS``.
    """
    try:
        await get_redis().set(
            f"{CACHE_PREFIX}{key}",
            json.dumps(value, ensure_ascii=False),
            ex=ttl or settings.CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_write_failed", error=str(exc))


async def cache_invalidate_answers() -> int:
    """Сбрасывает кеш ответов.

    Вызывается после переиндексации базы знаний: старые ответы могут
    ссылаться на неактуальную редакцию инструкции.

    Returns:
        Количество удалённых ключей.
    """
    client = get_redis()
    deleted = 0
    try:
        async for key in client.scan_iter(match=f"{CACHE_PREFIX}*", count=500):
            await client.delete(key)
            deleted += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_invalidate_failed", error=str(exc))
    logger.info("cache_invalidated", deleted=deleted)
    return deleted


# ─────────────────────────── ОТЗЫВ ТОКЕНОВ ───────────────────────────
async def revoke_token(jti: str, ttl_seconds: int) -> None:
    """Помещает токен в denylist.

    Args:
        jti: Идентификатор токена.
        ttl_seconds: Остаток жизни токена; запись самоудаляется по истечении.
    """
    await get_redis().set(revocation_key(jti), "1", ex=ttl_seconds)


async def is_token_revoked(jti: str) -> bool:
    """Проверяет, отозван ли токен.

    При недоступности Redis возвращает False: иначе любое падение кеша
    заблокировало бы вход всем пользователям.

    Args:
        jti: Идентификатор токена.

    Returns:
        True, если токен найден в denylist.
    """
    try:
        return bool(await get_redis().exists(revocation_key(jti)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("revocation_check_failed", error=str(exc))
        return False


# ─────────────────── СОСТОЯНИЕ ЗАПОЛНЕНИЯ ШАБЛОНА ───────────────────
async def slots_get(session_id: str) -> dict[str, Any]:
    """Читает накопленные поля шаблона для диалога.

    Args:
        session_id: Идентификатор сессии.

    Returns:
        Словарь заполненных полей; пустой, если ничего не сохранено.
    """
    try:
        raw = await get_redis().get(f"{SLOT_PREFIX}{session_id}")
    except Exception:  # noqa: BLE001
        return {}
    return json.loads(raw) if raw else {}  # type: ignore[no-any-return]


async def slots_set(session_id: str, slots: dict[str, Any]) -> None:
    """Сохраняет промежуточное состояние заполнения шаблона.

    Args:
        session_id: Идентификатор сессии.
        slots: Собранные от пользователя значения полей.
    """
    try:
        await get_redis().set(
            f"{SLOT_PREFIX}{session_id}",
            json.dumps(slots, ensure_ascii=False, default=str),
            ex=settings.SESSION_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slots_write_failed", error=str(exc))


async def slots_clear(session_id: str) -> None:
    """Очищает состояние заполнения после формирования документа.

    Args:
        session_id: Идентификатор сессии.
    """
    try:
        await get_redis().delete(f"{SLOT_PREFIX}{session_id}")
    except Exception:  # noqa: BLE001
        pass


async def lifespan_redis() -> None:
    """Проверяет соединение с Redis при старте приложения."""
    if await check_connection():
        logger.info("redis_connected")
    else:
        logger.error("redis_unavailable_at_startup")


async def close_redis() -> None:
    """Закрывает соединения с Redis."""
    global _client, _pool
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
    logger.info("redis_disconnected")
