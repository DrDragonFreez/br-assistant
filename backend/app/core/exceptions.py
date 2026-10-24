"""Доменные исключения и обработчики ошибок FastAPI.

Все ответы об ошибке приводятся к единому JSON-формату:
``{"detail": str, "code": str, "request_id": str | None}``.
Сообщения — на русском языке, так как их видит конечный пользователь.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Базовое исключение приложения.

    Attributes:
        message: Человекочитаемое сообщение для пользователя.
        code: Машиночитаемый код ошибки для фронтенда.
        status_code: HTTP-статус ответа.
        context: Дополнительные данные для логов (не возвращаются клиенту).
    """

    message: str = "Внутренняя ошибка сервиса"
    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.context = context or {}
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        """Сериализует исключение в тело HTTP-ответа."""
        return {"detail": self.message, "code": self.code, "request_id": request_id}


# ─────────────────────────── АУТЕНТИФИКАЦИЯ / ДОСТУП ───────────────────────────
class AuthError(AppError):
    """Неверные учётные данные или сломанный токен."""

    message = "Не удалось подтвердить учётные данные"
    code = "auth_failed"
    status_code = status.HTTP_401_UNAUTHORIZED


class TokenExpiredError(AuthError):
    """Срок действия токена истёк."""

    message = "Срок действия сессии истёк, войдите заново"
    code = "token_expired"


class PermissionDeniedError(AppError):
    """У роли пользователя нет нужного права."""

    message = "Недостаточно прав для выполнения операции"
    code = "permission_denied"
    status_code = status.HTTP_403_FORBIDDEN


class UserInactiveError(AuthError):
    """Учётная запись заблокирована администратором."""

    message = "Учётная запись отключена"
    code = "user_inactive"
    status_code = status.HTTP_403_FORBIDDEN


# ─────────────────────────── РЕСУРСЫ ───────────────────────────
class NotFoundError(AppError):
    """Запрошенный объект отсутствует."""

    message = "Запрошенный объект не найден"
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    """Нарушение уникальности или конкурентное изменение."""

    message = "Объект с такими параметрами уже существует"
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT


class ValidationError(AppError):
    """Запрос синтаксически верен, но не проходит бизнес-правила."""

    message = "Некорректные данные запроса"
    code = "validation_error"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


# ─────────────────────────── ДОКУМЕНТЫ ───────────────────────────
class UnsupportedFileTypeError(AppError):
    """Формат загруженного файла не поддерживается."""

    message = "Формат файла не поддерживается"
    code = "unsupported_file_type"
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


class FileTooLargeError(AppError):
    """Размер файла превышает лимит."""

    message = "Размер файла превышает допустимый лимит"
    code = "file_too_large"
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


class DocumentParseError(AppError):
    """Не удалось извлечь текст из документа."""

    message = "Не удалось обработать документ"
    code = "document_parse_error"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class TemplateNotFoundError(NotFoundError):
    """Шаблон модульного документа отсутствует в реестре."""

    message = "Шаблон документа не найден в реестре"
    code = "template_not_found"


# ─────────────────────────── RAG / LLM ───────────────────────────
class LLMError(AppError):
    """Ошибка обращения к языковой модели."""

    message = "Модель временно недоступна, повторите запрос"
    code = "llm_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class LLMTimeoutError(LLMError):
    """Модель не ответила за отведённое время."""

    message = "Модель не ответила за отведённое время"
    code = "llm_timeout"
    status_code = status.HTTP_504_GATEWAY_TIMEOUT


class EmbeddingError(AppError):
    """Не удалось построить векторное представление текста."""

    message = "Ошибка векторизации текста"
    code = "embedding_error"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class RetrievalError(AppError):
    """Ошибка обращения к векторному хранилищу."""

    message = "Ошибка поиска по базе знаний"
    code = "retrieval_error"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class KnowledgeBaseEmptyError(AppError):
    """База знаний не проиндексирована."""

    message = "База знаний пуста: загрузите и проиндексируйте регламенты"
    code = "kb_empty"
    status_code = status.HTTP_409_CONFLICT


# ─────────────────────────── ОБРАБОТЧИКИ ───────────────────────────
def _request_id(request: Request) -> str | None:
    """Достаёт сквозной идентификатор запроса, проставленный middleware."""
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Преобразует доменное исключение в JSON-ответ."""
    rid = _request_id(request)
    log = logger.bind(request_id=rid, error_code=exc.code, path=request.url.path)
    if exc.status_code >= 500:
        log.error("app_error", message=exc.message, **exc.context)
    else:
        log.warning("app_error", message=exc.message, **exc.context)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict(rid))


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Приводит стандартные HTTPException к единому формату."""
    rid = _request_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "code": f"http_{exc.status_code}",
            "request_id": rid,
        },
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Переводит ошибки валидации Pydantic в читаемый вид.

    Значения полей в лог не попадают: они могут содержать персональные данные.
    """
    rid = _request_id(request)
    fields = [".".join(str(loc) for loc in err["loc"][1:]) for err in exc.errors()]
    logger.warning("validation_error", request_id=rid, fields=fields, path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Некорректные данные запроса",
            "code": "validation_error",
            "request_id": rid,
            "fields": fields,
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Последний рубеж: не отдаёт наружу трассировку стека."""
    rid = _request_id(request)
    logger.exception(
        "unhandled_exception",
        request_id=rid,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Внутренняя ошибка сервиса",
            "code": "internal_error",
            "request_id": rid,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Регистрирует все обработчики исключений в приложении.

    Args:
        app: Экземпляр FastAPI.
    """
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
