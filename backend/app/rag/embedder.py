"""Векторизация текста через Ollama (bge-m3).

bge-m3 выбрана как мультиязычная модель, устойчивая к русскому нормативному
стилю. Для запросов и документов используется одна и та же модель: смешивать
разные эмбеддеры в одной коллекции нельзя — векторы окажутся в разных
пространствах.

Все обращения идут с повторными попытками и экспоненциальной задержкой:
Ollama может подгружать модель в память десятки секунд после старта.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.exceptions import EmbeddingError
from app.core.logging import get_logger

logger = get_logger(__name__)


class OllamaEmbedder:
    """Клиент эмбеддингов поверх HTTP-API Ollama."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """Инициализирует клиент.

        Args:
            base_url: Адрес Ollama; по умолчанию из настроек.
            model: Имя модели эмбеддингов.
            timeout: Таймаут запроса в секундах.
        """
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_EMBED_MODEL
        self.timeout = timeout or settings.LLM_TIMEOUT_SECONDS
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Возвращает переиспользуемый HTTP-клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.5, min=1, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        reraise=True,
    )
    async def _request_embeddings(self, inputs: list[str]) -> list[list[float]]:
        """Выполняет запрос к ``/api/embed``.

        Args:
            inputs: Список текстов.

        Returns:
            Список векторов.

        Raises:
            EmbeddingError: Если ответ не содержит векторов.
        """
        client = await self._get_client()
        response = await client.post(
            "/api/embed",
            json={
                "model": self.model,
                "input": inputs,
                "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

        vectors = payload.get("embeddings")
        if not vectors:
            single = payload.get("embedding")
            if single:
                vectors = [single]
        if not vectors:
            raise EmbeddingError(context={"model": self.model, "reason": "empty_response"})
        return [list(map(float, vector)) for vector in vectors]

    async def embed_documents(
        self, texts: list[str], *, batch_size: int = 16
    ) -> list[list[float]]:
        """Векторизует набор фрагментов документа.

        Args:
            texts: Тексты фрагментов.
            batch_size: Размер пачки; подбирается под объём видеопамяти.

        Returns:
            Список векторов в том же порядке, что и входные тексты.

        Raises:
            EmbeddingError: Если векторизация не удалась.
        """
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                vectors.extend(await self._request_embeddings(batch))
            except EmbeddingError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingError(
                    context={"model": self.model, "error": str(exc), "batch_start": start}
                ) from exc
            if start + batch_size < len(texts):
                await asyncio.sleep(0.05)

        logger.info("documents_embedded", count=len(vectors), model=self.model)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Векторизует пользовательский запрос.

        Args:
            text: Текст запроса.

        Returns:
            Вектор запроса.

        Raises:
            EmbeddingError: Если векторизация не удалась.
        """
        try:
            vectors = await self._request_embeddings([text])
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(context={"model": self.model, "error": str(exc)}) from exc
        return vectors[0]

    async def health(self) -> bool:
        """Проверяет готовность модели эмбеддингов.

        Returns:
            True, если модель отвечает.
        """
        try:
            await self.embed_query("проверка")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedder_healthcheck_failed", error=str(exc))
            return False

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


_embedder: OllamaEmbedder | None = None


def get_embedder() -> OllamaEmbedder:
    """Возвращает singleton-эмбеддер.

    Returns:
        Общий экземпляр :class:`OllamaEmbedder`.
    """
    global _embedder
    if _embedder is None:
        _embedder = OllamaEmbedder()
    return _embedder


async def close_embedder() -> None:
    """Освобождает ресурсы эмбеддера при остановке приложения."""
    global _embedder
    if _embedder is not None:
        await _embedder.close()
        _embedder = None
