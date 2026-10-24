"""Тесты запасного переранжирования.

Реранкер bge-reranker-v2-m3 требует отдельного сервиса и около 2,5 ГБ
памяти, поэтому при его отключении граф переходит на запасной алгоритм.
Запасной путь — не декоративный: именно он работает на машине без
свободной памяти, то есть на демонстрации. Ошибка в нём напрямую
выбрасывает из контекста фрагмент с ответом, и помощник отказывается
отвечать на вопрос, ответ на который в базе знаний есть.
"""

from __future__ import annotations

from app.rag.reranker import Reranker, _stems
from app.rag.retriever import RetrievedChunk


def _chunk(number: str, content: str, score: float) -> RetrievedChunk:
    """Собирает фрагмент для проверки.

    Args:
        number: Номер пункта.
        content: Текст фрагмента.
        score: Оценка гибридного поиска.

    Returns:
        Фрагмент выдачи.
    """
    return RetrievedChunk(
        chunk_id=f"chunk-{number}",
        point_id=f"point-{number}",
        content=content,
        score=score,
        clause_number=number,
    )


class TestStems:
    """Сравнение слов по усечённым основам."""

    def test_word_forms_share_a_stem(self) -> None:
        """Разные формы одного слова дают одну основу."""
        assert _stems("регистрируется") == _stems("регистрация")
        assert _stems("исходящий") == _stems("исходящего")

    def test_clause_numbers_are_not_truncated(self) -> None:
        """Номера пунктов остаются различимыми.

        Усечение «10.15» до пяти символов дало бы «10.15», а «10.3»
        осталось бы «10.3» — но проверять это нужно явно: склейка номеров
        сделала бы разные пункты неразличимыми при сравнении.
        """
        assert _stems("10.3") != _stems("10.15")


class TestFallbackKeepsTheAnswer:
    """Фрагмент с ответом не должен выпадать из выдачи."""

    def test_relevant_clause_survives_diversity(self) -> None:
        """Самый релевантный фрагмент остаётся в выдаче.

        Воспроизводит реальный случай: на вопрос о сроке регистрации
        исходящего документа поиск ставил п. 7.1 вторым, а отбор
        оставлял пять других фрагментов — ответ в контекст не попадал.
        Причина была двойная: отбор шёл по исходной оценке без
        пересчёта, а пословное сравнение не связывало «регистрируется»
        с «регистрация».
        """
        query = "В какой срок регистрируется исходящий документ?"
        chunks = [
            _chunk("5.6", "Регистрация входящего документа и внесение в РК.", 0.74),
            _chunk(
                "7.1",
                "Регистрация исходящего документа производится не позднее "
                "рабочего дня, следующего за днем его подписания.",
                0.73,
            ),
            _chunk("10.9", "Срок рассмотрения проекта документа в подразделении.", 0.70),
            _chunk("10.3", "Максимальный срок рассмотрения проекта составляет дни.", 0.69),
            _chunk("8.27", "Срок оформления приложения к документу.", 0.68),
            _chunk("5.11", "Срок передачи документа исполнителю.", 0.67),
        ]

        selected = Reranker()._fallback_rerank(query, chunks, top_n=5)

        assert "7.1" in {chunk.clause_number for chunk in selected}

    def test_selection_is_recomputed_after_each_pick(self) -> None:
        """Отбор учитывает дублирование, а не только исходный порядок.

        Два почти одинаковых фрагмента не должны занимать выдачу вместе,
        пока есть содержательно другой кандидат.
        """
        query = "порядок визирования проекта приказа"
        duplicate = "Визирование проекта приказа выполняется подразделением."
        chunks = [
            _chunk("1.1", duplicate, 0.80),
            _chunk("1.2", duplicate + " ", 0.79),
            _chunk("2.1", "Лингвистическая экспертиза проекта приказа.", 0.60),
        ]

        selected = Reranker()._fallback_rerank(query, chunks, top_n=2)
        numbers = [chunk.clause_number for chunk in selected]

        assert "2.1" in numbers

    def test_requested_count_is_respected(self) -> None:
        """Возвращается не больше запрошенного числа фрагментов."""
        query = "срок регистрации документа"
        chunks = [_chunk(f"3.{i}", f"Положение о сроках номер {i}.", 0.5) for i in range(8)]

        selected = Reranker()._fallback_rerank(query, chunks, top_n=3)

        assert len(selected) == 3

    def test_empty_query_returns_head_of_list(self) -> None:
        """Запрос без значимых слов не ломает отбор."""
        chunks = [_chunk("4.1", "Текст положения.", 0.5), _chunk("4.2", "Другое.", 0.4)]

        selected = Reranker()._fallback_rerank("и в на", chunks, top_n=1)

        assert len(selected) == 1
