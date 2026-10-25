"""Тесты узла перехода от ответа к действию.

Сценарии организаторов требуют, чтобы разъяснение заканчивалось
действием: подходящим модулем, перечнем визирующих подразделений и
вопросом, заполнять самому или вместе. Узел ``offer`` добавляется после
критика и дополняет готовый ответ, ничего в нём не переписывая.

Проверяется в первую очередь, когда узел *не* должен срабатывать:
предложение не по делу хуже его отсутствия, а предложение в ответ на
честный отказ прямо противоречит отказу.
"""

from __future__ import annotations

from app.agents.graph import format_approval_route, needs_action_offer
from app.agents.state import AnswerStatus, Intent


def _state(**overrides: object) -> dict[str, object]:
    """Готовит состояние графа с состоявшимся ответом.

    Args:
        overrides: Поля, которые нужно переопределить.

    Returns:
        Состояние для передачи в проверку.
    """
    state: dict[str, object] = {
        "answer_status": AnswerStatus.ANSWERED.value,
        "intent": Intent.QUESTION.value,
        "question": "Мне нужно подготовить приказ о вводе системы в эксплуатацию",
        "document_kind": "order",
        "template_candidates": [],
    }
    state.update(overrides)
    return state


class TestWhenOfferIsAdded:
    """Условия, при которых к ответу добавляется модуль."""

    def test_preparation_question_gets_offer(self) -> None:
        """Сотрудник готовит приказ — модуль предлагается."""
        assert needs_action_offer(_state()) is True

    def test_document_check_always_gets_offer(self) -> None:
        """Проверке документа нужно приглашение загрузить файл."""
        assert (
            needs_action_offer(
                _state(
                    intent=Intent.DOCUMENT_CHECK.value,
                    question="Проверь мой приказ",
                )
            )
            is True
        )

    def test_approval_route_question_gets_offer(self) -> None:
        """Вопрос о согласовании тоже ведёт к подготовке документа."""
        assert (
            needs_action_offer(
                _state(
                    intent=Intent.APPROVAL_ROUTE.value,
                    question="Надо оформить приказ, кто его визирует?",
                )
            )
            is True
        )


class TestWhenOfferIsWithheld:
    """Условия, при которых предлагать нечего."""

    def test_refusal_gets_no_offer(self) -> None:
        """К честному отказу бланк не предлагается.

        Иначе помощник сообщал бы, что данных нет, и тут же что-то
        предлагал — это противоречие в одном ответе.
        """
        assert (
            needs_action_offer(_state(answer_status=AnswerStatus.NO_CONTEXT.value))
            is False
        )

    def test_question_about_conditions_gets_no_offer(self) -> None:
        """Вопрос об условиях не является подготовкой документа."""
        assert (
            needs_action_offer(
                _state(
                    question="В каких случаях требуется сопроводительное письмо?",
                    document_kind="letter",
                )
            )
            is False
        )

    def test_unknown_document_kind_gets_no_offer(self) -> None:
        """Без вида документа подобрать модуль нечем."""
        assert needs_action_offer(_state(document_kind="")) is False

    def test_existing_candidates_are_not_duplicated(self) -> None:
        """Если бланк уже подобран ветвью шаблонов, узел не вмешивается."""
        assert (
            needs_action_offer(_state(template_candidates=[{"code": "MOD-ORDER"}]))
            is False
        )

    def test_template_request_is_handled_by_its_own_branch(self) -> None:
        """Прямой запрос бланка идёт своей ветвью, а не через дополнение."""
        assert (
            needs_action_offer(
                _state(
                    intent=Intent.TEMPLATE_REQUEST.value,
                    question="Подготовь приказ",
                )
            )
            is False
        )


class TestApprovalRouteFormatting:
    """Перечень визирования из матрицы обязательного визирования."""

    def test_rows_are_sorted_and_terms_shown(self) -> None:
        """Строки идут в установленном порядке, срок указан рядом."""
        text = format_approval_route(
            [
                {"order": 2, "term_days": 10, "department": "Департамент ИТ"},
                {"order": 1, "term_days": 10, "department": "Юридическое подразделение"},
            ]
        )

        assert "1. Юридическое подразделение — 10 раб. дн." in text
        assert "2. Департамент ИТ — 10 раб. дн." in text
        assert text.index("Юридическое") < text.index("Департамент ИТ")

    def test_rows_without_department_are_skipped(self) -> None:
        """Пункт с пустым подразделением хуже короткого перечня."""
        text = format_approval_route(
            [
                {"order": 1, "department": "Департамент ДОУ"},
                {"order": 2, "department": ""},
            ]
        )

        assert text.count("\n") == 1
        assert "Департамент ДОУ" in text

    def test_empty_route_gives_empty_text(self) -> None:
        """Пустая матрица не порождает пустого заголовка."""
        assert format_approval_route([]) == ""
