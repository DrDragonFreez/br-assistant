"""Тесты быстрой классификации обращений.

Правила снимают с модели очевидные случаи: приветствие, запрос бланка,
вопрос о согласовании, тему вне компетенции. Это и быстрее (ответ без
обращения к LLM), и предсказуемее — а значит, проверяемо тестом.

Отдельно важно, что правила умеют *воздержаться*: если обращение
сформулировано свободно, ``classify_by_rules`` возвращает None и
классификацию выполняет модель. Правило, которое угадывает вместо
воздержания, хуже отсутствующего правила.
"""

from __future__ import annotations

import pytest

from app.agents.router import classify_by_rules, detect_document_kind
from app.agents.state import Intent


# ─────────────────────────── ТИП ДОКУМЕНТА ───────────────────────────
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Нужен шаблон служебной записки", "memo"),
        ("Подготовь проект приказа о командировке", "order"),
        ("Как оформить исходящий документ?", "letter"),
        ("Где форма протокола совещания?", "protocol"),
        ("Какой срок согласования?", ""),
    ],
)
def test_detect_document_kind(question: str, expected: str) -> None:
    """Тип документа определяется по упоминанию в тексте обращения."""
    assert detect_document_kind(question) == expected


# ─────────────────────────── НАМЕРЕНИЯ ───────────────────────────
def test_empty_question_is_smalltalk() -> None:
    """Пустое обращение не уходит ни в поиск, ни в модель."""
    result = classify_by_rules("   ")

    assert result is not None
    assert result["intent"] == Intent.SMALLTALK.value
    assert result["needs_clarification"] is False


def test_greeting_is_smalltalk() -> None:
    """Приветствие обрабатывается без обращения к базе знаний."""
    result = classify_by_rules("Привет!")

    assert result is not None
    assert result["intent"] == Intent.SMALLTALK.value


def test_greeting_does_not_swallow_real_question() -> None:
    """Вопрос с приветствием в начале остаётся вопросом по инструкции."""
    result = classify_by_rules(
        "Привет! Подскажи, пожалуйста, какой срок согласования "
        "служебной записки установлен инструкцией"
    )

    assert result is not None
    assert result["intent"] == Intent.APPROVAL_ROUTE.value


def test_template_request_detected_with_kind() -> None:
    """Запрос черновика распознаётся вместе с типом документа."""
    result = classify_by_rules("Подготовь мне проект служебной записки")

    assert result is not None
    assert result["intent"] == Intent.TEMPLATE_REQUEST.value
    assert result["document_kind"] == "memo"
    assert result["needs_clarification"] is False


def test_template_request_without_kind_asks_clarification() -> None:
    """Без типа документа помощник уточняет, а не угадывает бланк."""
    result = classify_by_rules("Нужен шаблон")

    assert result is not None
    assert result["intent"] == Intent.TEMPLATE_REQUEST.value
    assert result["needs_clarification"] is True
    assert "тип документа" in result["missing_info"]


def test_document_check_detected() -> None:
    """Просьба проверить проект уходит в контур проверки."""
    result = classify_by_rules("Проверь мой документ на соответствие требованиям")

    assert result is not None
    assert result["intent"] == Intent.DOCUMENT_CHECK.value


def test_approval_route_detected() -> None:
    """Вопрос о согласующих подразделениях распознаётся правилами."""
    result = classify_by_rules("Кто согласует приказ о командировке?")

    assert result is not None
    assert result["intent"] == Intent.APPROVAL_ROUTE.value
    assert result["document_kind"] == "order"


def test_clause_mention_makes_it_a_question() -> None:
    """Прямая ссылка на пункт — вопрос по инструкции, и пункт сохраняется."""
    result = classify_by_rules("Что сказано в п. 3.4 инструкции?")

    assert result is not None
    assert result["intent"] == Intent.QUESTION.value
    assert "3.4" in result["mentioned_clauses"]


def test_annex_mention_is_recorded() -> None:
    """Упоминание приложения тоже попадает в список адресов."""
    result = classify_by_rules("Покажи Приложение 2")

    assert result is not None
    assert "Приложение 2" in result["mentioned_clauses"]


def test_out_of_scope_detected() -> None:
    """Тема вне компетенции отсекается до обращения к базе знаний."""
    result = classify_by_rules("Какая погода в Москве и курс доллара?")

    assert result is not None
    assert result["intent"] == Intent.OUT_OF_SCOPE.value


def test_out_of_scope_wins_over_document_words() -> None:
    """Приоритет у явного действия: просьба написать код — не запрос бланка."""
    result = classify_by_rules("Напиши код, который формирует приказ")

    assert result is not None
    assert result["intent"] == Intent.OUT_OF_SCOPE.value


# ─────────────────────────── ВОЗДЕРЖАНИЕ ───────────────────────────
@pytest.mark.parametrize(
    "question",
    [
        "Как правильно оформить доверенность на представителя?",
        "Куда передаются документы после регистрации?",
        "Нужно ли ставить печать на копии?",
    ],
)
def test_rules_abstain_on_free_form_questions(question: str) -> None:
    """Свободная формулировка передаётся модели, а не классифицируется наугад."""
    assert classify_by_rules(question) is None


def test_rules_always_report_their_source() -> None:
    """Результат правил помечен источником: это видно в трассе графа."""
    result = classify_by_rules("Привет")

    assert result is not None
    assert result["source"] == "rules"
