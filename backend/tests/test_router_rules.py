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


class TestModuleQuestionsAreNotTemplateRequests:
    """Вопрос о модуле — это вопрос, а не заказ бланка.

    Слово «модуль» в Инструкции означает утверждённую структуру
    документа. Пока оно стояло в шаблонах просьбы о бланке отдельным
    словом, «что такое модуль документа» попадало в ветку выдачи бланка
    и помощник переспрашивал тип документа вместо ответа со ссылкой
    на пункт. Ветка про модули при этом была недостижима.
    """

    @pytest.mark.parametrize(
        "question",
        [
            "Что такое модуль документа?",
            "Где найти реестр модулей документов?",
            "Какие требования к модулю приказа?",
        ],
    )
    def test_module_question_is_classified_as_question(self, question: str) -> None:
        """Вопрос о модулях получает намерение «вопрос» без уточнения."""
        result = classify_by_rules(question)

        assert result is not None
        assert result["intent"] == Intent.QUESTION.value
        assert result["needs_clarification"] is False

    @pytest.mark.parametrize(
        "question",
        [
            "Заполни модуль приказа",
            "Подготовь документ по модулю",
            "Сформируй проект по модулю распоряжения",
        ],
    )
    def test_request_to_build_by_module_is_template_request(self, question: str) -> None:
        """Просьба изготовить документ по модулю ведёт к подбору бланка."""
        result = classify_by_rules(question)

        assert result is not None
        assert result["intent"] == Intent.TEMPLATE_REQUEST.value


class TestQuestionAboutFormIsNotRequestForForm:
    """Вопрос о требованиях к бланку — не заказ бланка.

    Слова «бланк», «форма», «шаблон», «модуль» встречаются и в вопросе
    о требованиях, и в просьбе выдать черновик, поэтому различается
    формулировка, а не словарь. Пока различия не было, помощник на
    вопрос «какие зоны должны быть предусмотрены в электронном бланке»
    предлагал выбрать тип документа вместо ссылки на пункт.
    """

    @pytest.mark.parametrize(
        "question",
        [
            "Какие зоны должны быть предусмотрены в электронном бланке документа?",
            "Какие требования к бланку письма?",
            "По какой форме составляется протокол?",
            "Как оформляется приказ?",
        ],
    )
    def test_question_about_requirements_goes_to_search(self, question: str) -> None:
        """Вопрос о требованиях получает намерение «вопрос»."""
        result = classify_by_rules(question)

        assert result is not None
        assert result["intent"] == Intent.QUESTION.value
        assert result["needs_clarification"] is False

    @pytest.mark.parametrize(
        "question",
        [
            "Подготовь приказ о назначении ответственного",
            "Составь служебную записку",
            "Нужен бланк служебной записки",
            "Заполни форму служебной записки",
            "Покажи шаблон приказа",
        ],
    )
    def test_request_to_produce_document_goes_to_templates(self, question: str) -> None:
        """Побуждение изготовить документ ведёт к подбору бланка."""
        result = classify_by_rules(question)

        assert result is not None
        assert result["intent"] == Intent.TEMPLATE_REQUEST.value

    def test_reflexive_verb_is_not_a_request(self) -> None:
        """Страдательная форма глагола не означает просьбы изготовить.

        «Составляется», «оформляется», «создаётся» описывают порядок
        работы; побуждение — «составь», «оформи», «создай».
        """
        descriptive = classify_by_rules("По какой форме составляется протокол?")
        imperative = classify_by_rules("Составь протокол")

        assert descriptive is not None
        assert imperative is not None
        assert descriptive["intent"] == Intent.QUESTION.value
        assert imperative["intent"] == Intent.TEMPLATE_REQUEST.value
