"""Зависимости FastAPI: аутентификация, права доступа, аудит.

Проверка JWT выполняется здесь, а не в Nginx: директива ``auth_jwt`` доступна
только в коммерческой редакции NGINX Plus, что противоречит требованию
open source. Приложение проверяет подпись, издателя, срок действия, тип
токена и отзыв через denylist в Redis.

Права проверяются декларативно: зависимость ``require(Permission.X)``
подключается к эндпоинту, и логика доступа не размазывается по обработчикам.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthError, PermissionDeniedError, UserInactiveError
from app.core.logging import get_logger
from app.core.rbac import Permission, Role, allowed_access_levels, has_permission
from app.core.security import decode_token
from app.db.models import AuditLog, User
from app.db.postgres import get_session
from app.db.redis import is_token_revoked

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access-токен")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri=settings.REDIS_URL,
    strategy="fixed-window",
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDep,
) -> User:
    """Извлекает и проверяет текущего пользователя по токену.

    Args:
        request: HTTP-запрос.
        credentials: Заголовок Authorization.
        session: Сессия БД.

    Returns:
        Активная учётная запись.

    Raises:
        AuthError: Токен отсутствует, недействителен или отозван.
        UserInactiveError: Учётная запись отключена.
    """
    if credentials is None or not credentials.credentials:
        raise AuthError("Требуется авторизация", code="missing_token")

    payload = decode_token(credentials.credentials, expected_type="access")

    if await is_token_revoked(payload.jti):
        raise AuthError("Сессия завершена, войдите заново", code="token_revoked")

    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise AuthError("Некорректный идентификатор в токене", code="invalid_token") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена", code="user_not_found")
    if not user.is_active:
        raise UserInactiveError()

    request.state.user_id = str(user.id)
    request.state.user_role = user.role.value
    request.state.token_jti = payload.jti

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(*permissions: Permission) -> Callable[[User], Awaitable[User]]:
    """Создаёт зависимость, проверяющую наличие прав.

    Args:
        *permissions: Требуемые права (нужны все перечисленные).

    Returns:
        Зависимость FastAPI.
    """

    async def _check(user: CurrentUser) -> User:
        """Проверяет права текущего пользователя.

        Args:
            user: Текущий пользователь.

        Returns:
            Тот же пользователь, если прав достаточно.

        Raises:
            PermissionDeniedError: Не хватает хотя бы одного права.
        """
        missing = [item for item in permissions if not has_permission(user.role, item)]
        if missing:
            logger.warning(
                "permission_denied",
                role=user.role.value,
                required=[item.value for item in missing],
            )
            raise PermissionDeniedError(
                "Недостаточно прав для выполнения операции",
                context={"required": [item.value for item in missing]},
            )
        return user

    return _check


def require_role(*roles: Role) -> Callable[[User], Awaitable[User]]:
    """Создаёт зависимость, ограничивающую доступ по роли.

    Args:
        *roles: Допустимые роли.

    Returns:
        Зависимость FastAPI.
    """

    async def _check(user: CurrentUser) -> User:
        """Проверяет роль текущего пользователя.

        Args:
            user: Текущий пользователь.

        Returns:
            Тот же пользователь, если роль подходит.

        Raises:
            PermissionDeniedError: Роль не входит в список допустимых.
        """
        if user.role not in roles:
            raise PermissionDeniedError(
                "Операция доступна другой роли",
                context={"allowed": [item.value for item in roles]},
            )
        return user

    return _check


async def get_access_levels(user: CurrentUser) -> list[str]:
    """Возвращает грифы документов, доступные пользователю.

    Результат подставляется в фильтр Qdrant, поэтому недоступные сотруднику
    положения не попадают ни в выдачу, ни в контекст языковой модели.

    Args:
        user: Текущий пользователь.

    Returns:
        Список допустимых грифов.
    """
    return allowed_access_levels(user.role)


AccessLevels = Annotated[list[str], Depends(get_access_levels)]


def get_request_id(request: Request) -> str:
    """Возвращает сквозной идентификатор запроса.

    Args:
        request: HTTP-запрос.

    Returns:
        Идентификатор, проставленный middleware или Nginx.
    """
    return str(getattr(request.state, "request_id", "") or "")


RequestId = Annotated[str, Depends(get_request_id)]


def client_ip(request: Request) -> str:
    """Определяет адрес клиента с учётом обратного прокси.

    Args:
        request: HTTP-запрос.

    Returns:
        IP-адрес клиента.
    """
    forwarded = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


async def write_audit(
    session: AsyncSession,
    *,
    user: User | None,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    outcome: str = "success",
    request: Request | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Записывает действие в журнал аудита.

    В журнал попадает факт действия, но не его содержание: текст обращений
    и реквизиты документов могут содержать персональные данные.

    Args:
        session: Сессия БД.
        user: Пользователь, выполнивший действие.
        action: Код действия.
        resource_type: Тип объекта.
        resource_id: Идентификатор объекта.
        outcome: Исход (``success`` или ``failure``).
        request: HTTP-запрос для определения адреса и идентификатора.
        details: Дополнительные сведения без персональных данных.
    """
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            ip_address=client_ip(request) if request else "",
            request_id=get_request_id(request) if request else "",
            details=details or {},
        )
    )
