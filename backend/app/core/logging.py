"""Структурное логирование через structlog.

Ключевое требование ИБ: в логи не попадают персональные данные и реквизиты
документов. Процессор :func:`redact_sensitive` маскирует значения по списку
ключей и вычищает типовые паттерны (ФИО, email, телефон, номер документа)
из свободного текста.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "new_password",
        "old_password",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "secret",
        "secret_key",
        "cookie",
        "set-cookie",
        "full_name",
        "fio",
        "email",
        "phone",
        "document_number",
        "doc_number",
        "reg_number",
        "inn",
        "snils",
        "passport",
        "query",
        "question",
        "answer",
        "message",
        "content",
        "draft_text",
    }
)

_MASK: Final[str] = "***"

_PII_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"[\w\.\-+]+@[\w\-]+\.[\w\.\-]+"), "<email>"),
    (re.compile(r"(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"), "<phone>"),
    (re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.", re.UNICODE), "<fio>"),
    (re.compile(r"\b\d{2,5}-\d{1,3}/\d{2,6}\b"), "<doc_no>"),
    (re.compile(r"\b\d{3}-\d{3}-\d{3}\s?\d{2}\b"), "<snils>"),
)


def _mask_text(text: str) -> str:
    """Заменяет персональные данные в произвольной строке плейсхолдерами."""
    for pattern, placeholder in _PII_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def redact_sensitive(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Processor structlog: маскирует чувствительные поля события.

    Args:
        _logger: Логгер (не используется).
        _method_name: Имя метода логирования (не используется).
        event_dict: Словарь события.

    Returns:
        Событие с замаскированными значениями.
    """
    if not settings.PII_REDACTION_ENABLED:
        return event_dict

    for key, value in list(event_dict.items()):
        lowered = key.lower()
        if lowered in SENSITIVE_KEYS:
            if isinstance(value, str):
                event_dict[key] = f"{_MASK}(len={len(value)})"
            else:
                event_dict[key] = _MASK
        elif isinstance(value, str) and len(value) > 8:
            event_dict[key] = _mask_text(value)
    return event_dict


def add_request_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Processor structlog: добавляет статические поля сервиса."""
    event_dict.setdefault("service", "br-backend")
    event_dict.setdefault("env", settings.APP_ENV)
    event_dict.setdefault("version", settings.APP_VERSION)
    return event_dict


def configure_logging() -> None:
    """Настраивает structlog и стандартный logging.

    Вызывается один раз при старте приложения и в Celery-воркере.
    """
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        # structlog.stdlib.add_logger_name здесь неприменим: он читает
        # у логгера поле name, которого у PrintLogger нет, и любой вызов
        # лога падал бы с AttributeError. Имя модуля подставляет
        # get_logger — см. ниже.
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        add_request_context,
        redact_sensitive,
    ]

    if settings.LOG_FORMAT == "json":
        renderer: Any = structlog.processors.JSONRenderer(ensure_ascii=False)
        shared_processors.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
        shared_processors.append(structlog.processors.format_exc_info)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Возвращает именованный логгер.

    Args:
        name: Имя логгера, обычно ``__name__``.

    Returns:
        Связанный логгер structlog.
    """
    # Имя модуля привязывается значением, а не процессором: фабрика
    # PrintLogger не хранит имя, и structlog.stdlib.add_logger_name
    # с ней несовместим.
    logger = structlog.get_logger()
    if name:
        logger = logger.bind(logger=name)
    return logger  # type: ignore[no-any-return]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware: сквозной request_id и запись метрик доступа.

    Идентификатор берётся из заголовка ``X-Request-ID`` (его проставляет Nginx)
    либо генерируется. Он возвращается клиенту и попадает во все логи запроса.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Оборачивает обработку запроса контекстом логирования."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=request.headers.get(
                "X-Real-IP", request.client.host if request.client else "-"
            ),
        )

        logger = get_logger("http")
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed")
            raise

        response.headers["X-Request-ID"] = request_id
        logger.info("request_completed", status_code=response.status_code)
        structlog.contextvars.clear_contextvars()
        return response
