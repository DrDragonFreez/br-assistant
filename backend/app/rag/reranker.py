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


# Длина основы при лексическом сравнении. Морфологического анализатора в
# контуре нет, и точное совпадение токенов на русском тексте почти не
# работает: «регистрируется» и «регистрация», «исходящий» и «исходящего»
# считались разными словами. Из-за этого фрагмент с ответом терял половину
# своей оценки и выпадал из выдачи. Усечение до пяти символов снимает
# большую часть падежей и видов глагола; возможные склейки вроде
# «документ» и «документооборот» для признака пересечения допустимы.
STEM_LENGTH = 5

# Вес разнообразия в MMR. Разнообразие нужно: нормативный текст повторяет
# формулировки, и без него выдача заполняется почти одинаковыми
# фрагментами. Но перевешивать релевантность оно не должно, поэтому вес
# заметно меньше единицы.
MMR_DIVERSITY_WEIGHT = 0.3

# Доля общих основ, при которой фрагмент считается повтором уже
# отобранного. Такие фрагменты отсеиваются отдельным правилом, а не
# весом разнообразия: повышать вес, чтобы вытеснить дубликат, значит
# снова ставить разнообразие выше релевантности и терять фрагмент
# с ответом. Если все оставшиеся кандидаты — повторы, берётся лучший
# из них: выдача не должна оказаться короче запрошенной.
NEAR_DUPLICATE_REDUNDANCY = 0.9


def _stems(text: str) -> set[str]:
    """Возвращает множество усечённых основ текста.

    Номера пунктов не усекаются: «10.3» и «10.15» должны остаться
    различимыми.

    Args:
        text: Исходный текст.

    Returns:
        Множество основ.
    """
    stems: set[str] = set()
    for token in tokenize(text):
        if token.replace(".", "").isdigit():
            stems.add(token)
        else:
            stems.add(token[:STEM_LENGTH])
    return stems


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
        query_stems = _stems(query)
        if not query_stems:
            return chunks[:top_n]

        # Релевантность: оценка гибридного поиска уже учитывает и смысл
        # (плотный вектор), и точные формулировки (BM25), поэтому вес у неё
        # больший. Лексическое пересечение остаётся поправкой, а не
        # половиной решения: иначе фрагмент, где нужное слово стоит в другой
        # форме, проигрывает фрагменту, где оно просто упомянуто.
        relevance: dict[int, float] = {}
        stems_by_chunk: dict[int, set[str]] = {}
        for index, chunk in enumerate(chunks):
            chunk_stems = _stems(chunk.content)
            stems_by_chunk[index] = chunk_stems
            overlap = len(query_stems & chunk_stems) / len(query_stems)
            bonus = 0.15 if chunk.clause_number and chunk.clause_number in query else 0.0
            relevance[index] = 0.7 * chunk.score + 0.3 * overlap + bonus

        # Maximal Marginal Relevance: на каждом шаге берётся кандидат с
        # наибольшей разностью «релевантность минус схожесть с уже
        # отобранными». Пересчёт после каждого выбора обязателен — без него
        # отбор сводится к первым top_n по исходной оценке, а штраф за
        # дублирование лишь переписывает им оценки, ни на что не влияя.
        remaining = set(range(len(chunks)))
        selected: list[RetrievedChunk] = []
        selected_stems: set[str] = set()

        while remaining and len(selected) < top_n:
            best_index = -1
            best_value = -math.inf
            best_duplicate_index = -1
            best_duplicate_value = -math.inf

            for index in remaining:
                value = relevance[index]
                redundancy = 0.0
                if selected_stems:
                    chunk_stems = stems_by_chunk[index]
                    redundancy = len(chunk_stems & selected_stems) / max(len(chunk_stems), 1)
                    value -= MMR_DIVERSITY_WEIGHT * redundancy

                if redundancy > NEAR_DUPLICATE_REDUNDANCY:
                    if value > best_duplicate_value:
                        best_duplicate_value = value
                        best_duplicate_index = index
                    continue

                if value > best_value:
                    best_value = value
                    best_index = index

            if best_index < 0:
                # Остались только повторы — берём лучший из них.
                best_index = best_duplicate_index
                best_value = best_duplicate_value

            remaining.discard(best_index)
            chunk = chunks[best_index]
            chunk.score = min(max(best_value, 0.0), 1.0)
            chunk.retrieval_source = f"{chunk.retrieval_source}+mmr"
            selected.append(chunk)
            selected_stems |= stems_by_chunk[best_index]

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
