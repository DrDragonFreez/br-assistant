"""Тесты цитирования — критерий №1 оценки.

Проверяется, что ссылки на пункты извлекаются из текста ответа корректно
и что ссылка на отсутствующий в контексте пункт обнаруживается.
"""

from __future__ import annotations

from app.agents.critic import collect_context_clauses, find_invalid_citations
from app.rag.generator import extract_citations


def test_extract_citations_simple() -> None:
    """Из ответа извлекаются номера пунктов в формате [п. X.Y]."""
    answer = "Срок составляет 2 дня [п. 3.4]. Подпись обязательна [п. 3.4.2]."
    assert extract_citations(answer) == ["3.4", "3.4.2"]


def test_extract_citations_annex() -> None:
    """Ссылки на приложения распознаются отдельно."""
    answer = "Форма приведена в приложении [Приложение 2]."
    assert extract_citations(answer) == ["Приложение 2"]


def test_extract_citations_deduplicates() -> None:
    """Повторная ссылка на тот же пункт не дублируется."""
    answer = "Так сказано [п. 3.4]. И ещё раз [п. 3.4]."
    assert extract_citations(answer) == ["3.4"]


def test_extract_citations_empty_without_links() -> None:
    """Ответ без ссылок даёт пустой список — это ловит критик."""
    assert extract_citations("Срок составляет два рабочих дня.") == []


def test_collect_context_clauses(chunks: list) -> None:
    """Из фрагментов собираются все допустимые адреса цитирования."""
    allowed = collect_context_clauses(chunks)
    assert allowed == {"3.4", "3.4.2", "Приложение 2"}


def test_invalid_citation_detected(chunks: list) -> None:
    """Ссылка на пункт, которого нет в контексте, выявляется."""
    answer = "Требуется виза финансового блока [п. 9.9]."
    assert find_invalid_citations(answer, chunks) == ["9.9"]


def test_parent_clause_is_valid(chunks: list) -> None:
    """Ссылка на родительский пункт допустима при наличии дочернего."""
    answer = "Общие требования изложены в разделе [п. 3]."
    assert find_invalid_citations(answer, chunks) == []


def test_valid_citations_pass(chunks: list) -> None:
    """Корректные ссылки не помечаются как ошибочные."""
    answer = "Срок — 2 дня [п. 3.4], подпись обязательна [п. 3.4.2]."
    assert find_invalid_citations(answer, chunks) == []
