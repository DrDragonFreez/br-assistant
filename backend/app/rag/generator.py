"""Генерация ответов через Ollama.

Клиент решает четыре задачи сверх простого вызова модели:

* **Потоковая выдача** — ответ приходит в интерфейс по мере генерации,
  иначе сотрудник ждёт 20–40 секунд перед первым словом.
* **Структурированный вывод** — узлы маршрутизации и критика требуют JSON;
  модель иногда оборачивает его в markdown, поэтому ответ разбирается
  устойчиво к такому оформлению.
* **Повторные попытки и запасная модель** — при сбое дообученной модели
  запрос повторяется на базовой, а не возвращается ошибка.
* **Проверка ссылок** — из текста извлекаются упомянутые пункты, чтобы
  сверить их с переданным контекстом.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.exceptions import LLMError, LLMTimeoutError
from app.core.logging import get_logger
from app.core.metrics import (
    llm_errors_total,
    llm_fallback_total,
    llm_latency_seconds,
    llm_tokens_total,
    observe_latency,
)
from app.rag.prompts import NO_ANSWER_MARKER

logger = get_logger(__name__)

CITATION_RE = re.compile(
    r"\[(?:п\.?\s*|пункт\s*)?(\d{1,2}(?:\.\d{1,3}){0,4})\]|\[Приложение\s*(\d+|[IVXL]+)[^\]]*\]",
    re.IGNORECASE,
)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    """Решает, имеет ли смысл повторять запрос к модели.

    Повтор уместен при таймауте и сетевом сбое. Ответ 404 «model not found»
    повторять бессмысленно: модель не появится сама, и три попытки с
    экспоненциальной задержкой лишь отодвинут переключение на запасную.

    Args:
        exc: Исключение, возникшее при обращении к Ollama.

    Returns:
        True, если запрос стоит повторить.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


@dataclass(slots=True)
class GenerationResult:
    """Результат генерации ответа.

    Attributes:
        text: Сгенерированный текст.
        model: Имя модели, выдавшей ответ.
        prompt_tokens: Число токенов промпта.
        completion_tokens: Число токенов ответа.
        latency_ms: Длительность генерации.
        used_fallback: Признак того, что сработала запасная модель.
        cited_clauses: Номера пунктов, упомянутые в ответе.
    """

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    used_fallback: bool = False
    cited_clauses: list[str] = field(default_factory=list)

    @property
    def is_no_answer(self) -> bool:
        """True, если модель честно сообщила об отсутствии информации."""
        return NO_ANSWER_MARKER in self.text

    @property
    def has_citations(self) -> bool:
        """True, если в ответе есть хотя бы одна ссылка на пункт."""
        return bool(self.cited_clauses)


def extract_citations(text: str) -> list[str]:
    """Извлекает номера пунктов, упомянутые в тексте.

    Args:
        text: Текст ответа.

    Returns:
        Список номеров без повторов, в порядке появления.
    """
    found: list[str] = []
    for clause, annex in CITATION_RE.findall(text):
        value = clause or (f"Приложение {annex}" if annex else "")
        if value and value not in found:
            found.append(value)
    return found


def parse_json_response(text: str) -> dict[str, Any]:
    """Разбирает JSON из ответа модели.

    Малые модели часто добавляют пояснения вокруг JSON или оборачивают его
    в markdown. Функция последовательно пробует три стратегии разбора.

    Args:
        text: Сырой ответ модели.

    Returns:
        Распарсенный словарь; пустой, если JSON извлечь не удалось.
    """
    candidates: list[str] = []

    fence = JSON_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))

    obj = JSON_OBJECT_RE.search(text)
    if obj:
        candidates.append(obj.group(0))

    candidates.append(text.strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    logger.warning("json_parse_failed", preview_len=len(text))
    return {}


class AnswerGenerator:
    """Клиент генерации поверх HTTP-API Ollama."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        """Инициализирует генератор.

        Args:
            base_url: Адрес Ollama.
            model: Основная модель (дообученная).
            fallback_model: Запасная модель (базовая).
        """
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL_ACTIVE
        self.fallback_model = fallback_model or settings.OLLAMA_LLM_MODEL
        self._client: httpx.AsyncClient | None = None
        self._model_missing: bool = False

    async def _get_client(self) -> httpx.AsyncClient:
        """Возвращает переиспользуемый HTTP-клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(settings.LLM_TIMEOUT_SECONDS, connect=10.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    def _build_options(
        self, *, temperature: float | None, max_tokens: int | None, json_mode: bool
    ) -> dict[str, Any]:
        """Собирает параметры генерации.

        Args:
            temperature: Температура сэмплирования.
            max_tokens: Ограничение длины ответа.
            json_mode: Признак структурированного вывода.

        Returns:
            Словарь параметров для Ollama.
        """
        return {
            "temperature": 0.0 if json_mode else (temperature or settings.LLM_TEMPERATURE),
            "top_p": settings.LLM_TOP_P,
            "num_predict": max_tokens or settings.LLM_MAX_TOKENS,
            "num_ctx": settings.LLM_CONTEXT_WINDOW,
            "repeat_penalty": 1.05,
            "stop": ["ВОПРОС СОТРУДНИКА:", "ФРАГМЕНТЫ ИНСТРУКЦИИ:"] if not json_mode else [],
        }

    @retry(
        stop=stop_after_attempt(settings.LLM_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.LLM_RETRY_BACKOFF_BASE, min=1, max=20),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def _request_chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        options: dict[str, Any],
        json_mode: bool,
    ) -> dict[str, Any]:
        """Выполняет непотоковый запрос к ``/api/chat``.

        Args:
            model: Имя модели.
            system: Системный промпт.
            user: Пользовательский промпт.
            options: Параметры генерации.
            json_mode: Требовать ли JSON-ответ.

        Returns:
            Разобранный ответ Ollama.
        """
        client = await self._get_client()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        }
        if json_mode:
            payload["format"] = "json"

        response = await client.post("/api/chat", json=payload)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        stage: str = "answer",
    ) -> GenerationResult:
        """Генерирует ответ модели.

        При сбое основной модели автоматически повторяет запрос на базовой —
        дообученный адаптер может отсутствовать, если LoRA ещё не собрана.

        Args:
            system_prompt: Системный промпт.
            user_prompt: Пользовательский промпт.
            temperature: Температура сэмплирования.
            max_tokens: Ограничение длины ответа.
            json_mode: Требовать структурированный вывод.
            stage: Метка этапа для метрик (``answer``, ``router``, ``critic``).

        Returns:
            Результат генерации.

        Raises:
            LLMTimeoutError: Модель не ответила за отведённое время.
            LLMError: Обе модели недоступны.
        """
        options = self._build_options(
            temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
        )
        target_model = self.fallback_model if self._model_missing else self.model
        used_fallback = self._model_missing

        try:
            with observe_latency(llm_latency_seconds, model=target_model, stage=stage):
                payload = await self._request_chat(
                    model=target_model,
                    system=system_prompt,
                    user=user_prompt,
                    options=options,
                    json_mode=json_mode,
                )
        except httpx.TimeoutException as exc:
            llm_errors_total.labels(model=target_model, error_type="timeout").inc()
            raise LLMTimeoutError(context={"model": target_model, "stage": stage}) from exc
        except Exception as exc:  # noqa: BLE001
            llm_errors_total.labels(model=target_model, error_type=type(exc).__name__).inc()
            can_fallback = (
                settings.LLM_FALLBACK_ENABLED
                and not used_fallback
                and target_model != self.fallback_model
            )
            if not can_fallback:
                raise LLMError(context={"model": target_model, "error": str(exc)}) from exc

            logger.warning(
                "llm_fallback_triggered",
                from_model=target_model,
                to_model=self.fallback_model,
                error=str(exc),
            )
            llm_fallback_total.labels(
                from_model=target_model, to_model=self.fallback_model
            ).inc()
            self._model_missing = True
            used_fallback = True

            try:
                with observe_latency(
                    llm_latency_seconds, model=self.fallback_model, stage=stage
                ):
                    payload = await self._request_chat(
                        model=self.fallback_model,
                        system=system_prompt,
                        user=user_prompt,
                        options=options,
                        json_mode=json_mode,
                    )
            except Exception as fallback_exc:  # noqa: BLE001
                llm_errors_total.labels(
                    model=self.fallback_model, error_type=type(fallback_exc).__name__
                ).inc()
                raise LLMError(
                    context={"model": self.fallback_model, "error": str(fallback_exc)}
                ) from fallback_exc
            target_model = self.fallback_model

        text = str(payload.get("message", {}).get("content", "")).strip()
        prompt_tokens = int(payload.get("prompt_eval_count", 0))
        completion_tokens = int(payload.get("eval_count", 0))
        latency_ms = int(payload.get("total_duration", 0) / 1_000_000)

        llm_tokens_total.labels(model=target_model, kind="prompt").inc(prompt_tokens)
        llm_tokens_total.labels(model=target_model, kind="completion").inc(completion_tokens)

        result = GenerationResult(
            text=text,
            model=target_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            used_fallback=used_fallback,
            cited_clauses=extract_citations(text),
        )
        logger.info(
            "generation_completed",
            stage=stage,
            model=target_model,
            latency_ms=latency_ms,
            citations=len(result.cited_clauses),
            no_answer=result.is_no_answer,
        )
        return result

    async def generate_json(
        self, system_prompt: str, user_prompt: str, *, stage: str = "router"
    ) -> dict[str, Any]:
        """Генерирует структурированный ответ и разбирает его как JSON.

        Args:
            system_prompt: Системный промпт.
            user_prompt: Пользовательский промпт.
            stage: Метка этапа для метрик.

        Returns:
            Распарсенный словарь; пустой, если разбор не удался.
        """
        result = await self.generate(
            system_prompt,
            user_prompt,
            json_mode=True,
            max_tokens=700,
            stage=stage,
        )
        return parse_json_response(result.text)

    async def _stream_chat(
        self, *, model: str, system: str, user: str, options: dict[str, Any]
    ) -> AsyncIterator[str]:
        """Открывает потоковый запрос к ``/api/chat``.

        Args:
            model: Имя модели.
            system: Системный промпт.
            user: Пользовательский промпт.
            options: Параметры генерации.

        Yields:
            Фрагменты текста по мере их поступления.
        """
        client = await self._get_client()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "options": options,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        }

        async with client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    llm_tokens_total.labels(model=model, kind="completion").inc(
                        int(chunk.get("eval_count", 0))
                    )
                    break

    async def stream(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Генерирует ответ в потоковом режиме.

        Как и :meth:`generate`, при недоступности основной модели переходит
        на базовую: дообученный адаптер может быть ещё не собран. Переход
        возможен, только пока в поток не ушёл ни один фрагмент — иначе
        сотрудник увидел бы два ответа подряд.

        Args:
            system_prompt: Системный промпт.
            user_prompt: Пользовательский промпт.
            temperature: Температура сэмплирования.
            max_tokens: Ограничение длины ответа.

        Yields:
            Фрагменты текста по мере генерации.

        Raises:
            LLMTimeoutError: Модель не ответила за отведённое время.
            LLMError: Обе модели недоступны.
        """
        options = self._build_options(
            temperature=temperature, max_tokens=max_tokens, json_mode=False
        )
        target_model = self.fallback_model if self._model_missing else self.model
        produced = False

        try:
            async for piece in self._stream_chat(
                model=target_model,
                system=system_prompt,
                user=user_prompt,
                options=options,
            ):
                produced = True
                yield piece
            return
        except httpx.TimeoutException as exc:
            llm_errors_total.labels(model=target_model, error_type="timeout").inc()
            raise LLMTimeoutError(context={"model": target_model}) from exc
        except Exception as exc:  # noqa: BLE001
            llm_errors_total.labels(model=target_model, error_type=type(exc).__name__).inc()

            can_fallback = (
                settings.LLM_FALLBACK_ENABLED
                and not produced
                and target_model != self.fallback_model
            )
            if not can_fallback:
                raise LLMError(context={"model": target_model, "error": str(exc)}) from exc

            logger.warning(
                "llm_stream_fallback_triggered",
                from_model=target_model,
                to_model=self.fallback_model,
                error=str(exc),
            )
            llm_fallback_total.labels(
                from_model=target_model, to_model=self.fallback_model
            ).inc()
            self._model_missing = True
        try:
            async for piece in self._stream_chat(
                model=self.fallback_model,
                system=system_prompt,
                user=user_prompt,
                options=options,
            ):
                yield piece
        except httpx.TimeoutException as exc:
            llm_errors_total.labels(model=self.fallback_model, error_type="timeout").inc()
            raise LLMTimeoutError(context={"model": self.fallback_model}) from exc
        except Exception as exc:  # noqa: BLE001
            llm_errors_total.labels(
                model=self.fallback_model, error_type=type(exc).__name__
            ).inc()
            raise LLMError(
                context={"model": self.fallback_model, "error": str(exc)}
            ) from exc

    async def list_models(self) -> list[str]:
        """Возвращает список моделей, доступных в Ollama.

        Returns:
            Имена моделей; пустой список при ошибке.
        """
        try:
            client = await self._get_client()
            response = await client.get("/api/tags")
            response.raise_for_status()
            return [item["name"] for item in response.json().get("models", [])]
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_models_failed", error=str(exc))
            return []

    async def health(self) -> dict[str, Any]:
        """Проверяет доступность моделей.

        Returns:
            Словарь со статусом основной и запасной модели.
        """
        models = await self.list_models()
        # Ollama возвращает имена с тегом: «qwen2.5:7b-instruct».
        normalized = {name.split(":")[0] for name in models} | set(models)
        return {
            "available": bool(models),
            "models": models,
            "primary_model": self.model,
            "primary_ready": self.model in normalized or self.model.split(":")[0] in normalized,
            "fallback_model": self.fallback_model,
            "fallback_ready": self.fallback_model in normalized
            or self.fallback_model.split(":")[0] in normalized,
            "using_fallback": self._model_missing,
        }

    def reset_fallback(self) -> None:
        """Сбрасывает признак отсутствия основной модели.

        Вызывается после сборки LoRA-модели, чтобы вернуться к дообученной
        версии без перезапуска сервиса.
        """
        self._model_missing = False

    async def close(self) -> None:
        """Закрывает HTTP-клиент."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


_generator: AnswerGenerator | None = None


def get_generator() -> AnswerGenerator:
    """Возвращает singleton-генератор.

    Returns:
        Общий экземпляр :class:`AnswerGenerator`.
    """
    global _generator
    if _generator is None:
        _generator = AnswerGenerator()
    return _generator


async def close_generator() -> None:
    """Освобождает ресурсы генератора."""
    global _generator
    if _generator is not None:
        await _generator.close()
        _generator = None
