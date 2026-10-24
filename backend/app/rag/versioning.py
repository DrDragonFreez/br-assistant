"""Управление версиями документов базы знаний.

Требование слайда 4: решение должно предусматривать версионность и возможность
актуализации документов. Реализовано так:

* загрузка нового файла создаёт **новую версию**, старая не удаляется;
* если содержимое совпадает по хешу — версия не плодится;
* активная версия ровно одна; переключение атомарно и в PostgreSQL,
  и в Qdrant (через флаг ``is_active_version`` в payload);
* откат к предыдущей редакции выполняется без переиндексации;
* при активации новой версии сбрасывается кеш ответов.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.metrics import kb_active_version
from app.db.models import DocumentVersion, KnowledgeDocument
from app.db.qdrant_client import delete_version_points, set_version_active_flag
from app.db.redis import cache_invalidate_answers

logger = get_logger(__name__)


async def get_document_by_code(session: AsyncSession, code: str) -> KnowledgeDocument:
    """Находит документ базы знаний по коду.

    Args:
        session: Сессия БД.
        code: Код документа (например, ``INSTR-DOC``).

    Returns:
        Найденный документ.

    Raises:
        NotFoundError: Документ не зарегистрирован.
    """
    document = await session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.code == code)
    )
    if document is None:
        raise NotFoundError(f"Документ с кодом «{code}» не зарегистрирован в базе знаний")
    return document


async def find_version_by_hash(
    session: AsyncSession, document_id: uuid.UUID, content_hash: str
) -> DocumentVersion | None:
    """Ищет версию с тем же содержимым.

    Защита от повторной загрузки одного и того же файла.

    Args:
        session: Сессия БД.
        document_id: Идентификатор документа.
        content_hash: Хеш содержимого.

    Returns:
        Найденная версия или None.
    """
    return await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.content_hash == content_hash,
        )
    )


async def next_version_number(session: AsyncSession, document_id: uuid.UUID) -> int:
    """Вычисляет номер следующей версии.

    Args:
        session: Сессия БД.
        document_id: Идентификатор документа.

    Returns:
        Номер новой версии (начиная с 1).
    """
    last = await session.scalar(
        select(DocumentVersion.version_number)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )
    return int(last or 0) + 1


async def create_version(
    session: AsyncSession,
    *,
    document: KnowledgeDocument,
    content_hash: str,
    source_filename: str,
    source_path: str,
    pages_count: int,
    uploaded_by: uuid.UUID | None = None,
    version_label: str = "",
    changelog: str = "",
    effective_from: datetime | None = None,
) -> tuple[DocumentVersion, bool]:
    """Создаёт новую версию документа.

    Args:
        session: Сессия БД.
        document: Документ базы знаний.
        content_hash: Хеш содержимого файла.
        source_filename: Имя исходного файла.
        source_path: Путь к сохранённому файлу.
        pages_count: Количество страниц.
        uploaded_by: Кто загрузил.
        version_label: Человекочитаемая метка («ред. от 01.03.2026»).
        changelog: Описание изменений относительно предыдущей редакции.
        effective_from: Дата вступления редакции в силу.

    Returns:
        Кортеж ``(версия, создана_ли_новая)``. Если файл с таким содержимым
        уже загружен, возвращается существующая версия и ``False``.
    """
    existing = await find_version_by_hash(session, document.id, content_hash)
    if existing is not None:
        logger.info(
            "version_duplicate_skipped",
            document_code=document.code,
            version_number=existing.version_number,
        )
        return existing, False

    number = await next_version_number(session, document.id)
    version = DocumentVersion(
        document_id=document.id,
        version_number=number,
        version_label=version_label or f"ред. {number}",
        source_filename=source_filename,
        source_path=source_path,
        content_hash=content_hash,
        pages_count=pages_count,
        uploaded_by=uploaded_by,
        changelog=changelog,
        effective_from=effective_from or datetime.now(UTC),
        status="pending",
        is_active=False,
    )
    session.add(version)
    await session.flush()

    logger.info(
        "version_created",
        document_code=document.code,
        version_number=number,
        pages=pages_count,
    )
    return version, True


async def activate_version(session: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
    """Делает версию действующей.

    Порядок операций важен: сначала снимается флаг со старых версий, затем
    ставится на новую — и в PostgreSQL, и в Qdrant. Между этими шагами
    кратковременно активных версий нет, что безопаснее, чем две активные
    одновременно (иначе поиск вернул бы противоречивые требования).

    Args:
        session: Сессия БД.
        version_id: Идентификатор активируемой версии.

    Returns:
        Активированная версия.

    Raises:
        NotFoundError: Версия не найдена.
        ConflictError: Версия ещё не проиндексирована.
    """
    version = await session.get(DocumentVersion, version_id)
    if version is None:
        raise NotFoundError("Версия документа не найдена")
    if version.status != "indexed":
        raise ConflictError(
            "Версию нельзя активировать: она ещё не проиндексирована",
            context={"status": version.status},
        )

    previous = await session.scalars(
        select(DocumentVersion).where(
            DocumentVersion.document_id == version.document_id,
            DocumentVersion.is_active.is_(True),
            DocumentVersion.id != version_id,
        )
    )
    previous_versions = list(previous)

    if previous_versions:
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id.in_([item.id for item in previous_versions]))
            .values(is_active=False, status="archived", effective_to=datetime.now(UTC))
        )
        for item in previous_versions:
            await set_version_active_flag(str(item.id), is_active=False)

    version.is_active = True
    version.effective_from = version.effective_from or datetime.now(UTC)
    await session.flush()
    await set_version_active_flag(str(version.id), is_active=True)

    await cache_invalidate_answers()

    document = await session.get(KnowledgeDocument, version.document_id)
    if document is not None:
        kb_active_version.labels(document_code=document.code).set(version.version_number)

    logger.info(
        "version_activated",
        version_number=version.version_number,
        replaced=len(previous_versions),
    )
    return version


async def rollback_to_version(
    session: AsyncSession, document_id: uuid.UUID, version_number: int
) -> DocumentVersion:
    """Возвращает документ к указанной редакции.

    Переиндексация не требуется: фрагменты всех версий остаются в Qdrant,
    меняется только флаг действующей редакции.

    Args:
        session: Сессия БД.
        document_id: Идентификатор документа.
        version_number: Номер редакции, к которой откатываемся.

    Returns:
        Активированная версия.

    Raises:
        NotFoundError: Указанной редакции не существует.
    """
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == version_number,
        )
    )
    if version is None:
        raise NotFoundError(f"Редакция № {version_number} не найдена")

    if version.status == "archived":
        version.status = "indexed"
        version.effective_to = None
        await session.flush()

    activated = await activate_version(session, version.id)
    logger.warning("version_rollback", version_number=version_number)
    return activated


async def list_versions(
    session: AsyncSession, document_id: uuid.UUID
) -> list[DocumentVersion]:
    """Возвращает историю редакций документа.

    Args:
        session: Сессия БД.
        document_id: Идентификатор документа.

    Returns:
        Версии от новой к старой.
    """
    result = await session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return list(result)


async def delete_version(session: AsyncSession, version_id: uuid.UUID) -> None:
    """Удаляет версию документа вместе с её векторами.

    Args:
        session: Сессия БД.
        version_id: Идентификатор версии.

    Raises:
        NotFoundError: Версия не найдена.
        ConflictError: Попытка удалить действующую редакцию.
    """
    version = await session.get(DocumentVersion, version_id)
    if version is None:
        raise NotFoundError("Версия документа не найдена")
    if version.is_active:
        raise ConflictError(
            "Нельзя удалить действующую редакцию. Сначала активируйте другую версию."
        )

    await delete_version_points(str(version.id))
    await session.delete(version)
    logger.warning("version_deleted", version_number=version.version_number)
