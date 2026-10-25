"""Реквизиты, которые появляются после автора проекта.

Перечень реквизитов [п. 8.2] описывает готовый документ, а порядок
подготовки [п. 10.1] ставит подписание и регистрацию после того, как
проект ушёл от исполнителя: дата и номер вносятся в РК при регистрации
[п. 7.1]. Поэтому их отсутствие в проекте — состояние, а не нарушение.

До правки проверка считала отсутствие даты и подписи критичным, и
собранный по модулю организаторов черновик получал от помощника два
критичных несоответствия сразу после того, как помощник сам его и
собрал. Здесь закреплено обратное поведение.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag.retriever import RetrievedChunk
from app.services.doc_validator import (
    DEFERRED_REQUISITES,
    DocumentValidator,
    Violation,
    check_formal_requisites,
    compute_verdict,
)

# Проект приказа по модулю: содержательная часть есть, адресат и
# заголовок на месте, подписи и регистрационных данных нет.
DRAFT_ORDER = """ПРИКАЗ

О вводе в эксплуатацию подсистемы электронного архива

На основании акта о готовности к вводу в эксплуатацию, содержащего решение о
готовности компонента информационной системы к промышленной эксплуатации,
ПРИКАЗЫВАЮ:

1. Ввести подсистему электронного архива в эксплуатацию.
2. Департаменту информационных технологий обеспечить функционирование
подсистемы в соответствии с эксплуатационной документацией и её сопровождение.
"""


class TestDraftIsNotBlamedForFutureStages:
    """Проект не бракуется за то, чего в нём ещё не может быть."""

    def test_missing_date_and_signature_are_informational(self) -> None:
        """Дата, номер и подпись отмечаются как реквизиты следующих этапов."""
        violations = check_formal_requisites(DRAFT_ORDER)
        by_field = {item.field_name: item for item in violations}

        assert set(by_field) == {
            "дата документа",
            "регистрационный номер документа",
            "подпись",
        }
        assert all(item.severity == "info" for item in violations)

    def test_informational_marks_carry_the_stage_clause(self) -> None:
        """Отметка ссылается на пункт, устанавливающий этап."""
        by_field = {item.field_name: item for item in check_formal_requisites(DRAFT_ORDER)}

        assert by_field["дата документа"].clause == "7.1"
        assert by_field["регистрационный номер документа"].clause == "7.1"
        assert by_field["подпись"].clause == "10.1"
        # Сам реквизит остаётся реквизитом: ссылка на перечень сохранена.
        assert all("8.2" in item.requirement for item in by_field.values())
        assert all(item.recommendation for item in by_field.values())

    def test_draft_without_signature_is_compliant(self) -> None:
        """Проект без подписи и регистрации проходит проверку."""
        verdict, score = compute_verdict(check_formal_requisites(DRAFT_ORDER))

        assert verdict == "compliant"
        assert score == 1.0

    def test_informational_marks_do_not_lower_the_score(self) -> None:
        """Отметка о следующем этапе не отнимает баллов."""
        violations = [
            *check_formal_requisites(DRAFT_ORDER),
            Violation(severity="minor", field_name="оформление", problem="лишние пробелы"),
        ]
        verdict, score = compute_verdict(violations)

        assert verdict == "minor_issues"
        assert score == pytest.approx(0.97)

    def test_other_requisites_stay_blocking(self) -> None:
        """Послабление касается только реквизитов следующих этапов."""
        text = DRAFT_ORDER.replace("Департаменту информационных технологий", "Исполнителю")
        violations = check_formal_requisites(text)
        critical = [item for item in violations if item.severity == "critical"]

        assert [item.field_name for item in critical] == ["адресат"]
        assert compute_verdict(violations)[0] == "major_issues"

    def test_deferred_set_covers_only_signing_and_registration(self) -> None:
        """Послабление распространяется ровно на три реквизита."""
        assert set(DEFERRED_REQUISITES) == {"date", "number", "signature"}


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


CHUNK = RetrievedChunk(
    chunk_id="1",
    point_id="1",
    content=(
        "7.1. Регистрация исходящего документа производится не позднее рабочего дня, "
        "следующего за днем его подписания. В РК вносятся дата и номер."
    ),
    score=0.9,
    clause_number="7.1",
    document_code="INSTR-DOC",
    document_title="Инструкция по ДОУ",
)


class TestModelClaimAboutDeferredRequisite:
    """Замечание модели о дате и подписи не возвращает проблему обратно.

    Модель видит тот же пробел, что и формальная проверка, и охотно
    называет его критичным. Если пропустить такое замечание в отчёт,
    один и тот же реквизит окажется одновременно сведением о следующем
    этапе и критичным нарушением, а вердикт снова станет «требует
    доработки».
    """

    @pytest.mark.asyncio
    async def test_duplicate_claim_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Замечание о дате и подписи в перечень не попадает."""
        monkeypatch.setattr(
            "app.services.doc_validator.get_retriever",
            lambda: _StubRetriever([CHUNK]),
        )
        monkeypatch.setattr(
            "app.services.doc_validator.get_generator",
            lambda: _StubGenerator(
                {
                    "summary": "",
                    "violations": [
                        {
                            "severity": "critical",
                            "field": "Дата документа",
                            "problem": "отсутствует",
                            "clause": "7.1",
                        },
                        {
                            "severity": "critical",
                            "field": "подпись",
                            "problem": "отсутствует",
                            "clause": "7.1",
                        },
                    ],
                }
            ),
        )

        result = await DocumentValidator().validate(
            document_text=DRAFT_ORDER,
            allowed_levels=["public"],
        )

        assert result.verdict == "compliant"
        assert result.critical_count == 0
        assert result.recommendations == []
        assert {item.severity for item in result.violations} == {"info"}
        assert "следующих этапах" in result.summary or "регистрации" in result.summary

    @pytest.mark.asyncio
    async def test_claim_about_content_field_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Замечание о содержательном поле остаётся в отчёте.

        «Дата ввода в эксплуатацию» — поле приказа, а не реквизит
        бланка: сопоставление идёт по точному названию, иначе под
        послабление попало бы любое упоминание даты.
        """
        monkeypatch.setattr(
            "app.services.doc_validator.get_retriever",
            lambda: _StubRetriever([CHUNK]),
        )
        monkeypatch.setattr(
            "app.services.doc_validator.get_generator",
            lambda: _StubGenerator(
                {
                    "summary": "",
                    "violations": [
                        {
                            "severity": "major",
                            "field": "дата ввода в эксплуатацию",
                            "problem": "не указана",
                            "clause": "7.1",
                        }
                    ],
                }
            ),
        )

        result = await DocumentValidator().validate(
            document_text=DRAFT_ORDER,
            allowed_levels=["public"],
        )

        assert result.verdict == "minor_issues"
        assert [item["clause"] for item in result.recommendations] == ["7.1"]
        assert any(
            item.field_name == "дата ввода в эксплуатацию" for item in result.violations
        )
