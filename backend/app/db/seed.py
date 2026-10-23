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

KNOWLEDGE_DOCUMENTS: list[dict[str, Any]] = [
    {
        "code": "INSTR-DOC",
        "title": "Инструкция по документообороту Банка России",
        "doc_type": "instruction",
        "description": (
            "Устанавливает требования к подготовке, оформлению, согласованию, "
            "регистрации и исполнению документов."
        ),
        "access_level": AccessLevel.INTERNAL,
        "owner_department": "Центр документационного обеспечения",
    },
    {
        "code": "MATRIX-APPROVAL",
        "title": "Матрица согласования документов",
        "doc_type": "matrix",
        "description": "Определяет состав согласующих подразделений и сроки согласования.",
        "access_level": AccessLevel.CONFIDENTIAL,
        "owner_department": "Центр документационного обеспечения",
    },
    {
        "code": "GUIDE-MODULES",
        "title": "Рекомендации по работе с модульными документами",
        "doc_type": "template_guide",
        "description": "Порядок применения бланков типовых документов.",
        "access_level": AccessLevel.INTERNAL,
        "owner_department": "Центр документационного обеспечения",
    },
    {
        "code": "FAQ-PORTAL",
        "title": "Часто задаваемые вопросы по документообороту",
        "doc_type": "faq",
        "description": "Выдержки из FAQ внутреннего портала.",
        "access_level": AccessLevel.PUBLIC,
        "owner_department": "Центр документационного обеспечения",
    },
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "TPL-SLUZH-ZAPISKA",
        "name": "Служебная записка",
        "doc_kind": "memo",
        "description": "Бланк служебной записки для обращения в другое подразделение.",
        "file_path": "/app/data/templates/sluzhebnaya_zapiska.docx",
        "keywords": ["служебная записка", "служебка", "записка", "обращение"],
        "fields_schema": [
            {
                "name": "addressee",
                "label": "Кому адресована записка (подразделение, должность)",
                "type": "string",
                "required": True,
                "question": (
                    "Кому адресуется служебная записка — укажите подразделение и должность?"
                ),
            },
            {
                "name": "subject",
                "label": "Тема",
                "type": "string",
                "required": True,
                "question": "Какова тема служебной записки?",
            },
            {
                "name": "body",
                "label": "Содержание обращения",
                "type": "text",
                "required": True,
                "question": "Изложите суть обращения: что именно требуется?",
            },
            {
                "name": "deadline",
                "label": "Желаемый срок исполнения",
                "type": "date",
                "required": False,
                "question": "К какому сроку нужен ответ? Если срок не важен — так и скажите.",
            },
            {
                "name": "author_position",
                "label": "Должность автора",
                "type": "string",
                "required": True,
                "question": "Укажите вашу должность для подписи.",
            },
        ],
        "required_approvals": [
            {"department": "Непосредственный руководитель", "term_days": 1, "order": 1},
            {"department": "Центр документационного обеспечения", "term_days": 2, "order": 2},
        ],
        "related_clauses": ["3.1", "3.4", "Приложение 2"],
        "access_level": AccessLevel.INTERNAL,
    },
    {
        "code": "TPL-PRIKAZ",
        "name": "Приказ",
        "doc_kind": "order",
        "description": "Бланк приказа по основной деятельности.",
        "file_path": "/app/data/templates/prikaz.docx",
        "keywords": ["приказ", "распоряжение", "по основной деятельности"],
        "fields_schema": [
            {
                "name": "title",
                "label": "Заголовок приказа",
                "type": "string",
                "required": True,
                "question": "Сформулируйте заголовок приказа (о чём он)?",
            },
            {
                "name": "preamble",
                "label": "Основание издания",
                "type": "text",
                "required": True,
                "question": "На каком основании издаётся приказ?",
            },
            {
                "name": "clauses",
                "label": "Пункты распорядительной части",
                "type": "list",
                "required": True,
                "question": "Перечислите, что именно приказываете — по пунктам.",
            },
            {
                "name": "responsible",
                "label": "Ответственный за исполнение",
                "type": "string",
                "required": True,
                "question": "Кто назначается ответственным за исполнение?",
            },
            {
                "name": "control",
                "label": "Контроль исполнения возложен на",
                "type": "string",
                "required": False,
                "question": "На кого возложить контроль исполнения?",
            },
        ],
        "required_approvals": [
            {"department": "Юридический департамент", "term_days": 3, "order": 1},
            {"department": "Центр документационного обеспечения", "term_days": 2, "order": 2},
            {"department": "Руководитель структурного подразделения", "term_days": 1, "order": 3},
        ],
        "related_clauses": ["4.2", "4.5", "Приложение 1"],
        "access_level": AccessLevel.INTERNAL,
    },
    {
        "code": "TPL-PISMO",
        "name": "Деловое письмо",
        "doc_kind": "letter",
        "description": "Бланк исходящего делового письма внешнему адресату.",
        "file_path": "/app/data/templates/delovoe_pismo.docx",
        "keywords": ["письмо", "исходящее", "ответ на обращение", "деловое письмо"],
        "fields_schema": [
            {
                "name": "recipient_org",
                "label": "Организация-адресат",
                "type": "string",
                "required": True,
                "question": "В какую организацию направляется письмо?",
            },
            {
                "name": "recipient_person",
                "label": "Должностное лицо",
                "type": "string",
                "required": False,
                "question": "Есть ли конкретный адресат — должность и фамилия?",
            },
            {
                "name": "reference",
                "label": "Ссылка на входящий документ",
                "type": "string",
                "required": False,
                "question": "Письмо является ответом? Если да, укажите реквизиты входящего.",
            },
            {
                "name": "body",
                "label": "Текст письма",
                "type": "text",
                "required": True,
                "question": "Изложите содержание письма.",
            },
            {
                "name": "signer",
                "label": "Подписант",
                "type": "string",
                "required": True,
                "question": "Кто подписывает письмо — должность и фамилия?",
            },
        ],
        "required_approvals": [
            {"department": "Юридический департамент", "term_days": 3, "order": 1},
            {"department": "Руководитель структурного подразделения", "term_days": 2, "order": 2},
        ],
        "related_clauses": ["5.1", "5.7", "Приложение 3"],
        "access_level": AccessLevel.INTERNAL,
    },
    {
        "code": "TPL-PROTOKOL",
        "name": "Протокол совещания",
        "doc_kind": "protocol",
        "description": "Бланк протокола совещания с перечнем поручений.",
        "file_path": "/app/data/templates/protokol.docx",
        "keywords": ["протокол", "совещание", "поручения", "повестка"],
        "fields_schema": [
            {
                "name": "meeting_date",
                "label": "Дата совещания",
                "type": "date",
                "required": True,
                "question": "Когда состоялось совещание?",
            },
            {
                "name": "chairman",
                "label": "Председательствующий",
                "type": "string",
                "required": True,
                "question": "Кто председательствовал?",
            },
            {
                "name": "participants",
                "label": "Участники",
                "type": "list",
                "required": True,
                "question": "Перечислите участников совещания.",
            },
            {
                "name": "agenda",
                "label": "Повестка",
                "type": "list",
                "required": True,
                "question": "Какие вопросы были в повестке?",
            },
            {
                "name": "decisions",
                "label": "Принятые решения и поручения",
                "type": "list",
                "required": True,
                "question": "Какие решения приняты и какие поручения даны?",
            },
        ],
        "required_approvals": [
            {"department": "Председательствующий", "term_days": 2, "order": 1},
        ],
        "related_clauses": ["6.3", "Приложение 4"],
        "access_level": AccessLevel.INTERNAL,
    },
]


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
