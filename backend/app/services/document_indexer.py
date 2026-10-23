"""Индексация документов базы знаний.

Полный цикл: загрузка файла → нарезка с привязкой к пунктам → построение
плотных и разреженных векторов → запись в Qdrant и зеркалирование фрагментов
в PostgreSQL → активация версии.

Фрагменты дублируются в PostgreSQL сознательно: цитаты в ответах строятся
по точному тексту пункта, и эти данные должны переживать переиндексацию
векторного хранилища.

Операция длительная (минуты на крупную инструкцию), поэтому вызывается
из Celery, а не из обработчика HTTP-запроса.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import models
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import DocumentParseError
from app.core.logging import get_logger
from app.core.metrics import indexing_duration_seconds, kb_chunks_indexed
from app.core.rbac import AccessLevel
from app.db.models import DocumentChunk, DocumentVersion, KnowledgeDocument
from app.db.qdrant_client import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    count_points,
    delete_version_points,
    ensure_collection,
    get_qdrant,
)
from app.db.redis import cache_invalidate_answers
from app.rag.bm25 import get_bm25_encoder, reset_bm25_encoder
from app.rag.chunker import get_chunker
from app.rag.embedder import get_embedder
from app.rag.loader import load_document
from app.rag.versioning import activate_version, create_version, get_document_by_code

logger = get_logger(__name__)

UPLOAD_BATCH_SIZE = 64


class DocumentIndexer:
    """Индексация нормативных документов в векторное хранилище."""

    def __init__(self, collection: str | None = None) -> None:
        """Инициализирует индексатор.

        Args:
            collection: Имя коллекции Qdrant.
        """
        self.collection = collection or settings.QDRANT_COLLECTION

    async def index_version(
        self,
        session: AsyncSession,
        *,
        version: DocumentVersion,
        document: KnowledgeDocument,
        file_path: Path,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Индексирует версию документа.

        Args:
            session: Сессия БД.
            version: Версия документа.
            document: Документ базы знаний.
            file_path: Путь к исходному файлу.
            activate: Активировать версию после индексации.

        Returns:
            Сводка по результату индексации.

        Raises:
            DocumentParseError: Документ не удалось обработать.
        """
        started = time.perf_counter()
        version.status = "indexing"
        version.indexing_error = ""
        await session.flush()

        try:
            await ensure_collection(self.collection)

            loaded = await load_document(file_path)
            pages = [(page.page_number, page.text) for page in loaded.pages]
            chunks = get_chunker().split(pages)

            if not chunks:
                raise DocumentParseError(
                    "Документ не содержит пригодных для индексации фрагментов"
                )

            texts = [chunk.content for chunk in chunks]

            dense_vectors = await get_embedder().embed_documents(texts)
            if len(dense_vectors) != len(chunks):
                raise DocumentParseError(
                    "Количество векторов не совпадает с количеством фрагментов"
                )

            encoder = get_bm25_encoder()
            if settings.HYBRID_SEARCH_ENABLED:
                if encoder.is_fitted:
                    encoder.update(texts)
                else:
                    encoder.fit(texts)
                encoder.save()

            await delete_version_points(str(version.id), collection=self.collection)
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.version_id == version.id)
            )

            points: list[models.PointStruct] = []
            db_chunks: list[DocumentChunk] = []

            for chunk, dense in zip(chunks, dense_vectors, strict=True):
                point_id = uuid.uuid4()

                payload: dict[str, Any] = {
                    "chunk_id": str(point_id),
                    "content": chunk.content,
                    "clause_number": chunk.clause_number,
                    "section_title": chunk.section_title,
                    "breadcrumbs": chunk.breadcrumbs,
                    "page_number": chunk.page_number,
                    "annex_number": chunk.annex_number,
                    "document_id": str(document.id),
                    "document_code": document.code,
                    "document_title": document.title,
                    "doc_type": document.doc_type,
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                    "version_label": version.version_label,
                    "is_active_version": activate,
                    "access_level": document.access_level.value,
                    **chunk.extra,
                }

                vectors: dict[str, Any] = {DENSE_VECTOR_NAME: dense}
                if settings.HYBRID_SEARCH_ENABLED:
                    sparse = encoder.encode_document(chunk.content)
                    if not sparse.is_empty:
                        vectors[SPARSE_VECTOR_NAME] = models.SparseVector(
                            indices=sparse.indices, values=sparse.values
                        )

                points.append(
                    models.PointStruct(id=str(point_id), vector=vectors, payload=payload)
                )
                db_chunks.append(
                    DocumentChunk(
                        id=point_id,
                        version_id=version.id,
                        qdrant_point_id=point_id,
                        chunk_index=chunk.index,
                        clause_number=chunk.clause_number,
                        section_title=chunk.section_title,
                        breadcrumbs=chunk.breadcrumbs,
                        page_number=chunk.page_number,
                        annex_number=chunk.annex_number,
                        content=chunk.content,
                        content_tokens=chunk.tokens,
                        access_level=AccessLevel(document.access_level.value),
                        extra=chunk.extra,
                    )
                )

            client = get_qdrant()
            for start in range(0, len(points), UPLOAD_BATCH_SIZE):
                await client.upsert(
                    collection_name=self.collection,
                    points=points[start : start + UPLOAD_BATCH_SIZE],
                    wait=True,
                )

            session.add_all(db_chunks)

            version.status = "indexed"
            version.chunks_count = len(chunks)
            version.pages_count = loaded.pages_count
            version.indexed_at = datetime.now(UTC)
            await session.flush()

            if activate:
                await activate_version(session, version.id)

            await cache_invalidate_answers()

            duration = time.perf_counter() - started
            indexing_duration_seconds.observe(duration)
            total_points = await count_points(collection=self.collection)
            kb_chunks_indexed.labels(collection=self.collection).set(total_points)

            summary = {
                "document_code": document.code,
                "version_number": version.version_number,
                "chunks": len(chunks),
                "pages": loaded.pages_count,
                "with_clause": sum(1 for c in chunks if c.clause_number),
                "activated": activate,
                "duration_seconds": round(duration, 1),
            }
            logger.info("version_indexed", **summary)
            return summary

        except Exception as exc:
            version.status = "failed"
            version.indexing_error = str(exc)[:2000]
            await session.flush()
            logger.exception("indexing_failed", document_code=document.code)
            raise

    async def index_file(
        self,
        session: AsyncSession,
        *,
        document_code: str,
        file_path: str | Path,
        uploaded_by: uuid.UUID | None = None,
        version_label: str = "",
        changelog: str = "",
        activate: bool = True,
    ) -> dict[str, Any]:
        """Загружает файл как новую версию документа и индексирует её.

        Args:
            session: Сессия БД.
            document_code: Код документа базы знаний.
            file_path: Путь к файлу.
            uploaded_by: Кто загрузил.
            version_label: Метка редакции.
            changelog: Описание изменений.
            activate: Активировать версию после индексации.

        Returns:
            Сводка по результату индексации.
        """
        path = Path(file_path)
        document = await get_document_by_code(session, document_code)
        loaded = await load_document(path)

        version, created = await create_version(
            session,
            document=document,
            content_hash=loaded.content_hash,
            source_filename=path.name,
            source_path=str(path),
            pages_count=loaded.pages_count,
            uploaded_by=uploaded_by,
            version_label=version_label,
            changelog=changelog,
        )

        if not created and version.status == "indexed":
            logger.info(
                "indexing_skipped_duplicate",
                document_code=document_code,
                version_number=version.version_number,
            )
            return {
                "document_code": document_code,
                "version_number": version.version_number,
                "skipped": True,
                "reason": "Файл с таким содержимым уже проиндексирован",
            }

        return await self.index_version(
            session,
            version=version,
            document=document,
            file_path=path,
            activate=activate,
        )

    async def reindex_all(
        self, session: AsyncSession, *, rebuild_vocabulary: bool = True
    ) -> dict[str, Any]:
        """Переиндексирует все действующие версии документов.

        Словарь BM25 пересобирается заново: после накопления изменений
        инкрементальные обновления перестают отражать реальную статистику корпуса.

        Args:
            session: Сессия БД.
            rebuild_vocabulary: Пересобрать словарь BM25 с нуля.

        Returns:
            Сводка по всем переиндексированным документам.
        """
        if rebuild_vocabulary:
            reset_bm25_encoder()

        versions = await session.scalars(
            select(DocumentVersion).where(DocumentVersion.is_active.is_(True))
        )

        results: list[dict[str, Any]] = []
        failed: list[str] = []

        for version in list(versions):
            document = await session.get(KnowledgeDocument, version.document_id)
            if document is None or not version.source_path:
                continue
            try:
                results.append(
                    await self.index_version(
                        session,
                        version=version,
                        document=document,
                        file_path=Path(version.source_path),
                        activate=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{document.code}: {exc}")
                logger.error("reindex_document_failed", document_code=document.code)

        logger.info("reindex_completed", succeeded=len(results), failed=len(failed))
        return {
            "indexed": results,
            "failed": failed,
            "total_chunks": sum(item.get("chunks", 0) for item in results),
        }


_indexer: DocumentIndexer | None = None


def get_indexer() -> DocumentIndexer:
    """Возвращает singleton-индексатор.

    Returns:
        Общий экземпляр :class:`DocumentIndexer`.
    """
    global _indexer
    if _indexer is None:
        _indexer = DocumentIndexer()
    return _indexer
