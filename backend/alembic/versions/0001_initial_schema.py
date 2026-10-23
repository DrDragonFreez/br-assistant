"""Начальная схема базы данных.

Схема собрана по моделям ``app.db.models`` и покрывает четыре контура
решения: пользователи и аудит, база знаний с версионностью, диалоги
с цитатами, документы и отчёты проверки.

Отдельно создаются расширения PostgreSQL: ``pgcrypto`` нужен для
``server_default=gen_random_uuid()`` (в PostgreSQL 13+ функция есть и в ядре,
расширение оставлено для совместимости со старыми сборками), ``pg_trgm``
ускоряет поиск шаблонов по названию.

Имена ограничений и индексов соответствуют соглашению из
``app.db.base.NAMING_CONVENTION``: иначе откат миграции не найдёт объекты,
а повторный autogenerate предложит пересоздать их заново.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

ROLE_ENUM = postgresql.ENUM(
    "employee",
    "methodologist",
    "auditor",
    "admin",
    name="user_role",
    create_type=False,
)
ACCESS_ENUM = postgresql.ENUM(
    "public",
    "internal",
    "confidential",
    name="access_level",
    create_type=False,
)

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)

_PK_DEFAULT = sa.text("gen_random_uuid()")
_NOW = sa.text("now()")


def _id_column() -> sa.Column:
    """Создаёт колонку первичного ключа (UUID v4).

    Returns:
        Колонка ``id`` с серверным значением по умолчанию.
    """
    return sa.Column("id", UUID, nullable=False, server_default=_PK_DEFAULT)


def _timestamp_columns() -> list[sa.Column]:
    """Создаёт колонки отметок времени из ``TimestampMixin``.

    Returns:
        Список колонок ``created_at`` и ``updated_at``.
    """
    return [
        sa.Column("created_at", TS, nullable=False, server_default=_NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=_NOW),
    ]


def upgrade() -> None:
    """Создаёт схему с нуля."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    bind = op.get_bind()
    ROLE_ENUM.create(bind, checkfirst=True)
    ACCESS_ENUM.create(bind, checkfirst=True)

    # ─────────────────────── ПОЛЬЗОВАТЕЛИ И АУДИТ ───────────────────────
    op.create_table(
        "users",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("department", sa.String(length=255), nullable=False),
        sa.Column("position", sa.String(length=255), nullable=False),
        sa.Column("role", ROLE_ENUM, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_role_active", "users", ["role", "is_active"])

    op.create_table(
        "audit_log",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_audit_log_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"])
    op.create_index("ix_audit_action_created", "audit_log", ["action", "created_at"])

    # ─────────────────────── БАЗА ЗНАНИЙ ───────────────────────
    op.create_table(
        "knowledge_documents",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("doc_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("access_level", ACCESS_ENUM, nullable=False),
        sa.Column("owner_department", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("extra", JSONB, nullable=False),
        sa.CheckConstraint(
            "doc_type IN ('instruction','regulation','matrix','template_guide','faq','other')",
            name="ck_knowledge_documents_doc_type_allowed",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_documents"),
        sa.UniqueConstraint("code", name="uq_knowledge_documents_code"),
    )
    op.create_index("ix_knowledge_documents_created_at", "knowledge_documents", ["created_at"])
    op.create_index("ix_knowledge_documents_code", "knowledge_documents", ["code"])

    op.create_table(
        "document_versions",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("version_label", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=512), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_from", TS, nullable=True),
        sa.Column("effective_to", TS, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("indexing_error", sa.Text(), nullable=False),
        sa.Column("chunks_count", sa.Integer(), nullable=False),
        sa.Column("pages_count", sa.Integer(), nullable=False),
        sa.Column("indexed_at", TS, nullable=True),
        sa.Column("uploaded_by", UUID, nullable=True),
        sa.Column("changelog", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','indexing','indexed','failed','archived')",
            name="ck_document_versions_version_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name="fk_document_versions_document_id_knowledge_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name="fk_document_versions_uploaded_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
    )
    op.create_index("ix_document_versions_created_at", "document_versions", ["created_at"])
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_content_hash", "document_versions", ["content_hash"])
    op.create_index("ix_document_versions_status", "document_versions", ["status"])
    op.create_index(
        "uq_active_version_per_document",
        "document_versions",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "document_chunks",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column("qdrant_point_id", UUID, nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("clause_number", sa.String(length=64), nullable=False),
        sa.Column("section_title", sa.String(length=512), nullable=False),
        sa.Column("breadcrumbs", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("annex_number", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_tokens", sa.Integer(), nullable=False),
        sa.Column("access_level", ACCESS_ENUM, nullable=False),
        sa.Column("extra", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name="fk_document_chunks_version_id_document_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint("qdrant_point_id", name="uq_document_chunks_qdrant_point_id"),
        sa.UniqueConstraint("version_id", "chunk_index", name="uq_chunk_position"),
    )
    op.create_index("ix_document_chunks_created_at", "document_chunks", ["created_at"])
    op.create_index("ix_document_chunks_version_id", "document_chunks", ["version_id"])
    op.create_index("ix_document_chunks_clause_number", "document_chunks", ["clause_number"])
    op.create_index("ix_document_chunks_access_level", "document_chunks", ["access_level"])
    op.create_index("ix_chunks_clause_lookup", "document_chunks", ["version_id", "clause_number"])

    # ─────────────────────── ДИАЛОГИ ───────────────────────
    op.create_table(
        "chat_sessions",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("messages_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", TS, nullable=True),
        sa.Column("context", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_chat_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
    )
    op.create_index("ix_chat_sessions_created_at", "chat_sessions", ["created_at"])
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"])
    op.create_index("ix_chat_sessions_last_message_at", "chat_sessions", ["last_message_at"])
    op.create_index("ix_sessions_user_recent", "chat_sessions", ["user_id", "last_message_at"])

    op.create_table(
        "chat_messages",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("session_id", UUID, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=False),
        sa.Column("answer_status", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("top_score", sa.Float(), nullable=True),
        sa.Column("critic_verdict", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("extra", JSONB, nullable=False),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')",
            name="ck_chat_messages_message_role_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_chat_messages_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
    )
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_messages_intent", "chat_messages", ["intent"])
    op.create_index("ix_chat_messages_answer_status", "chat_messages", ["answer_status"])
    op.create_index("ix_messages_session_created", "chat_messages", ["session_id", "created_at"])

    op.create_table(
        "message_citations",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("message_id", UUID, nullable=False),
        sa.Column("chunk_id", UUID, nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("document_code", sa.String(length=64), nullable=False),
        sa.Column("document_title", sa.String(length=512), nullable=False),
        sa.Column("version_label", sa.String(length=64), nullable=False),
        sa.Column("clause_number", sa.String(length=64), nullable=False),
        sa.Column("section_title", sa.String(length=512), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name="fk_message_citations_chunk_id_document_chunks",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name="fk_message_citations_message_id_chat_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_citations"),
    )
    op.create_index("ix_message_citations_created_at", "message_citations", ["created_at"])
    op.create_index("ix_message_citations_message_id", "message_citations", ["message_id"])

    # ─────────────────────── ДОКУМЕНТЫ И ШАБЛОНЫ ───────────────────────
    op.create_table(
        "document_templates",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("doc_kind", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("keywords", JSONB, nullable=False),
        sa.Column("fields_schema", JSONB, nullable=False),
        sa.Column("required_approvals", JSONB, nullable=False),
        sa.Column("related_clauses", JSONB, nullable=False),
        sa.Column("access_level", ACCESS_ENUM, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_templates"),
        sa.UniqueConstraint("code", name="uq_document_templates_code"),
    )
    op.create_index("ix_document_templates_created_at", "document_templates", ["created_at"])
    op.create_index("ix_document_templates_code", "document_templates", ["code"])
    op.create_index("ix_document_templates_doc_kind", "document_templates", ["doc_kind"])

    op.create_table(
        "generated_documents",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("template_id", UUID, nullable=True),
        sa.Column("session_id", UUID, nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("filled_fields", JSONB, nullable=False),
        sa.Column("missing_fields", JSONB, nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','pending','ready','failed','expired')",
            name="ck_generated_documents_generated_status_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_generated_documents_session_id_chat_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["document_templates.id"],
            name="fk_generated_documents_template_id_document_templates",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_generated_documents_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generated_documents"),
    )
    op.create_index("ix_generated_documents_created_at", "generated_documents", ["created_at"])
    op.create_index("ix_generated_documents_user_id", "generated_documents", ["user_id"])
    op.create_index("ix_generated_documents_status", "generated_documents", ["status"])

    op.create_table(
        "validation_reports",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("template_id", UUID, nullable=True),
        sa.Column("source_filename", sa.String(length=512), nullable=False),
        sa.Column("detected_doc_kind", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("violations", JSONB, nullable=False),
        sa.Column("recommendations", JSONB, nullable=False),
        sa.Column("checked_rules", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('pending','compliant','minor_issues','major_issues','failed')",
            name="ck_validation_reports_validation_verdict_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["document_templates.id"],
            name="fk_validation_reports_template_id_document_templates",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_validation_reports_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_validation_reports"),
    )
    op.create_index("ix_validation_reports_created_at", "validation_reports", ["created_at"])
    op.create_index("ix_validation_reports_user_id", "validation_reports", ["user_id"])
    op.create_index("ix_validation_reports_verdict", "validation_reports", ["verdict"])

    # ─────────────────────── КАЧЕСТВО ───────────────────────
    op.create_table(
        "eval_runs",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("eval_set", sa.String(length=255), nullable=False),
        sa.Column("questions_total", sa.Integer(), nullable=False),
        sa.Column("faithfulness", sa.Float(), nullable=True),
        sa.Column("answer_relevancy", sa.Float(), nullable=True),
        sa.Column("context_precision", sa.Float(), nullable=True),
        sa.Column("context_recall", sa.Float(), nullable=True),
        sa.Column("citation_accuracy", sa.Float(), nullable=True),
        sa.Column("no_answer_rate", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("details", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
    )
    op.create_index("ix_eval_runs_created_at", "eval_runs", ["created_at"])
    op.create_index("ix_eval_runs_status", "eval_runs", ["status"])

    op.create_table(
        "message_feedback",
        _id_column(),
        *_timestamp_columns(),
        sa.Column("message_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("is_helpful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name="fk_message_feedback_message_id_chat_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_message_feedback_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_feedback"),
        sa.UniqueConstraint("message_id", "user_id", name="uq_feedback_per_user"),
    )
    op.create_index("ix_message_feedback_created_at", "message_feedback", ["created_at"])
    op.create_index("ix_message_feedback_message_id", "message_feedback", ["message_id"])


def downgrade() -> None:
    """Удаляет схему целиком.

    Порядок обратный созданию: сначала зависимые таблицы, затем типы.
    Расширения PostgreSQL не удаляются — они могут использоваться другими
    схемами той же базы.
    """
    op.drop_table("message_feedback")
    op.drop_table("eval_runs")
    op.drop_table("validation_reports")
    op.drop_table("generated_documents")
    op.drop_table("document_templates")
    op.drop_table("message_citations")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_table("document_chunks")
    op.drop_table("document_versions")
    op.drop_table("knowledge_documents")
    op.drop_table("audit_log")
    op.drop_table("users")

    bind = op.get_bind()
    ACCESS_ENUM.drop(bind, checkfirst=True)
    ROLE_ENUM.drop(bind, checkfirst=True)
