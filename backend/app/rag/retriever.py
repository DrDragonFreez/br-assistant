"""Гибридный поиск по базе знаний.

Запрос выполняется сразу по двум векторам — плотному (bge-m3) и разреженному
(BM25) — и результаты объединяются через Reciprocal Rank Fusion на стороне
Qdrant. Это даёт устойчивость и к перефразированным вопросам, и к точным
формулировкам с номерами пунктов.

Фильтр по уровню доступа применяется на уровне запроса: фрагменты, закрытые
для роли пользователя, не попадают в выдачу и, следовательно, не передаются
в языковую модель.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import models

from app.core.config import settings
from app.core.exceptions import KnowledgeBaseEmptyError, RetrievalError
from app.core.logging import get_logger
from app.core.metrics import observe_latency, retrieval_latency_seconds
from app.db.qdrant_client import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    build_access_filter,
    collection_exists,
    get_qdrant,
)
from app.rag.bm25 import get_bm25_encoder
from app.rag.embedder import get_embedder

logger = get_logger(__name__)

CLAUSE_MENTION_RE = re.compile(
    r"(?:п\.?|пункт[а-я]*|подпункт[а-я]*)\s*№?\s*(\d{1,2}(?:\.\d{1,3}){0,4})", re.IGNORECASE
)
ANNEX_MENTION_RE = re.compile(r"приложени[а-я]+\s*№?\s*(\d+|[IVXL]+)", re.IGNORECASE)


@dataclass(slots=True)
class RetrievedChunk:
    """Найденный фрагмент базы знаний.

    Attributes:
        chunk_id: Идентификатор фрагмента в PostgreSQL.
        point_id: Идентификатор точки в Qdrant.
        content: Текст фрагмента.
        score: Оценка релевантности (0–1 после реранжирования).
        clause_number: Номер пункта инструкции.
        section_title: Заголовок раздела.
        breadcrumbs: Структурный путь до фрагмента.
        document_code: Код документа базы знаний.
        document_title: Название документа.
        version_label: Метка редакции.
        page_number: Номер страницы.
        annex_number: Номер приложения.
        access_level: Гриф доступа фрагмента.
        retrieval_source: Как найден — ``hybrid``, ``dense``, ``clause_lookup``.
        extra: Дополнительные признаки фрагмента.
    """

    chunk_id: str
    point_id: str
    content: str
    score: float
    clause_number: str = ""
    section_title: str = ""
    breadcrumbs: str = ""
    document_code: str = ""
    document_title: str = ""
    version_label: str = ""
    page_number: int | None = None
    annex_number: str = ""
    access_level: str = "internal"
    retrieval_source: str = "hybrid"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def clause_numbers(self) -> list[str]:
        """Номера всех пунктов, текст которых попал во фрагмент.

        При нарезке короткие пункты объединяются, и список из payload
        описывает адрес фрагмента точнее, чем один ``clause_number``.

        Returns:
            Список номеров; пустой, если пункты не распознаны.
        """
        clauses = self.extra.get("clauses") or []
        numbers = [str(item) for item in clauses if str(item)]
        if not numbers and self.clause_number:
            return [self.clause_number]
        return numbers

    @property
    def citation_label(self) -> str:
        """Подпись для ссылки на источник в ответе.

        Returns:
            Строка вида «Инструкция по документообороту, п. 3.4.2 (с. 17)».
        """
        parts: list[str] = []
        if self.document_title:
            parts.append(self.document_title)
        if self.annex_number:
            parts.append(f"Приложение {self.annex_number}")
        numbers = self.clause_numbers
        if numbers:
            parts.append("п. " + ", ".join(numbers[:3]))
        elif self.section_title and not self.annex_number:
            parts.append(self.section_title)
        label = ", ".join(parts) if parts else "База знаний"
        if self.page_number:
            label += f" (с. {self.page_number})"
        return label

    @property
    def relevance_percent(self) -> int:
        """Релевантность в процентах.

        ТЗ предлагает показывать процент соответствия запросу — выводим его
        в интерфейсе рядом с каждой ссылкой.

        Returns:
            Целое число от 0 до 100.
        """
        return max(0, min(100, round(self.score * 100)))

    def to_payload(self) -> dict[str, Any]:
        """Сериализует фрагмент для передачи на фронтенд."""
        return {
            "chunk_id": self.chunk_id,
            "document_code": self.document_code,
            "document_title": self.document_title,
            "version_label": self.version_label,
            "clause_number": self.clause_number,
            "clause_numbers": self.clause_numbers,
            "section_title": self.section_title,
            "breadcrumbs": self.breadcrumbs,
            "page_number": self.page_number,
            "annex_number": self.annex_number,
            "citation_label": self.citation_label,
            "relevance": round(self.score, 4),
            "relevance_percent": self.relevance_percent,
            "quote": self.content[:600],
        }


def _point_to_chunk(point: Any, *, source: str = "hybrid") -> RetrievedChunk:
    """Преобразует точку Qdrant во фрагмент.

    Args:
        point: Точка из ответа Qdrant.
        source: Способ, которым точка была найдена.

    Returns:
        Заполненный :class:`RetrievedChunk`.
    """
    payload: dict[str, Any] = point.payload or {}
    return RetrievedChunk(
        chunk_id=str(payload.get("chunk_id", point.id)),
        point_id=str(point.id),
        content=str(payload.get("content", "")),
        score=float(getattr(point, "score", 0.0) or 0.0),
        clause_number=str(payload.get("clause_number", "")),
        section_title=str(payload.get("section_title", "")),
        breadcrumbs=str(payload.get("breadcrumbs", "")),
        document_code=str(payload.get("document_code", "")),
        document_title=str(payload.get("document_title", "")),
        version_label=str(payload.get("version_label", "")),
        page_number=payload.get("page_number"),
        annex_number=str(payload.get("annex_number", "")),
        access_level=str(payload.get("access_level", "internal")),
        retrieval_source=source,
        extra={
            key: value
            for key, value in payload.items()
            if key
            not in {
                "chunk_id",
                "content",
                "clause_number",
                "section_title",
                "breadcrumbs",
                "document_code",
                "document_title",
                "version_label",
                "page_number",
                "annex_number",
                "access_level",
            }
        },
    )


class Retriever:
    """Поиск релевантных положений инструкции."""

    def __init__(self, collection: str | None = None) -> None:
        """Инициализирует ретривер.

        Args:
            collection: Имя коллекции Qdrant; по умолчанию из настроек.
        """
        self.collection = collection or settings.QDRANT_COLLECTION

    async def _ensure_ready(self) -> None:
        """Проверяет, что база знаний проиндексирована.

        Raises:
            KnowledgeBaseEmptyError: Коллекция отсутствует.
        """
        if not await collection_exists(self.collection):
            raise KnowledgeBaseEmptyError()

    async def search(
        self,
        query: str,
        *,
        allowed_levels: list[str],
        top_k: int | None = None,
        document_codes: list[str] | None = None,
        version_id: str | None = None,
        score_threshold: float | None = None,
    ) -> list[RetrievedChunk]:
        """Выполняет гибридный поиск по базе знаний.

        Args:
            query: Запрос пользователя.
            allowed_levels: Грифы, доступные роли пользователя.
            top_k: Сколько кандидатов вернуть до реранжирования.
            document_codes: Ограничить поиск конкретными документами.
            version_id: Искать в конкретной редакции (для сравнения версий).
            score_threshold: Нижний порог оценки.

        Returns:
            Список найденных фрагментов, отсортированный по релевантности.

        Raises:
            KnowledgeBaseEmptyError: База знаний не проиндексирована.
            RetrievalError: Ошибка обращения к Qdrant.
        """
        await self._ensure_ready()

        limit = top_k or settings.RETRIEVER_TOP_K
        threshold = (
            score_threshold
            if score_threshold is not None
            else settings.RETRIEVER_SCORE_THRESHOLD
        )
        query_filter = build_access_filter(
            allowed_levels,
            document_codes=document_codes,
            only_active_versions=version_id is None,
            version_id=version_id,
        )

        try:
            dense_vector = await get_embedder().embed_query(query)
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(
                "Не удалось векторизовать запрос", context={"error": str(exc)}
            ) from exc

        encoder = get_bm25_encoder()
        sparse = encoder.encode_query(query) if settings.HYBRID_SEARCH_ENABLED else None
        use_hybrid = bool(sparse and not sparse.is_empty)

        client = get_qdrant()
        mode = "hybrid" if use_hybrid else "dense"

        try:
            with observe_latency(retrieval_latency_seconds, mode=mode):
                if use_hybrid:
                    assert sparse is not None
                    response = await client.query_points(
                        collection_name=self.collection,
                        prefetch=[
                            models.Prefetch(
                                query=dense_vector,
                                using=DENSE_VECTOR_NAME,
                                limit=limit * 2,
                                filter=query_filter,
                            ),
                            models.Prefetch(
                                query=models.SparseVector(
                                    indices=sparse.indices, values=sparse.values
                                ),
                                using=SPARSE_VECTOR_NAME,
                                limit=limit * 2,
                                filter=query_filter,
                            ),
                        ],
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=limit,
                        with_payload=True,
                    )
                else:
                    response = await client.query_points(
                        collection_name=self.collection,
                        query=dense_vector,
                        using=DENSE_VECTOR_NAME,
                        query_filter=query_filter,
                        limit=limit,
                        score_threshold=threshold,
                        with_payload=True,
                    )
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(context={"error": str(exc), "mode": mode}) from exc

        chunks = [_point_to_chunk(point, source=mode) for point in response.points]

        if not use_hybrid:
            chunks = [chunk for chunk in chunks if chunk.score >= threshold]

        logger.info(
            "retrieval_completed",
            mode=mode,
            found=len(chunks),
            top_score=round(chunks[0].score, 4) if chunks else 0.0,
        )
        return chunks

    async def lookup_clause(
        self,
        clause_number: str,
        *,
        allowed_levels: list[str],
        annex_number: str = "",
        document_codes: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Находит фрагменты по точному номеру пункта.

        Когда сотрудник спрашивает «что написано в пункте 4.2», семантический
        поиск избыточен — нужен прямой доступ по номеру.

        Args:
            clause_number: Номер пункта.
            allowed_levels: Доступные грифы.
            annex_number: Номер приложения, если указан.
            document_codes: Ограничение по документам.

        Returns:
            Фрагменты с указанным номером пункта.

        Raises:
            RetrievalError: Ошибка обращения к Qdrant.
        """
        await self._ensure_ready()

        conditions: list[models.Condition] = [
            models.FieldCondition(key="access_level", match=models.MatchAny(any=allowed_levels)),
            models.FieldCondition(key="is_active_version", match=models.MatchValue(value=True)),
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="clause_number", match=models.MatchValue(value=clause_number)
                    ),
                    models.FieldCondition(
                        key="clauses", match=models.MatchValue(value=clause_number)
                    ),
                ]
            ),
        ]
        if annex_number:
            conditions.append(
                models.FieldCondition(
                    key="annex_number", match=models.MatchValue(value=annex_number)
                )
            )
        if document_codes:
            conditions.append(
                models.FieldCondition(
                    key="document_code", match=models.MatchAny(any=document_codes)
                )
            )

        try:
            points, _ = await get_qdrant().scroll(
                collection_name=self.collection,
                scroll_filter=models.Filter(must=conditions),
                limit=8,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(context={"error": str(exc)}) from exc

        chunks: list[RetrievedChunk] = []
        for point in points:
            chunk = _point_to_chunk(point, source="clause_lookup")
            # Прямое попадание по номеру — максимальная уверенность.
            chunk.score = 1.0
            chunks.append(chunk)

        logger.info("clause_lookup", clause=clause_number, found=len(chunks))
        return chunks

    async def search_with_clause_boost(
        self,
        query: str,
        *,
        allowed_levels: list[str],
        top_k: int | None = None,
        document_codes: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Гибридный поиск с приоритетом явно упомянутых пунктов.

        Если в запросе есть прямая ссылка на пункт или приложение, эти
        фрагменты добавляются в начало выдачи независимо от семантики.

        Args:
            query: Запрос пользователя.
            allowed_levels: Доступные грифы.
            top_k: Количество кандидатов.
            document_codes: Ограничение по документам.

        Returns:
            Объединённая выдача без дубликатов.
        """
        mentioned_clauses = CLAUSE_MENTION_RE.findall(query)
        mentioned_annexes = ANNEX_MENTION_RE.findall(query)

        results = await self.search(
            query,
            allowed_levels=allowed_levels,
            top_k=top_k,
            document_codes=document_codes,
        )

        if not mentioned_clauses and not mentioned_annexes:
            return results

        direct: list[RetrievedChunk] = []
        for clause in mentioned_clauses[:3]:
            direct.extend(
                await self.lookup_clause(
                    clause, allowed_levels=allowed_levels, document_codes=document_codes
                )
            )
        for annex in mentioned_annexes[:2]:
            direct.extend(
                await self.search(
                    f"Приложение {annex}",
                    allowed_levels=allowed_levels,
                    top_k=4,
                    document_codes=document_codes,
                )
            )

        seen: set[str] = set()
        merged: list[RetrievedChunk] = []
        for chunk in [*direct, *results]:
            if chunk.point_id in seen:
                continue
            seen.add(chunk.point_id)
            merged.append(chunk)

        logger.info(
            "clause_boost_applied",
            clauses=len(mentioned_clauses),
            annexes=len(mentioned_annexes),
            total=len(merged),
        )
        return merged


_retriever: Retriever | None = None


def get_retriever() -> Retriever:
    """Возвращает singleton-ретривер.

    Returns:
        Общий экземпляр :class:`Retriever`.
    """
    global _retriever
    if _retriever is None:
        _retriever = Retriever()
    return _retriever
