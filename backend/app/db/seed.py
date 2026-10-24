"""Наполнение базы стартовыми данными.

Запуск: ``python -m app.db.seed`` (или ``make seed``).

Создаёт администратора из ``.env``, демонстрационных пользователей для каждой
роли — их показывают жюри при демонстрации разграничения доступа — и регистрирует
документы базы знаний со шаблонами модульных документов.
Идемпотентно: повторный запуск не плодит дубликаты.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.rbac import AccessLevel, Role
from app.core.security import hash_password
from app.db.models import DocumentTemplate, KnowledgeDocument, User
from app.db.module_templates import MODULE_TEMPLATES
from app.db.postgres import close_postgres, lifespan_postgres, session_scope

logger = get_logger(__name__)

DEMO_PASSWORD = "Demo#2026pass"

DEMO_USERS: list[dict[str, Any]] = [
    {
        "email": "employee@cbr.local",
        "full_name": "Иванов Иван Иванович",
        "department": "Департамент банковского регулирования",
        "position": "Главный специалист",
        "role": Role.EMPLOYEE,
    },
    {
        "email": "methodologist@cbr.local",
        "full_name": "Петрова Анна Сергеевна",
        "department": "Центр документационного обеспечения",
        "position": "Методолог",
        "role": Role.METHODOLOGIST,
    },
    {
        "email": "auditor@cbr.local",
        "full_name": "Смирнов Олег Петрович",
        "department": "Служба внутреннего аудита",
        "position": "Аудитор",
        "role": Role.AUDITOR,
    },
]

# Документы базы знаний соответствуют материалам кейса: три файла,
# которые лежат в data/instructions. Код документа используется при
# загрузке редакции (make index и экран администратора), поэтому
# менять его после первой индексации нельзя.
#
# Гриф у Матрицы — «конфиденциально» осознанно: на ней показывается
# разграничение доступа. Сотрудник спрашивает про состав визирующих
# подразделений и получает честный отказ, методолог — ответ с цитатой.
# В самих файлах пометок доступа нет, гриф задаётся здесь, на уровне
# реестра документов.
KNOWLEDGE_DOCUMENTS: list[dict[str, Any]] = [
    {
        "code": "INSTR-DOC",
        "title": "Инструкция по документационному обеспечению управления и документообороту",
        "doc_type": "instruction",
        "description": (
            "Основной документ базы знаний: 12 глав о подготовке, оформлении, "
            "согласовании, регистрации, исполнении и архивном хранении документов."
        ),
        "access_level": AccessLevel.INTERNAL,
        "owner_department": "Департамент ДОУ",
    },
    {
        "code": "MATRIX-APPROVAL",
        "title": "Матрица обязательного визирования внутренних и внешних документов",
        "doc_type": "matrix",
        "description": (
            "Определяет, какие подразделения визируют проект документа "
            "в зависимости от его вида. Применяется дополнительно к общему "
            "маршруту визирования из инструкции."
        ),
        "access_level": AccessLevel.CONFIDENTIAL,
        "owner_department": "Департамент ДОУ",
    },
    {
        "code": "GUIDE-MODULES",
        "title": "Методические рекомендации по работе с модульными документами",
        "doc_type": "template_guide",
        "description": (
            "Порядок подготовки проекта по модулю: регистрационная карточка, "
            "заполнение полей, визирование и лингвистическая экспертиза."
        ),
        "access_level": AccessLevel.INTERNAL,
        "owner_department": "Департамент ДОУ",
    },
]

# Шаблоны модульных документов описаны в app/db/module_templates.py:
# там же, где карта правок исходных файлов организаторов. Сид лишь
# регистрирует их в реестре — расходиться описаниям негде.
TEMPLATES: list[dict[str, Any]] = [item.to_db_payload() for item in MODULE_TEMPLATES]


async def seed_users() -> None:
    """Создаёт администратора и демонстрационных пользователей."""
    async with session_scope() as session:
        admin_email = settings.BOOTSTRAP_ADMIN_EMAIL
        existing = await session.scalar(select(User).where(User.email == admin_email))
        if existing is None:
            session.add(
                User(
                    email=admin_email,
                    hashed_password=hash_password(
                        settings.BOOTSTRAP_ADMIN_PASSWORD.get_secret_value()
                    ),
                    full_name="Администратор системы",
                    department="Центр документационного обеспечения",
                    position="Администратор",
                    role=Role.ADMIN,
                    is_active=True,
                )
            )
            logger.info("seed_admin_created", role=Role.ADMIN.value)
        else:
            logger.info("seed_admin_exists")

        for payload in DEMO_USERS:
            found = await session.scalar(select(User).where(User.email == payload["email"]))
            if found is not None:
                continue
            session.add(
                User(
                    email=payload["email"],
                    hashed_password=hash_password(DEMO_PASSWORD),
                    full_name=payload["full_name"],
                    department=payload["department"],
                    position=payload["position"],
                    role=payload["role"],
                    is_active=True,
                )
            )
            logger.info("seed_user_created", role=payload["role"].value)


async def seed_knowledge_documents() -> None:
    """Регистрирует документы базы знаний (без содержимого)."""
    async with session_scope() as session:
        for payload in KNOWLEDGE_DOCUMENTS:
            found = await session.scalar(
                select(KnowledgeDocument).where(KnowledgeDocument.code == payload["code"])
            )
            if found is not None:
                continue
            session.add(KnowledgeDocument(**payload))
            logger.info("seed_document_registered", code=payload["code"])


async def seed_templates() -> None:
    """Регистрирует шаблоны модульных документов."""
    async with session_scope() as session:
        for payload in TEMPLATES:
            found = await session.scalar(
                select(DocumentTemplate).where(DocumentTemplate.code == payload["code"])
            )
            if found is not None:
                continue
            session.add(DocumentTemplate(**payload))
            logger.info("seed_template_registered", code=payload["code"])


async def run_seed() -> None:
    """Выполняет полное наполнение базы стартовыми данными."""
    await lifespan_postgres()
    try:
        await seed_users()
        await seed_knowledge_documents()
        await seed_templates()
        logger.info("seed_completed")
    finally:
        await close_postgres()


def main() -> None:
    """Точка входа CLI."""
    configure_logging()
    settings.ensure_dirs()
    asyncio.run(run_seed())
    print("✓ Стартовые данные загружены.")
    print(f"  Администратор: {settings.BOOTSTRAP_ADMIN_EMAIL}")
    print(f"  Демо-пользователи (пароль {DEMO_PASSWORD}):")
    for user in DEMO_USERS:
        print(f"    - {user['email']} — {user['role'].value}")


if __name__ == "__main__":
    main()
