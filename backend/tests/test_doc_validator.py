"""Тесты детерминированных проверок проекта документа.

Проверка загруженного черновика состоит из двух частей: формальные
реквизиты ищутся регулярными выражениями, а соответствие требованиям
инструкции оценивает модель. Здесь проверяется первая часть — она
обязана работать одинаково при каждом прогоне, без обращения к LLM.
"""

from __future__ import annotations

import pytest

from app.services.doc_validator import (
    MIN_BODY_LENGTH,
    Violation,
    check_formal_requisites,
    compute_verdict,
    detect_document_kind,
)

VALID_MEMO = """Начальнику Департамента документационного обеспечения

№ 12-3/456 от 15 марта 2026 года

О согласовании проекта служебной записки

В соответствии с установленным порядком направляю на согласование проект
служебной записки о продлении срока проведения внутренней проверки состояния
документооборота в территориальном учреждении. Проект подготовлен с учётом
замечаний, полученных при предварительном рассмотрении.

Заместитель начальника управления ___________ И.О. Фамилия
"""


# ─────────────────────────── ТИП ДОКУМЕНТА ───────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ПРИКАЗ\nО внесении изменений", "order"),
        ("Служебная записка\nО переносе срока", "memo"),
        ("Протокол\nПрисутствовали: члены комиссии", "protocol"),
        ("Уважаемый Иван Иванович, просим вас рассмотреть", "letter"),
        ("Произвольный текст без признаков вида документа", ""),
    ],
)
def test_detect_document_kind(text: str, expected: str) -> None:
    """Вид документа определяется по первым строкам текста."""
    assert detect_document_kind(text) == expected


def test_detect_document_kind_looks_only_at_header() -> None:
    """Вид определяется по началу документа, а не по случайному упоминанию."""
    text = "x" * 2000 + "\nСлужебная записка"

    assert detect_document_kind(text) == ""


# ─────────────────────────── РЕКВИЗИТЫ ───────────────────────────
def test_valid_document_has_no_violations() -> None:
    """Корректно оформленный черновик проходит проверку без замечаний."""
    assert check_formal_requisites(VALID_MEMO) == []


def test_empty_document_reports_all_requisites() -> None:
    """В пустом документе не обнаружен ни один реквизит."""
    violations = check_formal_requisites("")
    fields = {item.field_name for item in violations}

    assert {"дата документа", "подпись", "адресат"} <= fields
    assert all(item.recommendation for item in violations)


def test_missing_date_is_informational() -> None:
    """Отсутствие даты — сведение о следующем этапе, а не нарушение.

    Дата вносится в РК при регистрации [п. 7.1], то есть после
    подписания, — в проекте её быть не может. Подробнее поведение
    закреплено в ``test_deferred_requisites``.
    """
    text = VALID_MEMO.replace("от 15 марта 2026 года", "")
    violations = [
        item for item in check_formal_requisites(text) if item.field_name == "дата документа"
    ]

    assert len(violations) == 1
    assert violations[0].severity == "info"
    assert violations[0].clause == "7.1"


def test_short_body_is_reported() -> None:
    """Слишком короткий текст отмечается отдельно от реквизитов."""
    text = "Начальнику управления\n№ 1 от 15 марта 2026 года\nО сроках\n___________ И.О. Фамилия"
    fields = {item.field_name for item in check_formal_requisites(text)}

    assert "содержательная часть" in fields
    assert len(text) < MIN_BODY_LENGTH


def test_colloquial_style_is_major() -> None:
    """Разговорные обороты в официальном документе — существенная ошибка."""
    text = VALID_MEMO.replace("Проект подготовлен", "Проект, короче, подготовлен")
    style = [item for item in check_formal_requisites(text) if item.field_name == "оформление"]

    assert len(style) == 1
    assert style[0].severity == "major"


def test_double_punctuation_is_minor() -> None:
    """Двойные знаки препинания отмечаются как незначительная правка."""
    text = VALID_MEMO.replace("рассмотрении.", "рассмотрении!!")
    style = [item for item in check_formal_requisites(text) if item.field_name == "оформление"]

    assert [item.severity for item in style] == ["minor"]


# ─────────────────────────── ВЕРДИКТ ───────────────────────────
def test_verdict_without_violations_is_compliant() -> None:
    """Без замечаний документ соответствует требованиям."""
    assert compute_verdict([]) == ("compliant", 1.0)


def test_single_critical_violation_blocks_document() -> None:
    """Одно критичное несоответствие сразу даёт major_issues."""
    verdict, score = compute_verdict(
        [Violation(severity="critical", field_name="подпись", problem="нет подписи")]
    )

    assert verdict == "major_issues"
    assert score == pytest.approx(0.75)


def test_three_major_violations_block_document() -> None:
    """Три существенных замечания равносильны критичному."""
    violations = [
        Violation(severity="major", field_name=f"поле {index}", problem="нарушение")
        for index in range(3)
    ]
    verdict, _ = compute_verdict(violations)

    assert verdict == "major_issues"


def test_minor_violations_are_not_blocking() -> None:
    """Мелкие замечания не блокируют документ, но снижают оценку."""
    verdict, score = compute_verdict(
        [Violation(severity="minor", field_name="оформление", problem="лишние пробелы")]
    )

    assert verdict == "minor_issues"
    assert score == pytest.approx(0.97)


def test_score_never_goes_below_zero() -> None:
    """Оценка остаётся в диапазоне [0, 1] при любом числе замечаний."""
    violations = [
        Violation(severity="critical", field_name=f"поле {index}", problem="нарушение")
        for index in range(20)
    ]
    _, score = compute_verdict(violations)

    assert score == 0.0
