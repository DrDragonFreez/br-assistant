"""Тесты чтения ссылок из базы при открытии истории диалога.

Ссылки приходят из двух источников. В живом ответе — словарями из
графа. В истории — объектами ORM, прочитанными из базы. Пока схема
ссылки не умела читать второй источник, открытие любого сохранённого
диалога с ответом возвращало ошибку сервиса: пустой диалог
открывался, диалог с ответом — нет.

В таблице хранится необходимый минимум полей, поэтому проверяется и
восполнение производных: подписи источника, хлебных крошек и процента
релевантности. Без них ссылка в истории выглядела бы иначе, чем в
только что полученном ответе.
"""

from __future__ import annotations

import uuid

from app.api.v1.schemas import CitationResponse


class _StoredCitation:
    """Ссылка в том виде, в каком она лежит в базе."""

    def __init__(self, **overrides: object) -> None:
        self.chunk_id = uuid.uuid4()
        self.document_code = "INSTR-DOC"
        self.document_title = "Инструкция по документационному обеспечению"
        self.version_label = "ред. 1"
        self.clause_number = "10.3"
        self.section_title = "Подготовка и согласование"
        self.page_number = 42
        self.quote = "10.3. Максимальный срок рассмотрения составляет…"
        self.relevance = 0.7667
        self.rank = 1
        for key, value in overrides.items():
            setattr(self, key, value)


class TestCitationFromDatabase:
    """Чтение ссылки из объекта ORM."""

    def test_stored_citation_is_read(self) -> None:
        """Ссылка из базы разбирается без ошибки."""
        citation = CitationResponse.model_validate(_StoredCitation())

        assert citation.document_code == "INSTR-DOC"
        assert citation.clause_number == "10.3"
        assert citation.page_number == 42

    def test_chunk_id_becomes_string(self) -> None:
        """Идентификатор фрагмента приводится к строке."""
        stored = _StoredCitation()

        citation = CitationResponse.model_validate(stored)

        assert citation.chunk_id == str(stored.chunk_id)

    def test_missing_chunk_id_is_empty(self) -> None:
        """Удалённый фрагмент не ломает историю.

        Фрагмент исчезает вместе с устаревшей редакцией документа, а
        ссылка на него в истории остаётся.
        """
        citation = CitationResponse.model_validate(_StoredCitation(chunk_id=None))

        assert citation.chunk_id == ""


class TestDerivedFields:
    """Поля, которых в базе нет, собираются из тех, что есть."""

    def test_percent_is_derived_from_relevance(self) -> None:
        """Процент считается из доли релевантности."""
        citation = CitationResponse.model_validate(_StoredCitation(relevance=0.7667))

        assert citation.relevance_percent == 77

    def test_label_contains_document_clause_and_page(self) -> None:
        """Подпись источника собирается в том же виде, что в ответе."""
        citation = CitationResponse.model_validate(_StoredCitation())

        assert "Инструкция по документационному обеспечению" in citation.citation_label
        assert "п. 10.3" in citation.citation_label
        assert "с. 42" in citation.citation_label

    def test_breadcrumbs_lead_to_the_clause(self) -> None:
        """Хлебные крошки ведут от раздела к пункту."""
        citation = CitationResponse.model_validate(_StoredCitation())

        assert citation.breadcrumbs == "Подготовка и согласование → п. 10.3"

    def test_clause_numbers_fall_back_to_the_main_clause(self) -> None:
        """Перечень пунктов не бывает пустым при известном пункте."""
        citation = CitationResponse.model_validate(_StoredCitation())

        assert citation.clause_numbers == ["10.3"]

    def test_explicit_values_are_kept(self) -> None:
        """Готовые значения из графа не перезаписываются."""
        citation = CitationResponse.model_validate(
            {
                "clause_number": "10.3",
                "clause_numbers": ["10.3", "10.4"],
                "citation_label": "Готовая подпись",
                "breadcrumbs": "Свои крошки",
                "relevance": 0.5,
                "relevance_percent": 95,
            }
        )

        assert citation.citation_label == "Готовая подпись"
        assert citation.breadcrumbs == "Свои крошки"
        assert citation.relevance_percent == 95
        assert citation.clause_numbers == ["10.3", "10.4"]
