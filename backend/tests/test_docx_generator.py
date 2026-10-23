"""Тесты генерации документов.

Проверяются вспомогательные функции, не требующие python-docx:
форматирование даты по правилам делопроизводства, безопасные имена файлов
и приведение значений полей к строке.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.docx_generator import format_date_ru, safe_filename
from app.services.template_registry import TemplateRegistry


def test_format_date_ru() -> None:
    """Дата оформляется словесно, как принято в документообороте."""
    assert format_date_ru(datetime(2026, 3, 15, tzinfo=UTC)) == "15 марта 2026 г."


def test_format_date_ru_all_months() -> None:
    """Все месяцы имеют корректную форму родительного падежа."""
    for month in range(1, 13):
        result = format_date_ru(datetime(2026, month, 1, tzinfo=UTC))
        assert result.startswith("1 ")
        assert result.endswith("2026 г.")


def test_safe_filename_keeps_cyrillic() -> None:
    """Кириллица в имени файла сохраняется, служебные символы убираются."""
    assert safe_filename("Служебная записка") == "Служебная записка"
    assert "/" not in safe_filename("Приказ/2026")
    assert ".." not in safe_filename("..\\..\\etc\\passwd")


def test_safe_filename_fallback() -> None:
    """Пустое имя заменяется безопасным значением по умолчанию."""
    assert safe_filename("///") == "document"


class _FakeTemplate:
    """Минимальная замена DocumentTemplate."""

    fields_schema = [
        {"name": "addressee", "label": "Адресат", "required": True},
        {"name": "subject", "label": "Тема", "required": True},
        {"name": "deadline", "label": "Срок", "required": False},
    ]


def test_missing_fields_detected() -> None:
    """Незаполненные обязательные поля выявляются."""
    missing = TemplateRegistry.missing_fields(
        _FakeTemplate(),  # type: ignore[arg-type]
        {"addressee": "Департамент"},
    )
    assert missing == ["subject"]


def test_optional_field_not_required() -> None:
    """Необязательные поля не попадают в список недостающих."""
    missing = TemplateRegistry.missing_fields(
        _FakeTemplate(),  # type: ignore[arg-type]
        {"addressee": "Департамент", "subject": "О согласовании"},
    )
    assert missing == []


def test_field_by_name() -> None:
    """Описание поля находится по имени."""
    field = TemplateRegistry.field_by_name(_FakeTemplate(), "subject")  # type: ignore[arg-type]
    assert field is not None
    assert field["label"] == "Тема"
