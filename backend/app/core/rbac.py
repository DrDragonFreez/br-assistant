"""Ролевая модель и права доступа.

Модель спроектирована под корпоративный контур Банка России:
роли отражают реальные функции (сотрудник, методолог Центра документационного
обеспечения, аудитор, администратор), а не технические уровни.

Дополнительно поддерживается разграничение по грифу документа
(:class:`AccessLevel`): пользователь видит только те положения базы знаний,
гриф которых не выше его допуска. Фильтр применяется на уровне Qdrant-запроса,
то есть недоступные фрагменты не попадают даже в контекст LLM.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Role(StrEnum):
    """Роль пользователя в системе."""

    EMPLOYEE = "employee"
    """Сотрудник: задаёт вопросы, генерирует и проверяет черновики."""

    METHODOLOGIST = "methodologist"
    """Методолог ЦДО: управляет базой знаний, версиями и шаблонами."""

    AUDITOR = "auditor"
    """Аудитор: читает журнал обращений и отчёты качества, без правок БЗ."""

    ADMIN = "admin"
    """Администратор: полный доступ, включая управление пользователями."""


class Permission(StrEnum):
    """Атомарное право доступа."""

    # Диалог
    CHAT_ASK = "chat:ask"
    CHAT_HISTORY_READ = "chat:history:read"
    CHAT_HISTORY_READ_ALL = "chat:history:read_all"

    # Документы и шаблоны
    DOC_GENERATE = "doc:generate"
    DOC_VALIDATE = "doc:validate"
    TEMPLATE_READ = "template:read"
    TEMPLATE_WRITE = "template:write"

    # База знаний
    KB_READ = "kb:read"
    KB_UPLOAD = "kb:upload"
    KB_REINDEX = "kb:reindex"
    KB_VERSION_MANAGE = "kb:version:manage"
    KB_DELETE = "kb:delete"

    # Администрирование
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    AUDIT_READ = "audit:read"
    METRICS_READ = "metrics:read"
    EVAL_RUN = "eval:run"


class AccessLevel(StrEnum):
    """Гриф доступа к фрагменту базы знаний.

    Порядок возрастания строгости задаётся :data:`ACCESS_LEVEL_ORDER`.
    """

    PUBLIC = "public"
    """Общедоступные положения: FAQ, открытые разделы инструкции."""

    INTERNAL = "internal"
    """Для внутреннего пользования: основной объём регламентов."""

    CONFIDENTIAL = "confidential"
    """Ограниченный доступ: отдельные приложения и матрицы согласования."""


ACCESS_LEVEL_ORDER: Final[dict[AccessLevel, int]] = {
    AccessLevel.PUBLIC: 0,
    AccessLevel.INTERNAL: 1,
    AccessLevel.CONFIDENTIAL: 2,
}

_EMPLOYEE_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.CHAT_ASK,
        Permission.CHAT_HISTORY_READ,
        Permission.DOC_GENERATE,
        Permission.DOC_VALIDATE,
        Permission.TEMPLATE_READ,
        Permission.KB_READ,
    }
)

_METHODOLOGIST_PERMISSIONS: Final[frozenset[Permission]] = _EMPLOYEE_PERMISSIONS | {
    Permission.TEMPLATE_WRITE,
    Permission.KB_UPLOAD,
    Permission.KB_REINDEX,
    Permission.KB_VERSION_MANAGE,
    Permission.METRICS_READ,
    Permission.EVAL_RUN,
}

_AUDITOR_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.CHAT_HISTORY_READ,
        Permission.CHAT_HISTORY_READ_ALL,
        Permission.KB_READ,
        Permission.TEMPLATE_READ,
        Permission.AUDIT_READ,
        Permission.METRICS_READ,
    }
)

_ADMIN_PERMISSIONS: Final[frozenset[Permission]] = frozenset(Permission)

ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.EMPLOYEE: _EMPLOYEE_PERMISSIONS,
    Role.METHODOLOGIST: _METHODOLOGIST_PERMISSIONS,
    Role.AUDITOR: _AUDITOR_PERMISSIONS,
    Role.ADMIN: _ADMIN_PERMISSIONS,
}

ROLE_MAX_ACCESS_LEVEL: Final[dict[Role, AccessLevel]] = {
    Role.EMPLOYEE: AccessLevel.INTERNAL,
    Role.METHODOLOGIST: AccessLevel.CONFIDENTIAL,
    Role.AUDITOR: AccessLevel.INTERNAL,
    Role.ADMIN: AccessLevel.CONFIDENTIAL,
}

ROLE_LABELS_RU: Final[dict[Role, str]] = {
    Role.EMPLOYEE: "Сотрудник",
    Role.METHODOLOGIST: "Методолог ЦДО",
    Role.AUDITOR: "Аудитор",
    Role.ADMIN: "Администратор",
}


def get_permissions(role: Role | str) -> frozenset[Permission]:
    """Возвращает набор прав для роли.

    Args:
        role: Роль пользователя.

    Returns:
        Неизменяемое множество прав. Для неизвестной роли — пустое множество.
    """
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()


def has_permission(role: Role | str, permission: Permission) -> bool:
    """Проверяет наличие права у роли.

    Args:
        role: Роль пользователя.
        permission: Проверяемое право.

    Returns:
        True, если роль обладает правом.
    """
    return permission in get_permissions(role)


def allowed_access_levels(role: Role | str) -> list[str]:
    """Возвращает грифы документов, доступные роли.

    Результат подставляется в фильтр Qdrant, поэтому возвращаются строки.

    Args:
        role: Роль пользователя.

    Returns:
        Список значений грифа, которые роль вправе читать.
    """
    try:
        max_level = ROLE_MAX_ACCESS_LEVEL[Role(role)]
    except ValueError:
        return [AccessLevel.PUBLIC.value]

    ceiling = ACCESS_LEVEL_ORDER[max_level]
    return [level.value for level, rank in ACCESS_LEVEL_ORDER.items() if rank <= ceiling]


def can_read_level(role: Role | str, level: AccessLevel | str) -> bool:
    """Проверяет, вправе ли роль читать документ с заданным грифом.

    Args:
        role: Роль пользователя.
        level: Гриф документа.

    Returns:
        True, если доступ разрешён.
    """
    try:
        return AccessLevel(level).value in allowed_access_levels(role)
    except ValueError:
        return False
