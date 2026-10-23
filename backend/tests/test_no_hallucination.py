"""Тесты анти-галлюцинационного контура.

Ключевое требование ТЗ: при отсутствии информации помощник обязан честно
сообщить об этом, а не придумать правдоподобный ответ.
"""

from __future__ import annotations

from app.agents.critic import find_speculation, run_deterministic_checks
from app.agents.state import CriticVerdict
from app.rag.generator import GenerationResult
from app.rag.prompts import NO_ANSWER_MARKER, NO_ANSWER_MESSAGE, SYSTEM_PROMPT


def test_system_prompt_requires_refusal_marker() -> None:
    """Системный промпт обязывает модель возвращать маркер отказа."""
    rendered = SYSTEM_PROMPT.format(no_answer_marker=NO_ANSWER_MARKER)
    assert NO_ANSWER_MARKER in rendered
    assert "ТОЛЬКО информацию из блока" in rendered


def test_no_answer_message_is_honest() -> None:
    """Текст отказа прямо сообщает об отсутствии информации."""
    assert "нет информации" in NO_ANSWER_MESSAGE.lower()


def test_generation_result_detects_refusal() -> None:
    """Маркер отказа в ответе модели распознаётся."""
    result = GenerationResult(text=NO_ANSWER_MARKER, model="test")
    assert result.is_no_answer is True
    assert result.has_citations is False


def test_speculation_patterns_detected() -> None:
    """Обороты домысливания выявляются в тексте ответа."""
    assert find_speculation("Обычно срок составляет 3 дня.")
    assert find_speculation("Скорее всего, требуется виза юриста.")
    assert not find_speculation("Срок составляет 2 рабочих дня [п. 3.4].")


def test_answer_without_citations_needs_revision(chunks: list) -> None:
    """Ответ без единой ссылки отправляется на доработку."""
    checks = run_deterministic_checks("Срок составляет два рабочих дня.", chunks)
    assert checks["verdict"] is CriticVerdict.REVISED


def test_invented_citation_rejected(chunks: list) -> None:
    """Выдуманная ссылка приводит к отклонению ответа без участия модели."""
    checks = run_deterministic_checks("Требуется виза [п. 12.7].", chunks)
    assert checks["verdict"] is CriticVerdict.REJECTED
    assert checks["invalid_citations"] == ["12.7"]


def test_grounded_answer_approved(chunks: list) -> None:
    """Обоснованный ответ со ссылками проходит проверку."""
    checks = run_deterministic_checks(
        "Срок согласования — 2 рабочих дня [п. 3.4].", chunks
    )
    assert checks["verdict"] is CriticVerdict.APPROVED
    assert checks["invalid_citations"] == []
