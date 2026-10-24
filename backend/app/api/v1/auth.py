"""Аутентификация и управление собственной учётной записью.

Меры защиты от подбора пароля:

* жёсткий rate-limit на вход (настраивается через ``RATE_LIMIT_LOGIN``);
* счётчик неудачных попыток с блокировкой учётной записи;
* одинаковый ответ при неверном пароле и несуществующем пользователе —
  перечисление учётных записей по коду ответа невозможно;
* отзыв токенов через denylist в Redis при выходе и смене пароля.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.api.v1.deps import CurrentUser, SessionDep, write_audit
from app.api.v1.schemas import (
    CurrentUserResponse,
    LoginRequest,
    PasswordChangeRequest,
    RefreshRequest,
    TokenResponse,
)
from app.core.config import settings
from app.core.exceptions import AuthError, UserInactiveError, ValidationError
from app.core.logging import get_logger
from app.core.rbac import ROLE_LABELS_RU, allowed_access_levels, get_permissions
from app.core.security import (
    create_token_pair,
    decode_token,
    hash_password,
    token_ttl_seconds,
    validate_password_strength,
    verify_password,
)
from app.db.models import User
from app.db.redis import is_token_revoked, revoke_token

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Аутентификация"])

MAX_FAILED_ATTEMPTS = 7


@router.post("/login", response_model=TokenResponse, summary="Вход в систему")
async def login(
    request: Request,
    payload: LoginRequest,
    session: SessionDep,
) -> TokenResponse:
    """Выполняет вход и выдаёт пару токенов.

    Args:
        request: HTTP-запрос.
        payload: Учётные данные.
        session: Сессия БД.

    Returns:
        Пара токенов доступа.

    Raises:
        AuthError: Неверные учётные данные.
        UserInactiveError: Учётная запись заблокирована.
    """
    user = await session.scalar(select(User).where(User.email == payload.email.lower()))

    if user is None or not verify_password(payload.password, user.hashed_password):
        if user is not None:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.is_active = False
                logger.warning("account_locked", attempts=user.failed_login_attempts)
            await write_audit(
                session,
                user=user,
                action="auth.login",
                outcome="failure",
                request=request,
                details={"attempts": user.failed_login_attempts},
            )
        else:
            await write_audit(
                session, user=None, action="auth.login", outcome="failure", request=request
            )
        raise AuthError("Неверный логин или пароль", code="invalid_credentials")

    if not user.is_active:
        raise UserInactiveError()

    user.failed_login_attempts = 0
    user.last_login_at = datetime.now(UTC)

    tokens = create_token_pair(subject=str(user.id), email=user.email, role=user.role)

    await write_audit(
        session,
        user=user,
        action="auth.login",
        outcome="success",
        request=request,
        details={"role": user.role.value},
    )
    logger.info("user_logged_in", role=user.role.value)

    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post("/refresh", response_model=TokenResponse, summary="Обновление токенов")
async def refresh(
    request: Request,
    payload: RefreshRequest,
    session: SessionDep,
) -> TokenResponse:
    """Обновляет пару токенов по refresh-токену.

    Прежний refresh-токен отзывается: повторное использование исключено.

    Args:
        request: HTTP-запрос.
        payload: Refresh-токен.
        session: Сессия БД.

    Returns:
        Новая пара токенов.

    Raises:
        AuthError: Токен недействителен.
        UserInactiveError: Учётная запись отключена.
    """
    token_payload = decode_token(payload.refresh_token, expected_type="refresh")

    if await is_token_revoked(token_payload.jti):
        raise AuthError("Токен обновления отозван", code="token_revoked")

    try:
        user_id = uuid.UUID(token_payload.sub)
    except ValueError as exc:
        raise AuthError("Некорректный токен", code="invalid_token") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise AuthError("Учётная запись не найдена", code="user_not_found")
    if not user.is_active:
        raise UserInactiveError()

    # Ротация: старый refresh-токен больше не действует.
    await revoke_token(token_payload.jti, token_ttl_seconds(token_payload))

    tokens = create_token_pair(subject=str(user.id), email=user.email, role=user.role)
    logger.info("tokens_refreshed", role=user.role.value)

    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # Без явного response_model=None FastAPI принимает аннотацию
    # возврата «-> None» за модель ответа и отказывается
    # регистрировать маршрут: тело при 204 недопустимо.
    response_model=None,
    summary="Выход из системы",
)
async def logout(request: Request, user: CurrentUser, session: SessionDep) -> None:
    """Завершает сессию, отзывая текущий access-токен.

    Args:
        request: HTTP-запрос.
        user: Текущий пользователь.
        session: Сессия БД.
    """
    jti = getattr(request.state, "token_jti", "")
    if jti:
        await revoke_token(jti, settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    await write_audit(session, user=user, action="auth.logout", request=request)
    logger.info("user_logged_out")


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Сведения о текущем пользователе",
)
async def get_me(user: CurrentUser) -> CurrentUserResponse:
    """Возвращает профиль текущего пользователя и его права.

    Интерфейс скрывает недоступные разделы на основании этих данных,
    но проверка прав всё равно выполняется на сервере при каждом запросе.

    Args:
        user: Текущий пользователь.

    Returns:
        Профиль с перечнем прав и доступных грифов.
    """
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        department=user.department,
        position=user.position,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        permissions=sorted(item.value for item in get_permissions(user.role)),
        allowed_access_levels=allowed_access_levels(user.role),
        role_label=ROLE_LABELS_RU.get(user.role, user.role.value),
    )


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    # Без явного response_model=None FastAPI принимает аннотацию
    # возврата «-> None» за модель ответа и отказывается
    # регистрировать маршрут: тело при 204 недопустимо.
    response_model=None,
    summary="Смена собственного пароля",
)
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    """Меняет пароль текущего пользователя.

    После смены пароля текущий токен отзывается: это стандартная мера
    на случай, если пароль меняют из-за компрометации.

    Args:
        request: HTTP-запрос.
        payload: Старый и новый пароль.
        user: Текущий пользователь.
        session: Сессия БД.

    Raises:
        AuthError: Старый пароль неверен.
        ValidationError: Новый пароль не соответствует политике.
    """
    if not verify_password(payload.old_password, user.hashed_password):
        await write_audit(
            session,
            user=user,
            action="auth.password_change",
            outcome="failure",
            request=request,
        )
        raise AuthError("Текущий пароль указан неверно", code="invalid_credentials")

    if payload.new_password == payload.old_password:
        raise ValidationError("Новый пароль должен отличаться от текущего")

    validate_password_strength(payload.new_password)
    user.hashed_password = hash_password(payload.new_password)

    jti = getattr(request.state, "token_jti", "")
    if jti:
        await revoke_token(jti, settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    await write_audit(session, user=user, action="auth.password_change", request=request)
    logger.info("password_changed")
