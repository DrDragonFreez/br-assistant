"""Тесты поиска и нарезки документов.

Проверяется главное свойство чанкера: фрагменты сохраняют привязку к
номерам пунктов инструкции, без которой невозможны корректные цитаты.
"""

from __future__ import annotations

from app.rag.bm25 import BM25Encoder, tokenize
from app.rag.chunker import ClauseAwareChunker
from app.rag.retriever import ANNEX_MENTION_RE, CLAUSE_MENTION_RE


def test_chunker_preserves_clause_numbers(instruction_pages: list) -> None:
    """Каждый пункт инструкции попадает в отдельный фрагмент с номером."""
    chunks = ClauseAwareChunker().split(instruction_pages)
    numbers = {chunk.clause_number for chunk in chunks if chunk.clause_number}
    assert "3.4" in numbers


def test_merged_chunk_keeps_every_clause_number(instruction_pages: list) -> None:
    """Объединение коротких пунктов не теряет ни одного номера.

    Короткий пункт («3.1. Общие положения.») присоединяется к следующему,
    чтобы не давать шум в выдаче. Если при этом потерять номера, ответ
    сошлётся на соседний пункт, а поиск по номеру не найдёт положение —
    это и есть грубая ошибка цитирования.
    """
    chunks = ClauseAwareChunker().split(instruction_pages)
    covered = {number for chunk in chunks for number in chunk.clause_numbers}

    assert {"3.1", "3.4", "3.4.2"} <= covered


def test_merged_chunk_address_belongs_to_main_clause(instruction_pages: list) -> None:
    """Основной адрес достаётся пункту с содержательным текстом."""
    chunks = ClauseAwareChunker().split(instruction_pages)
    merged = next(chunk for chunk in chunks if "3.1" in chunk.clause_numbers)

    assert merged.clause_number == "3.4"
    assert merged.citation_label == "п. 3.1, 3.4"
    assert merged.extra["clauses"] == ["3.1", "3.4"]


def test_chunker_detects_annex(instruction_pages: list) -> None:
    """Приложение распознаётся и помечается номером."""
    chunks = ClauseAwareChunker().split(instruction_pages)
    assert any(chunk.annex_number == "2" for chunk in chunks)


def test_chunker_sets_page_numbers(instruction_pages: list) -> None:
    """У фрагментов проставлены номера страниц для цитат."""
    chunks = ClauseAwareChunker().split(instruction_pages)
    assert all(chunk.page_number is not None for chunk in chunks)


def test_chunker_citation_label(instruction_pages: list) -> None:
    """Подпись цитаты формируется из номера пункта."""
    chunks = ClauseAwareChunker().split(instruction_pages)
    labels = [chunk.citation_label for chunk in chunks]
    assert any(label.startswith("п. ") for label in labels)


def test_clause_mention_detected_in_query() -> None:
    """Прямая ссылка на пункт в запросе распознаётся для приоритетного поиска."""
    assert CLAUSE_MENTION_RE.findall("что написано в пункте 3.4?") == ["3.4"]
    assert CLAUSE_MENTION_RE.findall("см. п. 4.2.1") == ["4.2.1"]
    assert ANNEX_MENTION_RE.findall("форма из приложения 2") == ["2"]


def test_tokenizer_keeps_clause_numbers() -> None:
    """Номер пункта не разбивается по точкам — иначе ломается точный поиск."""
    assert "3.4.2" in tokenize("Согласно пункту 3.4.2 инструкции")


def test_tokenizer_removes_stopwords() -> None:
    """Служебные слова отбрасываются."""
    tokens = tokenize("и в во не что он на я с со как")
    assert tokens == []


def test_bm25_encodes_query_and_document() -> None:
    """Кодировщик строит непустые разреженные векторы после обучения."""
    encoder = BM25Encoder().fit(
        [
            "Срок согласования служебной записки составляет два рабочих дня",
            "Приказ согласуется юридическим департаментом",
        ]
    )
    assert encoder.is_fitted
    assert not encoder.encode_query("срок согласования записки").is_empty
    assert not encoder.encode_document("Срок согласования записки").is_empty


def test_bm25_empty_before_fit() -> None:
    """До обучения кодировщик возвращает пустые векторы, а не падает."""
    encoder = BM25Encoder()
    assert encoder.encode_query("любой запрос").is_empty
