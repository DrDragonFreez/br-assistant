"""Переранжирование найденных фрагментов.

Ollama не предоставляет endpoint для cross-encoder-реранжирования, поэтому
bge-reranker-v2-m3 поднят отдельным сервисом (Infinity) с HTTP-API ``/rerank``.

Если реранкер отключён или недоступен, применяется запасной алгоритм MMR
на лексическом пересечении: качество ниже, но контур не падает — для демо
на машине без свободной видеопамяти это критично.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import observe_latency, rerank_latency_seconds
from app.rag.bm25 import tokenize
from app.rag.retriever import RetrievedChunk

logger = get_logger(__name__)


class Reranker:
    """Cross-encoder реранкер поверх сервиса Infinity."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """Инициализирует реранкер.

        Args:
            base_url: Адрес сервиса реранжирования.
            model: Имя модели.
            timeout: Таймаут запроса в секундах.
        """
        self.base_url = (base_url or settings.RERANKER_URL).rstrip("/")
        self.model = model or settings.RERANKER_MODEL
        self.timeout = timeout or settings.RERANKER_TIMEOUT_SECONDS
        self._client: httpx.AsyncClient | None = None
        self._unavailable: bool = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Возвращает переиспользуемый HTTP-клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
            )
        return self._client

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1.0, min=1, max=6),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        reraise=True,
    )
    async def _request_rerank(self, query: str, documents: list[str]) -> list[dict[str, Any]]:
        """Отправляет запрос на переранжирование.

        Args:
            query: Запрос пользователя.
            documents: Тексты фрагментов-кандидатов.

        Returns:
            Список словарей с индексом и оценкой релевантности.
        """
        client = await self._get_client()
        response = await client.post(
            "/rerank",
            json={"model": self.model, "query": query, "documents": documents},
        )
        response.raise_for_status()
        payload = response.json()
        return list(payload.get("results", []))

    @staticmethod
    def _normalize(score: float) -> float:
        """Приводит логит cross-encoder'а к диапазону 0–1.

        Это нужно, чтобы пороги отсечения и проценты релевантности
        в интерфейсе были сопоставимы между запусками.

        Args:
            score: Сырая оценка модели.

        Returns:
            Значение в диапазоне (0, 1).
        """
        if 0.0 <= score <= 1.0:
            return score
        return 1.0 / (1.0 + math.exp(-score))

    def _fallback_rerank(
        self, query: str, chunks: list[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        """Запасное переранжирование без нейросети.

        Комбинирует исходную оценку поиска с долей общих токенов и бонусом
        за явное совпадение номера пункта, затем снижает вес фрагментов,
        дублирующих уже отобранные (упрощённый MMR).

        Args:
            query: Запрос пользователя.
            chunks: Кандидаты.
            top_n: Сколько вернуть.

        Returns:
            Переранжированные фрагменты.
        """
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return chunks[:top_n]

        scored: list[tuple[float, RetrievedChunk]] = []
        for chunk in chunks:
            chunk_tokens = set(tokenize(chunk.content))
            overlap = len(query_tokens & chunk_tokens) / len(query_tokens)
            bonus = 0.15 if chunk.clause_number and chunk.clause_number in query else 0.0
            scored.append((0.5 * chunk.score + 0.5 * overlap + bonus, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)

        selected: list[RetrievedChunk] = []
        selected_tokens: set[str] = set()
        for score, chunk in scored:
            chunk_tokens = set(tokenize(chunk.content))
            final_score = score
            if selected_tokens:
                redundancy = len(chunk_tokens & selected_tokens) / max(len(chunk_tokens), 1)
                final_score *= 1.0 - 0.4 * redundancy
            chunk.score = min(max(final_score, 0.0), 1.0)
            chunk.retrieval_source = f"{chunk.retrieval_source}+mmr"
            selected.append(chunk)
            selected_tokens |= chunk_tokens
            if len(selected) >= top_n:
                break

        selected.sort(key=lambda item: item.score, reverse=True)
        logger.info("rerank_fallback_used", selected=len(selected))
        return selected

    async def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        *,
        top_n: int | None = None,
    ) -> list[RetrievedChunk]:
        """Переранжирует кандидатов по релевантности запросу.

        Args:
            query: Запрос пользователя.
            chunks: Кандидаты из векторного поиска.
            top_n: Сколько фрагментов оставить.

        Returns:
            Отсортированный по убыванию релевантности список.
        """
        if not chunks:
            return []

        limit = top_n or settings.RERANKER_TOP_N

        if not settings.RERANKER_ENABLED or self._unavailable:
            return self._fallback_rerank(query, chunks, limit)

        try:
            with observe_latency(rerank_latency_seconds):
                results = await self._request_rerank(query, [c.content for c in chunks])
        except Exception as exc:  # noqa: BLE001 — деградируем, но не падаем
            logger.warning("reranker_unavailable", error=str(exc), fallback="mmr")
            self._unavailable = True
            return self._fallback_rerank(query, chunks, limit)

        reranked: list[RetrievedChunk] = []
        for item in results:
            index = int(item.get("index", -1))
            if not 0 <= index < len(chunks):
                continue
            chunk = chunks[index]
            chunk.score = self._normalize(
                float(item.get("relevance_score", item.get("score", 0.0)))
            )
            chunk.retrieval_source = f"{chunk.retrieval_source}+rerank"
            reranked.append(chunk)

        if not reranked:
            return self._fallback_rerank(query, chunks, limit)

        reranked.sort(key=lambda item: item.score, reverse=True)
        top = reranked[:limit]

        logger.info(
            "rerank_completed",
            candidates=len(chunks),
            selected=len(top),
            top_score=round(top[0].score, 4) if top else 0.0,
        )
        return top

    async def health(self) -> bool:
        """Проверяет доступность сервиса реранжирования.

        Returns:
            True, если сервис отвечает.
        """
        if not settings.RERANKER_ENABLED:
            return False
        try:
            client = await self._get_client()
            response = await client.get("/health")
            healthy = response.status_code == 200
            if healthy:
                self._unavailable = False
            return healthy
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    """Возвращает singleton-реранкер.

    Returns:
        Общий экземпляр :class:`Reranker`.
    """
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


async def close_reranker() -> None:
    """Освобождает ресурсы реранкера."""
    global _reranker
    if _reranker is not None:
        await _reranker.close()
        _reranker = None
