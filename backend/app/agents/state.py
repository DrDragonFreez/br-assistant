"""Состояние графа обработки обращения.

LangGraph передаёт один словарь состояния между узлами. Здесь он описан как
TypedDict: поля типизированы, но остаются сериализуемыми, что нужно для
чекпоинтов и для передачи состояния между процессами.

Состояние намеренно плоское — так проще отлаживать и логировать путь обращения
через граф, а на защите наглядно показать, какой узел что добавил.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class Intent(StrEnum):
    """Намерение сотрудника, определённое маршрутизатором."""

    QUESTION = "question"
    """Вопрос о требованиях инструкции."""

    TEMPLATE_REQUEST = "template_request"
    """Запрос бланка или черновика документа."""

    DOCUMENT_CHECK = "document_check"
    """Проверка готового документа."""

    APPROVAL_ROUTE = "approval_route"
    """Вопрос о порядке и сроках согласования."""

    SMALLTALK = "smalltalk"
    """Приветствие или вопрос о возможностях помощника."""

    OUT_OF_SCOPE = "out_of_scope"
    """Тема вне компетенции помощника."""


class AnswerStatus(StrEnum):
    """Исход обработки обращения."""

    ANSWERED = "answered"
    """Ответ сформирован и подтверждён источниками."""

    NO_CONTEXT = "no_context"
    """Честный отказ: в базе знаний нет информации."""

    CLARIFICATION = "clarification"
    """Задан уточняющий вопрос."""

    TEMPLATE_READY = "template_ready"
    """Шаблон найден, черновик сформирован."""

    TEMPLATE_FILLING = "template_filling"
    """Идёт пошаговое заполнение шаблона."""

    INFO = "info"
    """Справочный ответ без обращения к базе знаний."""

    ERROR = "error"
    """Обработка завершилась ошибкой."""


class CriticVerdict(StrEnum):
    """Вердикт контролёра качества ответа."""

    APPROVED = "approved"
    REVISED = "needs_revision"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class Citation(BaseModel):
    """Ссылка на положение инструкции, использованное в ответе."""

    chunk_id: str = ""
    document_code: str = ""
    document_title: str = ""
    version_label: str = ""
    clause_number: str = ""
    clause_numbers: list[str] = Field(default_factory=list)
    section_title: str = ""
    breadcrumbs: str = ""
    page_number: int | None = None
    annex_number: str = ""
    citation_label: str = ""
    quote: str = ""
    relevance: float = 0.0
    relevance_percent: int = 0
    rank: int = 0


class ClarifyingQuestion(BaseModel):
    """Уточняющий вопрос к сотруднику."""

    question: str
    field: str = ""
    options: list[str] = Field(default_factory=list)


class TemplateField(BaseModel):
    """Поле шаблона документа, которое нужно заполнить."""

    name: str
    label: str = ""
    type: str = "string"
    required: bool = False
    question: str = ""
    value: Any = None


def _keep_last(_current: Any, update: Any) -> Any:
    """Редьюсер: последнее записанное значение побеждает.

    Нужен для полей, которые перезаписываются несколькими узлами
    (например, ответ может быть переписан критиком).

    Args:
        _current: Текущее значение.
        update: Новое значение.

    Returns:
        Новое значение.
    """
    return update


def _append_trace(current: list[str] | None, update: list[str]) -> list[str]:
    """Редьюсер: накапливает трассу прохождения узлов.

    Args:
        current: Уже накопленная трасса.
        update: Новые записи.

    Returns:
        Объединённый список.
    """
    return [*(current or []), *update]


class AssistantState(TypedDict, total=False):
    """Состояние графа обработки обращения.

    Поля сгруппированы по стадиям: вход, маршрутизация, поиск, генерация,
    контроль качества, работа с документами, служебное.
    """

    # ── Вход ──
    question: str
    session_id: str
    user_id: str
    user_role: str
    allowed_levels: list[str]
    history: list[dict[str, str]]
    request_id: str
    attached_document_text: str

    # ── Маршрутизация ──
    intent: Annotated[str, _keep_last]
    needs_clarification: Annotated[bool, _keep_last]
    missing_info: list[str]
    document_kind: str
    mentioned_clauses: list[str]

    # ── Поиск ──
    retrieved_chunks: Annotated[list[Any], _keep_last]
    reranked_chunks: Annotated[list[Any], _keep_last]
    top_score: Annotated[float, _keep_last]
    retrieval_mode: str

    # ── Генерация ──
    answer: Annotated[str, _keep_last]
    citations: Annotated[list[dict[str, Any]], _keep_last]
    answer_status: Annotated[str, _keep_last]
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    used_fallback: bool

    # ── Контроль качества ──
    critic_verdict: Annotated[str, _keep_last]
    critic_comment: str
    unsupported_claims: list[str]
    invalid_citations: list[str]
    regeneration_count: Annotated[int, _keep_last]

    # ── Уточнения ──
    clarifying_questions: list[dict[str, Any]]

    # ── Документы ──
    template_code: str
    template_name: str
    template_fields: list[dict[str, Any]]
    filled_fields: dict[str, Any]
    missing_fields: list[str]
    template_candidates: list[dict[str, Any]]
    generated_document_id: str
    approval_route: list[dict[str, Any]]

    # ── Служебное ──
    node_trace: Annotated[list[str], _append_trace]
    error: str
    latency_ms: int


def create_initial_state(
    *,
    question: str,
    session_id: str,
    user_id: str,
    user_role: str,
    allowed_levels: list[str],
    history: list[dict[str, str]] | None = None,
    request_id: str = "",
    attached_document_text: str = "",
    filled_fields: dict[str, Any] | None = None,
    template_code: str = "",
) -> AssistantState:
    """Создаёт начальное состояние графа.

    Args:
        question: Текст обращения.
        session_id: Идентификатор диалога.
        user_id: Идентификатор пользователя.
        user_role: Роль пользователя.
        allowed_levels: Грифы документов, доступные роли.
        history: Предыдущие реплики диалога.
        request_id: Сквозной идентификатор запроса.
        attached_document_text: Текст загруженного черновика, если есть.
        filled_fields: Уже собранные поля шаблона (продолжение заполнения).
        template_code: Код шаблона, если заполнение уже начато.

    Returns:
        Инициализированное состояние.
    """
    return AssistantState(
        question=question,
        session_id=session_id,
        user_id=user_id,
        user_role=user_role,
        allowed_levels=allowed_levels,
        history=history or [],
        request_id=request_id,
        attached_document_text=attached_document_text,
        intent="",
        needs_clarification=False,
        missing_info=[],
        document_kind="",
        mentioned_clauses=[],
        retrieved_chunks=[],
        reranked_chunks=[],
        top_score=0.0,
        retrieval_mode="",
        answer="",
        citations=[],
        answer_status="",
        model_name="",
        prompt_tokens=0,
        completion_tokens=0,
        used_fallback=False,
        critic_verdict="",
        critic_comment="",
        unsupported_claims=[],
        invalid_citations=[],
        regeneration_count=0,
        clarifying_questions=[],
        template_code=template_code,
        template_name="",
        template_fields=[],
        filled_fields=filled_fields or {},
        missing_fields=[],
        template_candidates=[],
        generated_document_id="",
        approval_route=[],
        node_trace=[],
        error="",
        latency_ms=0,
    )
