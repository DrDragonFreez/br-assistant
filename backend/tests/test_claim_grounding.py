"""Замечание модели должно опираться на текст процитированного пункта.

Ссылка на существующий пункт ещё не делает замечание верным. На живом
контуре проверка собранного приказа выдавала три замечания со ссылкой
на п. 10.12: «отсутствует гриф утверждения», «отсутствует отметка об
исполнителе», «отсутствует отметка о поступлении или регистрации» —
будто бы они обязательны для приказов объёмом более трёх страниц.
Пункт 10.12 говорит о пояснительной записке, об этих реквизитах в нём
ни слова. Пункт существует, поэтому проверка ссылки их пропускала, а
требования при этом выдуманы.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.retriever import RetrievedChunk
from app.services.doc_validator import (
    DocumentValidator,
    claim_grounded_in_clause,
    field_stems,
)

# Текст п. 10.12 дословно из проиндексированной инструкции.
CLAUSE_10_12 = RetrievedChunk(
    chunk_id="1",
    point_id="1",
    content=(
        "10.12. Пояснительная записка оформляется отдельным файлом и содержит: "
        "основание подготовки ПД; цели и задачи; основные положения; описание "
        "вносимых изменений; обоснование предлагаемых решений; сведения о ранее "
        "изданных документах; сведения о разногласиях; иные значимые сведения. "
        "Пояснительная записка обязательна для: положений о подразделениях; "
        "приказов по основной деятельности объемом более трех страниц; "
        "распоряжений объемом более трех страниц; приказов и распоряжений, "
        "вносящих изменения; регламентов; проектов модулей; иных установленных "
        "случаев. Для прочих документов необходимость пояснительной записки "
        "определяет ответственное подразделение."
    ),
    score=0.9,
    clause_number="10.12",
    document_code="INSTR-DOC",
    document_title="Инструкция по ДОУ",
)

DRAFT_ORDER = """ПРИКАЗ

О вводе в эксплуатацию подсистемы электронного архива

На основании акта о готовности к вводу в эксплуатацию, содержащего решение о
готовности компонента информационной системы к промышленной эксплуатации,
ПРИКАЗЫВАЮ:

1. Ввести подсистему электронного архива в эксплуатацию.
2. Департаменту информационных технологий обеспечить функционирование
подсистемы в соответствии с эксплуатационной документацией и её сопровождение.
"""

# Ровно те три замечания, что пришли от модели на живом контуре.
FABRICATED_CLAIMS = [
    {
        "field": "гриф утверждения",
        "clause": "10.12",
        "problem": "Отсутствует гриф утверждения.",
        "severity": "minor",
    },
    {
        "field": "отметка об исполнителе",
        "clause": "10.12",
        "problem": "Отсутствует отметка об исполнителе.",
        "severity": "minor",
    },
    {
        "field": "отметка о поступлении или регистрации",
        "clause": "10.12",
        "problem": "Отсутствует отметка о поступлении или регистрации.",
        "severity": "minor",
    },
]


class _StubRetriever:
    """Поиск с заранее известным результатом."""

    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self._chunks = chunks

    async def search(self, query: str, **kwargs: Any) -> list[RetrievedChunk]:
        """Возвращает подготовленные фрагменты."""
        return self._chunks


class _StubGenerator:
    """Генератор, отдающий заданный разбор."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def generate_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        """Возвращает подготовленный ответ модели."""
        return self._payload


async def _validate(
    monkeypatch: pytest.MonkeyPatch,
    violations: list[dict[str, Any]],
    summary: str = "",
    verdict: str = "",
    text: str = DRAFT_ORDER,
) -> Any:
    """Прогоняет проверку проекта приказа с заданными замечаниями модели."""
    monkeypatch.setattr(
        "app.services.doc_validator.get_retriever",
        lambda: _StubRetriever([CLAUSE_10_12]),
    )
    monkeypatch.setattr(
        "app.services.doc_validator.get_generator",
        lambda: _StubGenerator(
            {"summary": summary, "verdict": verdict, "violations": violations}
        ),
    )
    return await DocumentValidator().validate(
        document_text=text,
        allowed_levels=["public"],
    )


class TestFieldStems:
    """Основы смысловых слов поля."""

    def test_service_words_are_ignored(self) -> None:
        """«Отметка» есть почти везде и о требовании ничего не говорит."""
        assert field_stems("отметка об исполнителе") == {"испол"}

    def test_short_words_keep_four_letters(self) -> None:
        """Короткое слово сокращается до четырёх букв."""
        assert field_stems("гриф утверждения") == {"гриф", "утвер"}

    def test_generic_field_has_no_stems(self) -> None:
        """Поле из одних служебных слов сверить не с чем."""
        assert field_stems("содержание документа") == set()


class TestClaimGrounding:
    """Сверка названия поля с текстом процитированного пункта."""

    @pytest.mark.parametrize("claim", FABRICATED_CLAIMS, ids=lambda c: c["field"])
    def test_fabricated_claim_is_not_grounded(self, claim: dict[str, str]) -> None:
        """Реквизита нет в тексте п. 10.12 — замечание не опирается на пункт."""
        assert not claim_grounded_in_clause(claim["field"], "10.12", [CLAUSE_10_12])

    def test_claim_matching_clause_text_is_grounded(self) -> None:
        """Пояснительная записка — ровно то, о чём п. 10.12."""
        assert claim_grounded_in_clause("пояснительная записка", "10.12", [CLAUSE_10_12])

    def test_claim_without_clause_is_kept(self) -> None:
        """Без ссылки на пункт сверять не с чем."""
        assert claim_grounded_in_clause("гриф утверждения", "", [CLAUSE_10_12])

    def test_unknown_clause_is_left_to_reference_check(self) -> None:
        """Пункта нет в контексте — это решает проверка ссылки."""
        assert claim_grounded_in_clause("гриф утверждения", "8.13", [CLAUSE_10_12])


class TestValidatorDropsUngroundedClaims:
    """Замечания без опоры на пункт не доходят до отчёта."""

    @pytest.mark.asyncio
    async def test_fabricated_claims_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Три выдуманных замечания с живого контура в отчёт не попадают."""
        result = await _validate(monkeypatch, FABRICATED_CLAIMS)

        assert not any(item.clause == "10.12" for item in result.violations)
        assert not any(item["clause"] == "10.12" for item in result.recommendations)

    @pytest.mark.asyncio
    async def test_grounded_claim_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Верное замечание по тому же пункту остаётся в отчёте."""
        result = await _validate(
            monkeypatch,
            [
                *FABRICATED_CLAIMS,
                {
                    "field": "пояснительная записка",
                    "clause": "10.12",
                    "problem": "Не приложена пояснительная записка.",
                    "severity": "minor",
                },
            ],
        )

        remaining = [item for item in result.violations if item.clause == "10.12"]
        assert [item.field_name for item in remaining] == ["пояснительная записка"]

    @pytest.mark.asyncio
    async def test_summary_of_dropped_claims_is_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Итог модели о выдуманных замечаниях не доходит до отчёта.

        На живом контуре после отсева замечаний вердикт был
        «соответствует», а итог модели по-прежнему говорил об
        отсутствующих обязательных реквизитах — та же выдумка, только
        строкой выше.
        """
        model_summary = (
            "Документ соответствует основным требованиям инструкции, однако "
            "отсутствуют несколько обязательных реквизитов для приказов по "
            "основной деятельности объемом более трех страниц."
        )
        result = await _validate(monkeypatch, FABRICATED_CLAIMS, summary=model_summary)

        assert result.summary != model_summary
        assert "трех страниц" not in result.summary
        assert result.verdict == "compliant"
        assert result.summary.startswith("Документ соответствует")

    @pytest.mark.asyncio
    async def test_summary_kept_when_nothing_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Все замечания приняты — итог модели остаётся как есть."""
        model_summary = "Не приложена пояснительная записка."
        result = await _validate(
            monkeypatch,
            [
                {
                    "field": "пояснительная записка",
                    "clause": "10.12",
                    "problem": "Не приложена пояснительная записка.",
                    "severity": "minor",
                }
            ],
            summary=model_summary,
            verdict="minor_issues",
        )

        assert result.verdict == "minor_issues"
        assert result.summary == model_summary

    @pytest.mark.asyncio
    async def test_summary_replaced_when_verdict_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Итог «соответствует» не остаётся при вердикте «существенные замечания».

        Модель пишет итог, не видя формальных замечаний: на живом
        контуре исходный модуль с пятнадцатью прочерками получал итог
        «соответствует основным требованиям».
        """
        model_summary = "Документ соответствует основным требованиям инструкции."
        result = await _validate(
            monkeypatch,
            [],
            summary=model_summary,
            verdict="compliant",
            text=DRAFT_ORDER + "\nОтветственный: ____________________\n",
        )

        assert result.verdict == "major_issues"
        assert result.summary != model_summary

    def test_clause_number_in_field_falls_back_to_problem(self) -> None:
        """«Пункт 9.5» в поле — сверяется формулировка проблемы."""
        assert not claim_grounded_in_clause(
            "пункт 10.12", "10.12", [CLAUSE_10_12], "Отсутствует гриф утверждения."
        )
        assert claim_grounded_in_clause(
            "пункт 10.12", "10.12", [CLAUSE_10_12], "Не приложена пояснительная записка."
        )
