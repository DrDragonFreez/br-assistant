"""Загрузка исходных документов базы знаний.

Поддерживаются PDF, DOCX, TXT, Markdown и HTML. Для PDF важно сохранить
номера страниц: без них в цитате невозможно указать, где именно в инструкции
находится положение, а это прямое требование критерия №1.

Все операции ввода-вывода выполняются в пуле потоков: библиотеки pypdf и
python-docx синхронные, и их прямой вызов заблокировал бы event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.exceptions import DocumentParseError, UnsupportedFileTypeError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PageContent:
    """Содержимое одной страницы или логического блока документа."""

    page_number: int
    text: str


@dataclass(slots=True)
class LoadedDocument:
    """Результат загрузки документа.

    Attributes:
        filename: Имя исходного файла.
        pages: Постраничное содержимое.
        content_hash: SHA-256 от нормализованного текста; по нему определяется,
            изменился ли документ, и нужна ли новая версия.
        metadata: Служебные сведения (автор, число страниц и т. п.).
    """

    filename: str
    pages: list[PageContent]
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        """Полный текст документа одной строкой."""
        return "\n\n".join(page.text for page in self.pages)

    @property
    def pages_count(self) -> int:
        """Количество страниц."""
        return len(self.pages)


def _normalize_whitespace(text: str) -> str:
    """Приводит пробелы и переносы к единому виду.

    Нормативные PDF часто содержат мягкие переносы и разорванные слова —
    без очистки они попадут в цитату и в эмбеддинг.

    Args:
        text: Исходный текст.

    Returns:
        Очищенный текст.
    """
    text = text.replace("\xa0", " ").replace(" ", "\n")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compute_hash(text: str) -> str:
    """Считает SHA-256 от нормализованного текста.

    Args:
        text: Текст документа.

    Returns:
        Шестнадцатеричный дайджест.
    """
    return hashlib.sha256(_normalize_whitespace(text).encode("utf-8")).hexdigest()


def _is_watermark_char(char: dict[str, Any]) -> bool:
    """Определяет, относится ли символ к водяному знаку.

    Водяной знак рисуется поверх текста светлой заливкой («УЧЕБНЫЙ ПРИМЕР»,
    «КОПИЯ», «ДСП»). Для глаза он фоновый, но pdfplumber читает его символы
    в общем потоке и вставляет посреди слов: «законоИдательство»,
    «стрОуктурные». Такой текст портит и лексический поиск, и эмбеддинги,
    и цитату, которую видит сотрудник, поэтому символы отбрасываются
    по светлоте заливки — основной текст документа чёрный.

    Args:
        char: Описание символа из pdfplumber.

    Returns:
        True, если символ нужно отбросить.
    """
    color = char.get("non_stroking_color")
    if color is None:
        return False

    if isinstance(color, (int, float)):
        components = [float(color)]
    else:
        try:
            components = [float(item) for item in color]
        except (TypeError, ValueError):
            return False

    if not components:
        return False
    return min(components) >= settings.PDF_WATERMARK_MIN_LIGHTNESS


def _keep_pdf_object(obj: dict[str, Any]) -> bool:
    """Фильтр объектов страницы для pdfplumber.

    Args:
        obj: Объект страницы (символ, линия, прямоугольник).

    Returns:
        True, если объект остаётся в выдаче.
    """
    if obj.get("object_type") != "char":
        return True
    return not _is_watermark_char(obj)


def _load_pdf_sync(path: Path) -> tuple[list[PageContent], dict[str, Any]]:
    """Извлекает текст из PDF (синхронно).

    Сначала пробуется pdfplumber — он лучше держит таблицы, которыми богата
    матрица согласования. При неудаче используется pypdf.

    Если включено ``PDF_DROP_WATERMARK``, светлые символы отбрасываются
    до извлечения текста: иначе водяной знак попадает внутрь слов
    (см. :func:`_is_watermark_char`). Запасной парсер pypdf такой фильтрации
    не умеет — он используется только когда pdfplumber не справился вовсе.

    Args:
        path: Путь к файлу.

    Returns:
        Кортеж ``(страницы, метаданные)``.

    Raises:
        DocumentParseError: Если ни один парсер не справился.
    """
    pages: list[PageContent] = []
    meta: dict[str, Any] = {}

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            meta = {k: str(v) for k, v in (pdf.metadata or {}).items()}
            for index, raw_page in enumerate(pdf.pages, start=1):
                page = (
                    raw_page.filter(_keep_pdf_object)
                    if settings.PDF_DROP_WATERMARK
                    else raw_page
                )
                text = page.extract_text() or ""
                for table in page.extract_tables() or []:
                    rows = [
                        " | ".join(cell.strip() if cell else "" for cell in row)
                        for row in table
                        if any(row)
                    ]
                    if rows:
                        text += "\n\n[Таблица]\n" + "\n".join(rows)
                cleaned = _normalize_whitespace(text)
                if cleaned:
                    pages.append(PageContent(page_number=index, text=cleaned))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfplumber_failed", error=str(exc), fallback="pypdf")
        pages = []

    if not pages:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            meta = {k.lstrip("/"): str(v) for k, v in (reader.metadata or {}).items()}
            for index, page in enumerate(reader.pages, start=1):
                cleaned = _normalize_whitespace(page.extract_text() or "")
                if cleaned:
                    pages.append(PageContent(page_number=index, text=cleaned))
        except Exception as exc:  # noqa: BLE001
            raise DocumentParseError(
                "Не удалось извлечь текст из PDF-файла", context={"error": str(exc)}
            ) from exc

    if not pages:
        raise DocumentParseError(
            "PDF не содержит извлекаемого текста — вероятно, это скан. "
            "Требуется распознавание (OCR)."
        )
    return pages, meta


def _load_docx_sync(path: Path) -> tuple[list[PageContent], dict[str, Any]]:
    """Извлекает текст из DOCX (синхронно).

    Понятия страницы в DOCX нет, поэтому документ разбивается на логические
    блоки по заголовкам: номер блока используется как «страница».

    Args:
        path: Путь к файлу.

    Returns:
        Кортеж ``(блоки, метаданные)``.

    Raises:
        DocumentParseError: Если файл повреждён.
    """
    try:
        from docx import Document as DocxDocument

        document = DocxDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(
            "Не удалось открыть файл Word", context={"error": str(exc)}
        ) from exc

    core = document.core_properties
    meta: dict[str, Any] = {
        "title": core.title or "",
        "author": core.author or "",
        "revision": core.revision or 0,
    }

    blocks: list[str] = []
    current: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        is_heading = paragraph.style.name.lower().startswith("heading")
        if is_heading and current:
            blocks.append("\n".join(current))
            current = []
        current.append(text)

    if current:
        blocks.append("\n".join(current))

    for table in document.tables:
        rows = [
            " | ".join(cell.text.strip() for cell in row.cells)
            for row in table.rows
            if any(cell.text.strip() for cell in row.cells)
        ]
        if rows:
            blocks.append("[Таблица]\n" + "\n".join(rows))

    pages = [
        PageContent(page_number=index, text=_normalize_whitespace(block))
        for index, block in enumerate(blocks, start=1)
        if block.strip()
    ]
    if not pages:
        raise DocumentParseError("Документ Word не содержит текста")
    return pages, meta


def _load_text_sync(path: Path) -> tuple[list[PageContent], dict[str, Any]]:
    """Читает plain text или Markdown.

    Args:
        path: Путь к файлу.

    Returns:
        Кортеж ``(одна страница, пустые метаданные)``.

    Raises:
        DocumentParseError: Если файл пуст.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    cleaned = _normalize_whitespace(raw)
    if not cleaned:
        raise DocumentParseError("Файл пуст")
    return [PageContent(page_number=1, text=cleaned)], {}


def _load_html_sync(path: Path) -> tuple[list[PageContent], dict[str, Any]]:
    """Извлекает текст из HTML (например, выгрузки FAQ с портала).

    Args:
        path: Путь к файлу.

    Returns:
        Кортеж ``(одна страница, метаданные с заголовком)``.

    Raises:
        DocumentParseError: Если в файле нет текста.
    """
    from bs4 import BeautifulSoup

    raw = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    cleaned = _normalize_whitespace(soup.get_text(separator="\n"))
    if not cleaned:
        raise DocumentParseError("HTML-файл не содержит текста")
    return [PageContent(page_number=1, text=cleaned)], {"title": title}


_LOADERS = {
    ".pdf": _load_pdf_sync,
    ".docx": _load_docx_sync,
    ".txt": _load_text_sync,
    ".md": _load_text_sync,
    ".html": _load_html_sync,
    ".htm": _load_html_sync,
}


async def load_document(path: str | Path) -> LoadedDocument:
    """Загружает документ и возвращает его постраничное содержимое.

    Args:
        path: Путь к файлу.

    Returns:
        Загруженный документ с хешем содержимого.

    Raises:
        UnsupportedFileTypeError: Расширение файла не поддерживается.
        DocumentParseError: Файл не удалось прочитать.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise DocumentParseError(f"Файл не найден: {file_path.name}")

    suffix = file_path.suffix.lower()
    loader = _LOADERS.get(suffix)
    if loader is None:
        raise UnsupportedFileTypeError(
            f"Формат «{suffix}» не поддерживается. "
            f"Доступные: {', '.join(sorted(_LOADERS))}"
        )

    pages, meta = await asyncio.to_thread(loader, file_path)
    full_text = "\n\n".join(page.text for page in pages)

    document = LoadedDocument(
        filename=file_path.name,
        pages=pages,
        content_hash=compute_hash(full_text),
        metadata={**meta, "pages_count": len(pages), "chars": len(full_text)},
    )
    logger.info(
        "document_loaded",
        filename=file_path.name,
        pages=len(pages),
        chars=len(full_text),
    )
    return document


async def discover_documents(directory: str | Path | None = None) -> list[Path]:
    """Находит файлы базы знаний в каталоге.

    Args:
        directory: Каталог поиска; по умолчанию ``INSTRUCTIONS_DIR``.

    Returns:
        Отсортированный список путей к поддерживаемым файлам.
    """
    root = Path(directory or settings.INSTRUCTIONS_DIR)
    if not root.exists():
        return []
    found = [
        item
        for item in sorted(root.rglob("*"))
        if item.is_file() and item.suffix.lower() in _LOADERS
    ]
    logger.info("documents_discovered", count=len(found), directory=str(root))
    return found
