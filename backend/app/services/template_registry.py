"""Реестр шаблонов модульных документов.

Отвечает за две вещи: доступ к метаданным шаблонов из базы и наличие самих
файлов .docx на диске. Если файл бланка отсутствует (на хакатоне исходные
бланки могут прийти позже), генерируется структурный шаблон по схеме полей —
документ всё равно будет сформирован, пусть и в упрощённом оформлении.

Расширение реестра не требует изменения кода: достаточно добавить запись
в таблицу ``document_templates`` и положить файл бланка в каталог шаблонов.
Это прямо отвечает на требование «подключение новых шаблонов и процессов».
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import TemplateNotFoundError
from app.core.logging import get_logger
from app.core.rbac import allowed_access_levels
from app.db.models import DocumentTemplate

logger = get_logger(__name__)

COMMON_PLACEHOLDERS: tuple[str, ...] = (
    "doc_date",
    "doc_number",
    "author_name",
    "author_position",
    "department",
)


class TemplateRegistry:
    """Доступ к шаблонам документов."""

    def __init__(self, templates_dir: Path | None = None) -> None:
        """Инициализирует реестр.

        Args:
            templates_dir: Каталог с файлами бланков.
        """
        self.templates_dir = Path(templates_dir or settings.TEMPLATES_DIR)

    async def list_templates(
        self, session: AsyncSession, *, user_role: str, only_active: bool = True
    ) -> list[DocumentTemplate]:
        """Возвращает шаблоны, доступные роли пользователя.

        Args:
            session: Сессия БД.
            user_role: Роль пользователя.
            only_active: Исключить отключённые шаблоны.

        Returns:
            Список шаблонов.
        """
        levels = allowed_access_levels(user_role)
        query = select(DocumentTemplate).where(DocumentTemplate.access_level.in_(levels))
        if only_active:
            query = query.where(DocumentTemplate.is_active.is_(True))
        result = await session.scalars(query.order_by(DocumentTemplate.name))
        return list(result)

    async def get_by_code(
        self, session: AsyncSession, code: str, *, user_role: str | None = None
    ) -> DocumentTemplate:
        """Находит шаблон по коду с проверкой прав доступа.

        Args:
            session: Сессия БД.
            code: Код шаблона.
            user_role: Роль пользователя для проверки грифа.

        Returns:
            Найденный шаблон.

        Raises:
            TemplateNotFoundError: Шаблон отсутствует или недоступен роли.
        """
        template = await session.scalar(
            select(DocumentTemplate).where(DocumentTemplate.code == code)
        )
        if template is None or not template.is_active:
            raise TemplateNotFoundError(f"Шаблон «{code}» не найден в реестре")

        if user_role is not None and template.access_level.value not in allowed_access_levels(
            user_role
        ):
            raise TemplateNotFoundError(f"Шаблон «{code}» не найден в реестре")

        return template

    def resolve_file(self, template: DocumentTemplate) -> Path | None:
        """Определяет путь к файлу бланка.

        Args:
            template: Шаблон из реестра.

        Returns:
            Путь к существующему файлу либо None.
        """
        candidates: list[Path] = []
        if template.file_path:
            candidates.append(Path(template.file_path))
            candidates.append(self.templates_dir / Path(template.file_path).name)
        candidates.append(self.templates_dir / f"{template.code}.docx")

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate

        logger.warning("template_file_missing", template_code=template.code)
        return None

    @staticmethod
    def required_fields(template: DocumentTemplate) -> list[str]:
        """Возвращает имена обязательных полей шаблона.

        Args:
            template: Шаблон из реестра.

        Returns:
            Список имён полей.
        """
        return [
            str(field["name"])
            for field in (template.fields_schema or [])
            if field.get("required")
        ]

    @staticmethod
    def missing_fields(template: DocumentTemplate, filled: dict[str, Any]) -> list[str]:
        """Определяет незаполненные обязательные поля.

        Args:
            template: Шаблон из реестра.
            filled: Собранные значения.

        Returns:
            Имена полей, которые ещё нужно заполнить.
        """
        return [
            name
            for name in TemplateRegistry.required_fields(template)
            if not filled.get(name)
        ]

    @staticmethod
    def field_by_name(template: DocumentTemplate, name: str) -> dict[str, Any] | None:
        """Находит описание поля по имени.

        Args:
            template: Шаблон из реестра.
            name: Имя поля.

        Returns:
            Описание поля или None.
        """
        for field in template.fields_schema or []:
            if field.get("name") == name:
                return dict(field)
        return None

    def discover_files(self) -> list[Path]:
        """Находит файлы бланков в каталоге шаблонов.

        Returns:
            Список найденных файлов .docx.
        """
        if not self.templates_dir.exists():
            return []
        return sorted(
            item
            for item in self.templates_dir.glob("*.docx")
            if not item.name.startswith("~$")
        )


_registry: TemplateRegistry | None = None


def get_template_registry() -> TemplateRegistry:
    """Возвращает singleton-реестр шаблонов.

    Returns:
        Общий экземпляр :class:`TemplateRegistry`.
    """
    global _registry
    if _registry is None:
        _registry = TemplateRegistry()
    return _registry
