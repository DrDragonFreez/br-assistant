"""Хеширование паролей и работа с JWT.

Nginx в open source-редакции не умеет валидировать JWT (директива ``auth_jwt``
доступна только в NGINX Plus), поэтому подпись, срок жизни и издатель токена
проверяются здесь, в приложении. Nginx отвечает за rate-limit и заголовки.

Отзыв токенов реализован через denylist в Redis по идентификатору ``jti``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.exceptions import AuthError, TokenExpiredError, ValidationError
from app.core.rbac import Role

TokenType = Literal["access", "refresh"]

_pwd_context: Final[CryptContext] = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)

REVOKED_TOKEN_PREFIX: Final[str] = "auth:revoked:"


class TokenPayload(BaseModel):
    """Полезная нагрузка JWT после успешной проверки подписи."""

    sub: str = Field(description="Идентификатор пользователя (UUID строкой)")
    email: str = Field(default="", description="Логин пользователя")
    role: Role = Field(default=Role.EMPLOYEE, description="Роль пользователя")
    type: TokenType = Field(default="access", description="Тип токена")
    jti: str = Field(description="Уникальный идентификатор токена")
    exp: int = Field(description="Время истечения (unix)")
    iat: int = Field(description="Время выпуска (unix)")
    iss: str = Field(default="", description="Издатель токена")


class TokenPair(BaseModel):
    """Пара токенов, возвращаемая при входе."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Время жизни access-токена в секундах")


# ─────────────────────────── ПАРОЛИ ───────────────────────────
def hash_password(password: str) -> str:
    """Хеширует пароль алгоритмом bcrypt.

    Args:
        password: Пароль в открытом виде.

    Returns:
        Хеш для хранения в БД.
    """
    return _pwd_context.hash(password[:72])


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Сверяет пароль с хешем.

    Args:
        plain_password: Введённый пароль.
        hashed_password: Хеш из БД.

    Returns:
        True, если пароль верный.
    """
    try:
        return _pwd_context.verify(plain_password[:72], hashed_password)
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> None:
    """Проверяет пароль на соответствие парольной политике.

    Args:
        password: Проверяемый пароль.

    Raises:
        ValidationError: Если пароль не удовлетворяет требованиям.
    """
    min_len = settings.PASSWORD_MIN_LENGTH
    problems: list[str] = []

    if len(password) < min_len:
        problems.append(f"не короче {min_len} символов")
    if not any(ch.isdigit() for ch in password):
        problems.append("минимум одна цифра")
    if not any(ch.isalpha() for ch in password):
        problems.append("минимум одна буква")
    if not any(not ch.isalnum() for ch in password):
        problems.append("минимум один спецсимвол")

    if problems:
        raise ValidationError(
            "Пароль не соответствует требованиям: " + ", ".join(problems),
            code="weak_password",
        )


# ─────────────────────────── JWT ───────────────────────────
def _create_token(
    *,
    subject: str,
    email: str,
    role: Role | str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    """Собирает и подписывает JWT.

    Returns:
        Кортеж ``(токен, jti, момент истечения)``.
    """
    now = datetime.now(UTC)
    expire = now + expires_delta
    jti = uuid.uuid4().hex

    payload: dict[str, Any] = {
        "sub": str(subject),
        "email": email,
        "role": str(role),
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": settings.JWT_ISSUER,
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(
        payload,
        settings.SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, jti, expire


def create_access_token(
    *,
    subject: str,
    email: str,
    role: Role | str,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Создаёт access-токен.

    Args:
        subject: Идентификатор пользователя.
        email: Логин пользователя.
        role: Роль пользователя.
        extra_claims: Дополнительные поля полезной нагрузки.

    Returns:
        Кортеж ``(токен, jti)``.
    """
    token, jti, _ = _create_token(
        subject=subject,
        email=email,
        role=role,
        token_type="access",
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims=extra_claims,
    )
    return token, jti


def create_refresh_token(
    *, subject: str, email: str, role: Role | str
) -> tuple[str, str, datetime]:
    """Создаёт refresh-токен.

    Args:
        subject: Идентификатор пользователя.
        email: Логин пользователя.
        role: Роль пользователя.

    Returns:
        Кортеж ``(токен, jti, момент истечения)``.
    """
    return _create_token(
        subject=subject,
        email=email,
        role=role,
        token_type="refresh",
        expires_delta=timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
    )


def create_token_pair(*, subject: str, email: str, role: Role | str) -> TokenPair:
    """Создаёт пару access + refresh токенов.

    Args:
        subject: Идентификатор пользователя.
        email: Логин пользователя.
        role: Роль пользователя.

    Returns:
        Заполненный :class:`TokenPair`.
    """
    access_token, _ = create_access_token(subject=subject, email=email, role=role)
    refresh_token, _, _ = create_refresh_token(subject=subject, email=email, role=role)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenPayload:
    """Проверяет подпись токена и возвращает полезную нагрузку.

    Args:
        token: JWT в виде строки.
        expected_type: Ожидаемый тип токена. Если задан и не совпадает —
            будет возбуждено исключение (защита от подстановки refresh вместо access).

    Returns:
        Разобранная полезная нагрузка.

    Raises:
        TokenExpiredError: Срок действия токена истёк.
        AuthError: Подпись недействительна, издатель не совпал или тип неверный.
    """
    try:
        raw: dict[str, Any] = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require_exp": True, "require_sub": True, "verify_iss": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except JWTError as exc:
        raise AuthError("Недействительный токен", code="invalid_token") from exc

    try:
        payload = TokenPayload.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — структура токена не соответствует схеме
        raise AuthError("Некорректная структура токена", code="invalid_token") from exc

    if expected_type and payload.type != expected_type:
        raise AuthError(
            f"Ожидался токен типа «{expected_type}»",
            code="wrong_token_type",
        )
    return payload


def revocation_key(jti: str) -> str:
    """Формирует ключ denylist в Redis для идентификатора токена.

    Args:
        jti: Идентификатор токена.

    Returns:
        Ключ Redis.
    """
    return f"{REVOKED_TOKEN_PREFIX}{jti}"


def token_ttl_seconds(payload: TokenPayload) -> int:
    """Считает остаток времени жизни токена.

    Нужен, чтобы запись в denylist жила ровно до истечения самого токена
    и не засоряла Redis.

    Args:
        payload: Полезная нагрузка токена.

    Returns:
        Количество секунд до истечения (минимум 1).
    """
    remaining = payload.exp - int(datetime.now(UTC).timestamp())
    return max(remaining, 1)
