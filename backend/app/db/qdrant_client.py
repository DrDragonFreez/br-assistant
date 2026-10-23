"""Клиент Qdrant и управление коллекцией базы знаний.

Коллекция хранит два вектора на точку: плотный (bge-m3, 1024 измерения) и
разреженный (BM25-подобные веса). Гибридный поиск объединяет их через
Reciprocal Rank Fusion на стороне Qdrant — это лучше работает на нормативных
текстах, где важны точные формулировки и номера пунктов.

Фильтрация по ``access_level`` применяется на уровне запроса: фрагменты,
недоступные роли пользователя, не попадают даже в контекст модели.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings
from app.core.exceptions import RetrievalError
from app.core.logging import get_logger

logger = get_logger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

_client: AsyncQdrantClient | None = None


def get_qdrant() -> AsyncQdrantClient:
    """Возвращает singleton-клиент Qdrant.

    Returns:
        Асинхронный клиент векторного хранилища.
    """
    global _client
    if _client is None:
        api_key = settings.QDRANT_API_KEY.get_secret_value() or None
        if settings.QDRANT_PREFER_GRPC:
            _client = AsyncQdrantClient(
                host=settings.QDRANT_HOST,
                grpc_port=settings.QDRANT_GRPC_PORT,
                prefer_grpc=True,
                api_key=api_key,
                timeout=30,
            )
        else:
            _client = AsyncQdrantClient(url=settings.QDRANT_URL, api_key=api_key, timeout=30)
    return _client


async def check_connection() -> bool:
    """Проверяет доступность Qdrant.

    Returns:
        True, если список коллекций получен.
    """
    try:
        await get_qdrant().get_collections()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant_healthcheck_failed", error=str(exc))
        return False


async def collection_exists(name: str | None = None) -> bool:
    """Проверяет существование коллекции.

    Args:
        name: Имя коллекции; по умолчанию из настроек.

    Returns:
        True, если коллекция создана.
    """
    collection = name or settings.QDRANT_COLLECTION
    try:
        return await get_qdrant().collection_exists(collection)
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(context={"error": str(exc)}) from exc


async def ensure_collection(name: str | None = None, *, recreate: bool = False) -> None:
    """Создаёт коллекцию с нужной схемой векторов и индексами payload.

    Идемпотентна: повторный вызов ничего не ломает.

    Args:
        name: Имя коллекции; по умолчанию из настроек.
        recreate: Удалить существующую коллекцию перед созданием.

    Raises:
        RetrievalError: Если Qdrant недоступен.
    """
    client = get_qdrant()
    collection = name or settings.QDRANT_COLLECTION

    try:
        exists = await client.collection_exists(collection)
        if exists and recreate:
            await client.delete_collection(collection)
            logger.warning("qdrant_collection_deleted", collection=collection)
            exists = False

        if not exists:
            await client.create_collection(
                collection_name=collection,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=settings.VECTOR_SIZE,
                        distance=models.Distance.COSINE,
                        on_disk=False,
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False)
                    )
                },
                optimizers_config=models.OptimizersConfigDiff(default_segment_number=2),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128),
            )
            logger.info("qdrant_collection_created", collection=collection)

        payload_indexes: dict[str, models.PayloadSchemaType] = {
            "document_code": models.PayloadSchemaType.KEYWORD,
            "document_id": models.PayloadSchemaType.KEYWORD,
            "version_id": models.PayloadSchemaType.KEYWORD,
            "version_number": models.PayloadSchemaType.INTEGER,
            "is_active_version": models.PayloadSchemaType.BOOL,
            "access_level": models.PayloadSchemaType.KEYWORD,
            "clause_number": models.PayloadSchemaType.KEYWORD,
            "clauses": models.PayloadSchemaType.KEYWORD,
            "doc_type": models.PayloadSchemaType.KEYWORD,
        }
        for field, schema in payload_indexes.items():
            try:
                await client.create_payload_index(
                    collection_name=collection,
                    field_name=field,
                    field_schema=schema,
                    wait=True,
                )
            except Exception:  # noqa: BLE001
                continue

        try:
            await client.create_payload_index(
                collection_name=collection,
                field_name="content",
                field_schema=models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.MULTILINGUAL,
                    min_token_len=2,
                    max_token_len=24,
                    lowercase=True,
                ),
                wait=True,
            )
        except Exception:  # noqa: BLE001
            pass

    except RetrievalError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(
            "Не удалось подготовить коллекцию базы знаний", context={"error": str(exc)}
        ) from exc


def build_access_filter(
    allowed_levels: list[str],
    *,
    document_codes: list[str] | None = None,
    only_active_versions: bool = True,
    version_id: str | None = None,
) -> models.Filter:
    """Собирает фильтр поиска с учётом прав доступа и версионности.

    Args:
        allowed_levels: Грифы, доступные роли пользователя.
        document_codes: Ограничение по конкретным документам.
        only_active_versions: Искать только в действующей редакции.
        version_id: Поиск в конкретной версии (для сравнения редакций).

    Returns:
        Фильтр Qdrant, применяемый к запросу.
    """
    conditions: list[models.Condition] = [
        models.FieldCondition(key="access_level", match=models.MatchAny(any=allowed_levels))
    ]
    if version_id:
        conditions.append(
            models.FieldCondition(key="version_id", match=models.MatchValue(value=version_id))
        )
    elif only_active_versions:
        conditions.append(
            models.FieldCondition(key="is_active_version", match=models.MatchValue(value=True))
        )
    if document_codes:
        conditions.append(
            models.FieldCondition(key="document_code", match=models.MatchAny(any=document_codes))
        )
    return models.Filter(must=conditions)


async def count_points(*, collection: str | None = None, only_active: bool = True) -> int:
    """Считает количество точек в коллекции.

    Args:
        collection: Имя коллекции; по умолчанию из настроек.
        only_active: Учитывать только действующие редакции.

    Returns:
        Количество точек; 0 при ошибке.
    """
    name = collection or settings.QDRANT_COLLECTION
    try:
        query_filter = (
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="is_active_version", match=models.MatchValue(value=True)
                    )
                ]
            )
            if only_active
            else None
        )
        result = await get_qdrant().count(
            collection_name=name, count_filter=query_filter, exact=True
        )
        return int(result.count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant_count_failed", error=str(exc))
        return 0


async def delete_version_points(version_id: str, *, collection: str | None = None) -> None:
    """Удаляет точки, относящиеся к версии документа.

    Используется при переиндексации, чтобы не плодить дубликаты.

    Args:
        version_id: Идентификатор версии.
        collection: Имя коллекции; по умолчанию из настроек.
    """
    name = collection or settings.QDRANT_COLLECTION
    await get_qdrant().delete(
        collection_name=name,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="version_id", match=models.MatchValue(value=version_id)
                    )
                ]
            )
        ),
        wait=True,
    )
    logger.info("qdrant_version_points_deleted", version_id=version_id)


async def set_version_active_flag(
    version_id: str, *, is_active: bool, collection: str | None = None
) -> None:
    """Переключает признак действующей редакции у точек версии.

    Позволяет активировать новую редакцию инструкции без переиндексации
    остальных документов.

    Args:
        version_id: Идентификатор версии.
        is_active: Новое значение признака.
        collection: Имя коллекции; по умолчанию из настроек.
    """
    name = collection or settings.QDRANT_COLLECTION
    await get_qdrant().set_payload(
        collection_name=name,
        payload={"is_active_version": is_active},
        points=models.Filter(
            must=[
                models.FieldCondition(key="version_id", match=models.MatchValue(value=version_id))
            ]
        ),
        wait=True,
    )
    logger.info("qdrant_version_flag_updated", version_id=version_id, is_active=is_active)


async def collection_stats(collection: str | None = None) -> dict[str, Any]:
    """Возвращает статистику коллекции для админки и метрик.

    Args:
        collection: Имя коллекции; по умолчанию из настроек.

    Returns:
        Словарь со статусом, числом точек и сегментов.
    """
    name = collection or settings.QDRANT_COLLECTION
    try:
        info = await get_qdrant().get_collection(name)
        return {
            "collection": name,
            "status": str(info.status),
            "points_count": info.points_count or 0,
            "segments_count": info.segments_count or 0,
            "vectors_count": info.vectors_count or 0,
        }
    except Exception as exc:  # noqa: BLE001
        return {"collection": name, "status": "unavailable", "error": str(exc)}


async def lifespan_qdrant() -> None:
    """Готовит коллекцию при старте приложения."""
    if not await check_connection():
        logger.error("qdrant_unavailable_at_startup")
        return
    await ensure_collection()
    stats = await collection_stats()
    logger.info("qdrant_ready", **stats)


async def close_qdrant() -> None:
    """Закрывает соединение с Qdrant."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("qdrant_disconnected")
