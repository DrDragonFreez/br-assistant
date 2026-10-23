"""Тесты контракта размещения маршрутов.

Два дефекта, которые эти проверки закрывают, не видны ни по логам, ни по
healthcheck, поэтому поймать их можно только тестом:

* ``root_path`` вместе с префиксом версии. Starlette вырезает ``root_path``
  из пути до сопоставления маршрута. Если прокси префикс не срезает, а
  приложение считает, что срезает, каждый запрос к ``/api/v1/*`` отдаёт 404,
  тогда как ``/health`` и Swagger продолжают работать.
* ``/metrics`` с префиксом версии. Nginx отдаёт наружу всё, что начинается
  с ``/api/``, поэтому метрики, подключённые в роутер версии, оказались бы
  доступны из интернета.

Проверки работают без поднятых сервисов: ``Settings`` читается из окружения,
а состав роутера версии разбирается по исходному коду, без импорта модулей,
которым нужны подключения.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings

ROUTER_SOURCE = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "router.py"

_SECRET = "s" * 32


def _included_routers() -> set[str]:
    """Возвращает имена модулей, подключённых в роутер первой версии.

    Исходный код разбирается статически: импорт ``app.api.v1.router``
    затянул бы зависимости, которым нужны Redis и PostgreSQL.

    Returns:
        Множество имён вида ``chat``, ``auth``.
    """
    tree = ast.parse(ROUTER_SOURCE.read_text(encoding="utf-8"))
    included: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "include_router" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Attribute) and isinstance(argument.value, ast.Name):
            included.add(argument.value.id)
    return included


# ─────────────────────── ПРЕФИКСЫ И root_path ───────────────────────
def test_root_path_is_empty_by_default() -> None:
    """По умолчанию префикс не срезается: nginx.conf передаёт путь как есть."""
    assert settings.API_ROOT_PATH == ""
    assert settings.API_V1_PREFIX == "/api/v1"


def test_root_path_colliding_with_prefix_is_rejected() -> None:
    """Конфигурация с двойным префиксом не должна стартовать."""
    with pytest.raises(ValidationError):
        Settings(SECRET_KEY=_SECRET, API_ROOT_PATH="/api", API_V1_PREFIX="/api/v1")


def test_root_path_outside_prefix_is_allowed() -> None:
    """Прокси может срезать и другой префикс — это не конфликт."""
    config = Settings(SECRET_KEY=_SECRET, API_ROOT_PATH="/assistant", API_V1_PREFIX="/api/v1")

    assert config.API_ROOT_PATH == "/assistant"


def test_empty_root_path_allows_any_prefix() -> None:
    """Пустой root_path не ограничивает префикс версии."""
    config = Settings(SECRET_KEY=_SECRET, API_V1_PREFIX="/api/v1")

    assert config.API_ROOT_PATH == ""


# ─────────────────────── СОСТАВ РОУТЕРА ВЕРСИИ ───────────────────────
def test_metrics_not_exposed_under_version_prefix() -> None:
    """Метрики не подключаются в роутер версии: иначе их отдаёт nginx."""
    assert "metrics" not in _included_routers()


def test_health_not_exposed_under_version_prefix() -> None:
    """Проверки состояния живут в корне: их опрашивает docker healthcheck."""
    assert "health" not in _included_routers()


def test_business_routers_are_connected() -> None:
    """Прикладные разделы API подключены все до единого."""
    assert _included_routers() == {"auth", "chat", "documents", "admin"}


def test_metrics_endpoint_path_is_root_level() -> None:
    """Эндпоинт метрик объявлен как ``/metrics`` без префикса версии."""
    from app.api.v1.metrics import router as metrics_router

    paths = {route.path for route in metrics_router.routes}

    assert paths == {"/metrics"}
