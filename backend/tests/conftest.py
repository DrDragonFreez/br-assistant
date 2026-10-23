"""Общие фикстуры тестов.

Юнит-тесты не требуют поднятых сервисов: проверяется чистая логика
(чанкер, извлечение цитат, детерминированные проверки критика).
Тесты, которым нужны PostgreSQL, Qdrant и Ollama, помечаются маркером
``integration`` и пропускаются по умолчанию.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

os.environ.setdefault("SECRET_KEY", "test" * 10)
os.environ.setdefault("APP_ENV", "local")
os.environ.setdefault("LOG_FORMAT", "console")


@dataclass(slots=True)
class FakeChunk:
    """Минимальная замена RetrievedChunk для юнит-тестов."""

    content: str = ""
    clause_number: str = ""
    annex_number: str = ""
    section_title: str = ""
    breadcrumbs: str = ""
    document_code: str = "INSTR-DOC"
    document_title: str = "Инструкция по документообороту"
    version_label: str = "ред. 1"
    page_number: int | None = None
    score: float = 0.9
    chunk_id: str = "00000000-0000-0000-0000-000000000001"
    point_id: str = "00000000-0000-0000-0000-000000000001"
    access_level: str = "internal"
    retrieval_source: str = "hybrid"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def relevance_percent(self) -> int:
        """Релевантность в процентах."""
        return max(0, min(100, round(self.score * 100)))

    @property
    def citation_label(self) -> str:
        """Подпись источника."""
        parts = [self.document_title]
        if self.clause_number:
            parts.append(f"п. {self.clause_number}")
        return ", ".join(parts)

    def to_payload(self) -> dict[str, Any]:
        """Сериализация для фронтенда."""
        return {
            "chunk_id": self.chunk_id,
            "document_code": self.document_code,
            "document_title": self.document_title,
            "clause_number": self.clause_number,
            "annex_number": self.annex_number,
            "section_title": self.section_title,
            "page_number": self.page_number,
            "citation_label": self.citation_label,
            "relevance": self.score,
            "relevance_percent": self.relevance_percent,
            "quote": self.content[:600],
        }


@pytest.fixture
def chunks() -> list[FakeChunk]:
    """Набор фрагментов с номерами пунктов 3.4, 3.4.2 и Приложением 2."""
    return [
        FakeChunk(content="Срок согласования — 2 рабочих дня.", clause_number="3.4", page_number=12),
        FakeChunk(content="Записка подписывается автором.", clause_number="3.4.2", page_number=12),
        FakeChunk(content="Форма бланка приведена ниже.", annex_number="2", page_number=48),
    ]


@pytest.fixture
def instruction_pages() -> list[tuple[int, str]]:
    """Фрагмент нормативного текста для проверки чанкера."""
    return [
        (
            1,
            "РАЗДЕЛ III. СОГЛАСОВАНИЕ ДОКУМЕНТОВ\n"
            "3.1. Общие положения о согласовании.\n"
            "3.4. Служебная записка согласуется непосредственным руководителем "
            "в течение 1 рабочего дня, после чего направляется в Центр "
            "документационного обеспечения, где срок согласования составляет "
            "не более 2 рабочих дней.\n"
            "3.4.2. Записка подписывается автором с указанием должности.\n",
        ),
        (
            2,
            "ПРИЛОЖЕНИЕ 2\n"
            "Форма бланка служебной записки приводится в настоящем приложении.\n",
        ),
    ]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Пропускает интеграционные тесты, если сервисы не подняты.

    Маркер ``integration`` ставится на тесты, которым нужны PostgreSQL,
    Qdrant и Ollama. По умолчанию они пропускаются: ``make test`` должен
    проходить на любой машине и не падать там, где стек не запущен.
    Запуск целиком: ``RUN_INTEGRATION_TESTS=1 pytest``.

    Args:
        config: Конфигурация запуска pytest.
        items: Собранные тесты.
    """
    if os.environ.get("RUN_INTEGRATION_TESTS") == "1":
        return

    skip_integration = pytest.mark.skip(
        reason="нужны поднятые сервисы; запуск: RUN_INTEGRATION_TESTS=1 pytest"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
