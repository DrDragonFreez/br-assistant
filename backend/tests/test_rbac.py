"""Тесты разграничения доступа.

Фильтр по грифу — единственное, что отделяет сотрудника от положений
с грифом «конфиденциально»: список допустимых грифов подставляется прямо
в запрос к Qdrant, поэтому недоступные фрагменты не попадают ни в выдачу,
ни в контекст модели. Ошибка в этой таблице означает утечку, которую
не видно по ответу помощника, — отсюда отдельные тесты.
"""

from __future__ import annotations

import pytest

from app.core.rbac import (
    AccessLevel,
    Permission,
    Role,
    allowed_access_levels,
    can_read_level,
    get_permissions,
    has_permission,
)


# ─────────────────────────── ГРИФЫ ───────────────────────────
def test_employee_cannot_read_confidential() -> None:
    """Сотруднику недоступен гриф «конфиденциально»."""
    levels = allowed_access_levels(Role.EMPLOYEE)

    assert AccessLevel.CONFIDENTIAL.value not in levels
    assert levels == [AccessLevel.PUBLIC.value, AccessLevel.INTERNAL.value]


def test_auditor_cannot_read_confidential() -> None:
    """Аудитор читает журналы, но не конфиденциальные положения."""
    assert not can_read_level(Role.AUDITOR, AccessLevel.CONFIDENTIAL)


def test_methodologist_reads_all_levels() -> None:
    """Методолог ЦДО работает со всеми грифами: он ведёт базу знаний."""
    levels = allowed_access_levels(Role.METHODOLOGIST)

    assert AccessLevel.CONFIDENTIAL.value in levels
    assert len(levels) == len(AccessLevel)


def test_levels_are_ordered_from_public() -> None:
    """Грифы возвращаются по возрастанию: так читается фильтр Qdrant."""
    for role in Role:
        levels = allowed_access_levels(role)
        assert levels[0] == AccessLevel.PUBLIC.value
        assert len(set(levels)) == len(levels)


def test_unknown_role_gets_public_only() -> None:
    """Неизвестная роль понижается до публичного грифа, а не до админского."""
    assert allowed_access_levels("devops") == [AccessLevel.PUBLIC.value]


def test_unknown_level_is_denied() -> None:
    """Неизвестный гриф трактуется как запрет."""
    assert not can_read_level(Role.ADMIN, "secret")


# ─────────────────────────── ПРАВА ───────────────────────────
def test_employee_cannot_manage_knowledge_base() -> None:
    """Сотрудник не загружает документы и не запускает переиндексацию."""
    for permission in (
        Permission.KB_UPLOAD,
        Permission.KB_REINDEX,
        Permission.KB_VERSION_MANAGE,
        Permission.KB_DELETE,
        Permission.USER_WRITE,
        Permission.AUDIT_READ,
    ):
        assert not has_permission(Role.EMPLOYEE, permission)


def test_auditor_cannot_ask_or_generate() -> None:
    """Аудитор только читает: ни обращений, ни черновиков документов."""
    assert not has_permission(Role.AUDITOR, Permission.CHAT_ASK)
    assert not has_permission(Role.AUDITOR, Permission.DOC_GENERATE)
    assert has_permission(Role.AUDITOR, Permission.CHAT_HISTORY_READ_ALL)


def test_admin_has_every_permission() -> None:
    """У администратора полный набор прав."""
    assert get_permissions(Role.ADMIN) == frozenset(Permission)


def test_only_auditor_and_admin_read_all_history() -> None:
    """Чужие диалоги доступны только аудитору и администратору."""
    allowed = {
        role for role in Role if has_permission(role, Permission.CHAT_HISTORY_READ_ALL)
    }

    assert allowed == {Role.AUDITOR, Role.ADMIN}


@pytest.mark.parametrize("role", list(Role))
def test_permissions_declared_for_every_role(role: Role) -> None:
    """Для каждой роли объявлен непустой набор прав."""
    assert get_permissions(role)
