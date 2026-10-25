"""Итог проверки и положения, по которым она шла.

Валидатор возвращал и короткий итог, и перечень найденных положений
инструкции, но хранить их было негде: в ``validation_reports`` таких
столбцов не было, фоновая задача их не записывала, и в отчёте оставались
пустыми и строка итога, и блок «Положения, по которым шла проверка».
Для критерия «наличие ссылок на конкретные положения» это потеря:
замечания ссылались на пункты, а основание проверки — нет.

Миграция только добавляет столбцы со значениями по умолчанию, поэтому
применима к заполненной базе: у ранее сохранённых отчётов итог
останется пустым, а перечень положений — пустым списком.

Revision ID: 0002_validation_summary
Revises: 0001_initial_schema
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_validation_summary"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Добавляет итог проверки и перечень проверенных положений."""
    op.add_column(
        "validation_reports",
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "validation_reports",
        sa.Column("citations", JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    """Удаляет добавленные столбцы."""
    op.drop_column("validation_reports", "citations")
    op.drop_column("validation_reports", "summary")
