"""Модели PostgreSQL.

Структура покрывает четыре контура решения:

* **Пользователи и аудит** — ролевая модель, журнал обращений (требование
  «разграничение доступа» и «защита информации»).
* **База знаний с версионностью** — документ, его версии, фрагменты.
  Активная версия ровно одна; старые сохраняются и доступны для сравнения
  (требование слайда 4 «версионность и актуализация документов»).
* **Диалоги** — сессии, сообщения и цитаты на пункты инструкции.
* **Документы** — реестр шаблонов, сформированные черновики и отчёты проверки.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.rbac import AccessLevel, Role
from app.db.base import Base, TimestampMixin, UUIDMixin

_ROLE_ENUM = SAEnum(Role, name="user_role", values_callable=lambda e: [i.value for i in e])
_ACCESS_ENUM = SAEnum(
    AccessLevel, name="access_level", values_callable=lambda e: [i.value for i in e]
)


# ═══════════════════════════ ПОЛЬЗОВАТЕЛИ ═══════════════════════════
class User(Base, UUIDMixin, TimestampMixin):
    """Учётная запись сотрудника."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    department: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    position: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[Role] = mapped_column(_ROLE_ENUM, nullable=False, default=Role.EMPLOYEE)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sessions: Mapped[list[ChatSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )
    audit_entries: Mapped[list[AuditLog]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (Index("ix_users_role_active", "role", "is_active"),)


class AuditLog(Base, UUIDMixin, TimestampMixin):
    """Журнал значимых действий.

    Хранится то, *что* сделано, но не содержимое запроса: текст вопроса и ответа
    могут включать персональные данные и реквизиты документов.
    """

    __tablename__ = "audit_log"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    user: Mapped[User | None] = relationship(back_populates="audit_entries", lazy="noload")

    __table_args__ = (Index("ix_audit_action_created", "action", "created_at"),)


# ═══════════════════════════ БАЗА ЗНАНИЙ ═══════════════════════════
class KnowledgeDocument(Base, UUIDMixin, TimestampMixin):
    """Нормативный документ базы знаний.

    Запись описывает документ как сущность («Инструкция по документообороту»),
    а его содержимое живёт в версиях :class:`DocumentVersion`.
    """

    __tablename__ = "knowledge_documents"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False, default="instruction")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    access_level: Mapped[AccessLevel] = mapped_column(
        _ACCESS_ENUM, nullable=False, default=AccessLevel.INTERNAL
    )
    owner_department: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentVersion.version_number.desc()",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('instruction','regulation','matrix','template_guide','faq','other')",
            name="doc_type_allowed",
        ),
    )


class DocumentVersion(Base, UUIDMixin, TimestampMixin):
    """Версия документа базы знаний.

    Активная версия в каждый момент одна — это гарантирует частичный
    уникальный индекс ``uq_active_version_per_document``. Поиск по умолчанию
    ведётся только по активным версиям, но старые остаются в Qdrant,
    чтобы можно было показать, как требование выглядело раньше.
    """

    __tablename__ = "document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    indexing_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    chunks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    changelog: Mapped[str] = mapped_column(Text, nullable=False, default="")

    document: Mapped[KnowledgeDocument] = relationship(back_populates="versions", lazy="joined")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="version", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        Index(
            "uq_active_version_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "status IN ('pending','indexing','indexed','failed','archived')",
            name="version_status_allowed",
        ),
    )


class DocumentChunk(Base, UUIDMixin, TimestampMixin):
    """Фрагмент документа — зеркало точки в Qdrant.

    Текст дублируется в PostgreSQL намеренно: цитаты в ответе формируются
    отсюда, что позволяет показать точную формулировку пункта, даже если
    векторное хранилище недоступно, и переиндексировать базу без потери данных.
    """

    __tablename__ = "document_chunks"

    version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    qdrant_point_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    clause_number: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    section_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    breadcrumbs: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page_number: Mapped[int | None] = mapped_column(Integer)
    annex_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_level: Mapped[AccessLevel] = mapped_column(
        _ACCESS_ENUM, nullable=False, default=AccessLevel.INTERNAL, index=True
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    version: Mapped[DocumentVersion] = relationship(back_populates="chunks", lazy="noload")

    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index", name="uq_chunk_position"),
        Index("ix_chunks_clause_lookup", "version_id", "clause_number"),
    )


# ═══════════════════════════ ДИАЛОГИ ═══════════════════════════
class ChatSession(Base, UUIDMixin, TimestampMixin):
    """Сессия диалога с помощником."""

    __tablename__ = "chat_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Новый диалог")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    messages_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    user: Mapped[User] = relationship(back_populates="sessions", lazy="noload")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
        lazy="noload",
    )

    __table_args__ = (Index("ix_sessions_user_recent", "user_id", "last_message_at"),)


class ChatMessage(Base, UUIDMixin, TimestampMixin):
    """Сообщение диалога.

    Поле :attr:`answer_status` фиксирует исход обработки и используется
    в метриках: ``answered`` / ``no_context`` (честный отказ) /
    ``clarification`` (уточняющий вопрос) / ``error``.
    """

    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    intent: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    answer_status: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    top_score: Mapped[float | None] = mapped_column(Float)
    critic_verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    session: Mapped[ChatSession] = relationship(back_populates="messages", lazy="noload")
    citations: Mapped[list[MessageCitation]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageCitation.rank",
        lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="message_role_allowed"),
        Index("ix_messages_session_created", "session_id", "created_at"),
    )


class MessageCitation(Base, UUIDMixin, TimestampMixin):
    """Ссылка ответа на конкретное положение инструкции.

    Критерий №1 оценки требует, чтобы каждый ответ опирался на конкретные
    пункты. Цитаты хранятся отдельными записями, поэтому их можно
    пересчитать, показать в интерфейсе и проверить в тестах.
    """

    __tablename__ = "message_citations"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    document_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    version_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    clause_number: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    section_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    page_number: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relevance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    message: Mapped[ChatMessage] = relationship(back_populates="citations", lazy="noload")


# ═══════════════════════════ ДОКУМЕНТЫ И ШАБЛОНЫ ═══════════════════════════
class DocumentTemplate(Base, UUIDMixin, TimestampMixin):
    """Шаблон модульного документа.

    :attr:`fields_schema` описывает поля для заполнения — по нему
    агент-заполнитель понимает, что именно спросить у сотрудника.
    :attr:`required_approvals` хранит выдержку из матрицы согласования.
    """

    __tablename__ = "document_templates"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    doc_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="other", index=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    keywords: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    fields_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    required_approvals: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    related_clauses: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    access_level: Mapped[AccessLevel] = mapped_column(
        _ACCESS_ENUM, nullable=False, default=AccessLevel.INTERNAL
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    drafts: Mapped[list[GeneratedDocument]] = relationship(
        back_populates="template", lazy="noload"
    )


class GeneratedDocument(Base, UUIDMixin, TimestampMixin):
    """Сформированный по шаблону черновик документа."""

    __tablename__ = "generated_documents"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_templates.id", ondelete="SET NULL")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    filled_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    template: Mapped[DocumentTemplate | None] = relationship(
        back_populates="drafts", lazy="joined"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending','ready','failed','expired')",
            name="generated_status_allowed",
        ),
    )


class ValidationReport(Base, UUIDMixin, TimestampMixin):
    """Отчёт проверки загруженного черновика на соответствие инструкции.

    Реализует дополнительную функцию со слайда 5: загрузка проекта документа,
    выявление несоответствий и рекомендации по доработке.
    """

    __tablename__ = "validation_reports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_templates.id", ondelete="SET NULL")
    )
    source_filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    detected_doc_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    violations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    recommendations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    checked_rules: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('pending','compliant','minor_issues','major_issues','failed')",
            name="validation_verdict_allowed",
        ),
    )


# ═══════════════════════════ КАЧЕСТВО ═══════════════════════════
class EvalRun(Base, UUIDMixin, TimestampMixin):
    """Прогон offline-оценки качества (RAGAS).

    Хранит метрики по эталонному набору вопросов, чтобы на защите показать
    сравнение базовой и дообученной модели в динамике.
    """

    __tablename__ = "eval_runs"

    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    eval_set: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    questions_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    faithfulness: Mapped[float | None] = mapped_column(Float)
    answer_relevancy: Mapped[float | None] = mapped_column(Float)
    context_precision: Mapped[float | None] = mapped_column(Float)
    context_recall: Mapped[float | None] = mapped_column(Float)
    citation_accuracy: Mapped[float | None] = mapped_column(Float)
    no_answer_rate: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class MessageFeedback(Base, UUIDMixin, TimestampMixin):
    """Обратная связь сотрудника по ответу помощника."""

    __tablename__ = "message_feedback"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    is_helpful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_feedback_per_user"),)


__all__ = [
    "User",
    "AuditLog",
    "KnowledgeDocument",
    "DocumentVersion",
    "DocumentChunk",
    "ChatSession",
    "ChatMessage",
    "MessageCitation",
    "DocumentTemplate",
    "GeneratedDocument",
    "ValidationReport",
    "EvalRun",
    "MessageFeedback",
]
