"""Ответ «вместе» на предложение модуля запускает заполнение в диалоге.

Узел offer спрашивает: «Заполните модуль самостоятельно… либо подготовлю
проект вместе с вами. Как удобнее?». Ответ словами раньше уходил в
модель маршрутизатора, та относила его к болтовне, и сотрудник получал
справку «Что я умею» вместо первого вопроса по модулю.
"""

from __future__ import annotations

import pytest

from app.agents.router import accepts_joint_filling
from app.api.v1.chat import resolve_template_context

OFFER = {"offered_template": {"code": "MOD-ORDER-IS-DEPLOY", "name": "Приказ о вводе в эксплуатацию"}}


@pytest.mark.parametrize(
    "reply",
    ["Давай вместе", "вместе", "Заполни", "подготовь, пожалуйста", "давайте заполним", "второй вариант", "с вами"],
)
def test_joint_filling_is_recognised(reply: str) -> None:
    """Короткое согласие распознаётся."""
    assert accepts_joint_filling(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Заполню сам",
        "самостоятельно",
        "нет, не надо",
        "Какие сроки согласования приказа по основной деятельности и кто визирует его вместе с юристами?",
        "",
    ],
)
def test_other_replies_are_not_consent(reply: str) -> None:
    """Выбор «сам», отказ и новый длинный вопрос согласием не считаются."""
    assert not accepts_joint_filling(reply)


def test_consent_after_offer_starts_filling() -> None:
    """После предложения модуля «вместе» идёт тем же путём, что кнопка."""
    code, question = resolve_template_context(
        message="Давай вместе", requested_code="", previous_status="answered", previous_extra=OFFER
    )
    assert code == "MOD-ORDER-IS-DEPLOY"
    assert question == "Заполним бланк «Приказ о вводе в эксплуатацию»"


def test_consent_without_offer_changes_nothing() -> None:
    """Без предложенного модуля реплика уходит как есть."""
    assert resolve_template_context(
        message="Давай вместе", requested_code="", previous_status="answered", previous_extra={}
    ) == ("", "Давай вместе")


def test_filling_continues_without_client_code() -> None:
    """Шаг заполнения продолжается, даже если клиент не прислал код модуля."""
    code, question = resolve_template_context(
        message="ПЭА",
        requested_code="",
        previous_status="template_filling",
        previous_extra={"template_code": "MOD-ORDER-IS-DEPLOY"},
    )
    assert (code, question) == ("MOD-ORDER-IS-DEPLOY", "ПЭА")


def test_client_code_wins() -> None:
    """Код модуля из запроса клиента имеет приоритет."""
    assert resolve_template_context(
        message="вместе", requested_code="MOD-DIR-AUDIT", previous_status="answered", previous_extra=OFFER
    ) == ("MOD-DIR-AUDIT", "вместе")
