"""Схемы запросов и ответов API.

Все модели — Pydantic v2. Исходящие схемы не содержат служебных полей
(хеши паролей, внутренние идентификаторы точек Qdrant, пути к файлам
на диске): наружу отдаётся только то, что нужно интерфейсу.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.rbac import AccessLevel, Role


class ORMModel(BaseModel):
    """Базовая схема для моделей, читаемых из ORM."""

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════ АУТЕНТИФИКАЦИЯ ═══════════════════════════
class LoginRequest(BaseModel):
    """Запрос на вход в систему."""

    email: EmailStr = Field(description="Корпоративный адрес электронной почты")
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    """Запрос на обновление пары токенов."""

    refresh_token: str = Field(min_length=10)


class TokenResponse(BaseModel):
    """Пара токенов доступа."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Время жизни access-токена в секундах")


class UserResponse(ORMModel):
    """Сведения о пользователе."""

    id: uuid.UUID
    email: str
    full_name: str
    department: str
    position: str
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class CurrentUserResponse(UserResponse):
    """Сведения о текущем пользователе с его правами."""

    permissions: list[str] = Field(default_factory=list)
    allowed_access_levels: list[str] = Field(default_factory=list)
    role_label: str = ""


class UserCreateRequest(BaseModel):
    """Создание учётной записи."""

    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(max_length=255)
    department: str = Field(default="", max_length=255)
    position: str = Field(default="", max_length=255)
    role: Role = Role.EMPLOYEE


class UserUpdateRequest(BaseModel):
    """Изменение учётной записи."""

    full_name: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, max_length=255)
    role: Role | None = None
    is_active: bool | None = None


class PasswordChangeRequest(BaseModel):
    """Смена собственного пароля."""

    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=10, max_length=128)


# ═══════════════════════════ ДИАЛОГ ═══════════════════════════
class CitationResponse(BaseModel):
    """Ссылка на положение инструкции."""

    chunk_id: str = ""
    document_code: str = ""
    document_title: str = ""
    version_label: str = ""
    clause_number: str = Field(default="", description="Основной адрес фрагмента")
    clause_numbers: list[str] = Field(
        default_factory=list,
        description="Все пункты, текст которых попал во фрагмент: короткие "
        "пункты инструкции объединяются при нарезке",
    )
    section_title: str = ""
    breadcrumbs: str = ""
    page_number: int | None = None
    annex_number: str = ""
    citation_label: str = Field(default="", description="Готовая подпись источника")
    quote: str = Field(default="", description="Фрагмент текста инструкции")
    relevance: float = 0.0
    relevance_percent: int = Field(default=0, description="Релевантность запросу, %")
    rank: int = 0


class ClarifyingQuestionResponse(BaseModel):
    """Уточняющий вопрос к сотруднику."""

    question: str
    field: str = ""
    options: list[str] = Field(default_factory=list)


class TemplateCandidateResponse(BaseModel):
    """Шаблон документа, подходящий обращению."""

    code: str
    name: str
    description: str = ""
    doc_kind: str = ""
    related_clauses: list[str] = Field(default_factory=list)
    match_percent: int = 0
    fields: list[dict[str, Any]] = Field(default_factory=list)
    required_approvals: list[dict[str, Any]] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """Обращение сотрудника к помощнику."""

    message: str = Field(min_length=1, max_length=4000)
    session_id: uuid.UUID | None = Field(
        default=None, description="Диалог; при отсутствии создаётся новый"
    )
    template_code: str = Field(
        default="", max_length=64, description="Выбранный шаблон при заполнении документа"
    )

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        """Убирает крайние пробелы и проверяет, что текст не пуст."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Сообщение не может быть пустым")
        return cleaned


class ChatResponse(BaseModel):
    """Ответ помощника."""

    message_id: uuid.UUID
    session_id: uuid.UUID
    answer: str
    answer_status: str = Field(
        description=(
            "answered | no_context | clarification | template_ready | "
            "template_filling | info | error"
        )
    )
    citations: list[CitationResponse] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestionResponse] = Field(default_factory=list)
    template_candidates: list[TemplateCandidateResponse] = Field(default_factory=list)
    intent: str = ""
    top_relevance_percent: int = 0
    critic_verdict: str = ""
    model_name: str = ""
    latency_ms: int = 0
    generated_document_id: uuid.UUID | None = None
    filled_fields: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


class MessageResponse(ORMModel):
    """Сообщение из истории диалога."""

    id: uuid.UUID
    role: str
    content: str
    answer_status: str = ""
    intent: str = ""
    critic_verdict: str = ""
    created_at: datetime
    citations: list[CitationResponse] = Field(default_factory=list)


class SessionResponse(ORMModel):
    """Диалог сотрудника с помощником."""

    id: uuid.UUID
    title: str
    messages_count: int
    is_archived: bool
    created_at: datetime
    last_message_at: datetime | None = None


class SessionDetailResponse(SessionResponse):
    """Диалог вместе с историей сообщений."""

    messages: list[MessageResponse] = Field(default_factory=list)


class SessionCreateRequest(BaseModel):
    """Создание нового диалога."""

    title: str = Field(default="Новый диалог", max_length=255)


class FeedbackRequest(BaseModel):
    """Обратная связь по ответу помощника."""

    is_helpful: bool
    reason: str = Field(default="", max_length=64)
    comment: str = Field(default="", max_length=2000)


# ═══════════════════════════ ДОКУМЕНТЫ ═══════════════════════════
class TemplateResponse(ORMModel):
    """Шаблон модульного документа."""

    code: str
    name: str
    description: str = ""
    doc_kind: str = ""
    fields_schema: list[dict[str, Any]] = Field(default_factory=list)
    required_approvals: list[dict[str, Any]] = Field(default_factory=list)
    related_clauses: list[str] = Field(default_factory=list)
    version: int = 1


class DocumentGenerateRequest(BaseModel):
    """Запрос на формирование черновика документа."""

    template_code: str = Field(min_length=1, max_length=64)
    values: dict[str, Any] = Field(default_factory=dict)
    session_id: uuid.UUID | None = None


class GeneratedDocumentResponse(ORMModel):
    """Сформированный черновик документа."""

    id: uuid.UUID
    filename: str = ""
    file_size: int = 0
    status: str
    missing_fields: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime | None = None
    template_name: str = ""
    download_url: str = ""


class ViolationResponse(BaseModel):
    """Несоответствие требованиям инструкции."""

    severity: Literal["critical", "major", "minor"]
    field: str = ""
    problem: str
    requirement: str = ""
    clause: str = ""
    recommendation: str = ""


class RecommendationResponse(BaseModel):
    """Рекомендация по доработке документа."""

    priority: int
    severity: str
    action: str
    clause: str = ""


class ValidationReportResponse(ORMModel):
    """Отчёт проверки проекта документа."""

    id: uuid.UUID
    source_filename: str = ""
    detected_doc_kind: str = ""
    verdict: str
    score: float = 0.0
    violations: list[ViolationResponse] = Field(default_factory=list)
    recommendations: list[RecommendationResponse] = Field(default_factory=list)
    checked_rules: int = 0
    created_at: datetime
    summary: str = ""
    citations: list[CitationResponse] = Field(default_factory=list)


# ═══════════════════════════ БАЗА ЗНАНИЙ ═══════════════════════════
class DocumentVersionResponse(ORMModel):
    """Редакция документа базы знаний."""

    id: uuid.UUID
    version_number: int
    version_label: str = ""
    source_filename: str = ""
    status: str
    is_active: bool
    chunks_count: int = 0
    pages_count: int = 0
    changelog: str = ""
    created_at: datetime
    indexed_at: datetime | None = None
    effective_from: datetime | None = None
    indexing_error: str = ""


class KnowledgeDocumentResponse(ORMModel):
    """Документ базы знаний."""

    id: uuid.UUID
    code: str
    title: str
    doc_type: str
    description: str = ""
    access_level: AccessLevel
    owner_department: str = ""
    is_active: bool
    versions: list[DocumentVersionResponse] = Field(default_factory=list)


class DocumentRegisterRequest(BaseModel):
    """Регистрация нового документа базы знаний."""

    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Z0-9\-]+$")
    title: str = Field(min_length=3, max_length=512)
    doc_type: Literal[
        "instruction", "regulation", "matrix", "template_guide", "faq", "other"
    ] = "instruction"
    description: str = Field(default="", max_length=4000)
    access_level: AccessLevel = AccessLevel.INTERNAL
    owner_department: str = Field(default="", max_length=255)


class TaskAcceptedResponse(BaseModel):
    """Подтверждение постановки задачи в очередь."""

    task_id: str
    status: str = "accepted"
    message: str = ""
    resource_id: uuid.UUID | None = None


# ═══════════════════════════ КАЧЕСТВО И СОСТОЯНИЕ ═══════════════════════════
class EvalRunResponse(ORMModel):
    """Прогон оценки качества."""

    id: uuid.UUID
    name: str = ""
    model_name: str = ""
    eval_set: str = ""
    questions_total: int = 0
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    citation_accuracy: float | None = None
    no_answer_rate: float | None = None
    duration_seconds: int = 0
    status: str
    created_at: datetime


class EvalRunRequest(BaseModel):
    """Запуск оценки качества."""

    name: str = Field(default="", max_length=255)
    limit: int | None = Field(default=None, ge=1, le=500)
    skip_ragas: bool = Field(
        default=False, description="Считать только собственные метрики (быстрый прогон)"
    )


class ComponentHealth(BaseModel):
    """Состояние отдельного компонента."""

    name: str
    healthy: bool
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Состояние сервиса."""

    status: Literal["ok", "degraded", "unavailable"]
    version: str
    environment: str
    components: list[ComponentHealth] = Field(default_factory=list)


class KnowledgeBaseStatsResponse(BaseModel):
    """Показатели базы знаний."""

    collection: str
    collection_status: str
    points_count: int
    documents_count: int
    active_versions: int
    failed_versions: int
    templates_count: int


class AuditLogResponse(ORMModel):
    """Запись журнала действий."""

    id: uuid.UUID
    action: str
    resource_type: str = ""
    resource_id: str = ""
    outcome: str = ""
    ip_address: str = ""
    created_at: datetime
    user_email: str = ""
