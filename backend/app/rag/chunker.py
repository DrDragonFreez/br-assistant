"""Нарезка нормативных текстов на фрагменты с привязкой к пунктам.

Обычный рекурсивный сплиттер режет текст по символам и теряет границы пунктов.
Для инструкции это неприемлемо: ответ обязан ссылаться на конкретное положение
(«п. 3.4.2»), а значит каждый фрагмент должен знать свой номер пункта, раздел
и страницу.

Алгоритм работает в три прохода:

1. Разметка структуры — поиск заголовков разделов, номеров пунктов и приложений.
2. Группировка абзацев в блоки по пунктам.
3. Доведение блоков до целевого размера: длинные режутся по предложениям
   с перекрытием, короткие соседние объединяются в пределах одного раздела.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CLAUSE_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,4})\.?\s+(?=[А-ЯЁA-Z«(])")
SECTION_RE = re.compile(
    r"^\s*(?:(?i:РАЗДЕЛ|ГЛАВА|ЧАСТЬ|ПОДРАЗДЕЛ)\s+[IVXL\d]+\.?\s*(.*)"
    r"|([А-ЯЁ][А-ЯЁ\s,\-]{8,}))\s*$"
)
ANNEX_RE = re.compile(r"^\s*ПРИЛОЖЕНИЕ\s*(?:№\s*)?(\d+|[IVXL]+)", re.IGNORECASE)
SUBITEM_RE = re.compile(r"^\s*(?:[а-яё]\)|\d{1,2}\)|[-–—•])\s+")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«(])")
WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")

@dataclass(slots=True)
class Chunk:
    """Фрагмент документа, готовый к индексации.

    Attributes:
        index: Порядковый номер фрагмента в версии документа.
        content: Текст фрагмента.
        clause_number: Номер пункта инструкции, если распознан. Это основной
            адрес фрагмента — тот, с которого он начинается.
        clause_numbers: Номера всех пунктов, текст которых попал во фрагмент.
            Короткие пункты объединяются в один фрагмент, и без полного
            списка цитата указывала бы на соседний пункт вместо нужного.
        section_title: Заголовок раздела.
        breadcrumbs: Путь «Раздел → Подраздел → Пункт» для отображения в цитате.
        page_number: Номер страницы исходного файла.
        annex_number: Номер приложения, если фрагмент относится к приложению.
        tokens: Приблизительное число токенов.
        extra: Дополнительные признаки (наличие таблицы, срока и т. п.).
    """

    index: int
    content: str
    clause_number: str = ""
    clause_numbers: list[str] = field(default_factory=list)
    section_title: str = ""
    breadcrumbs: str = ""
    page_number: int | None = None
    annex_number: str = ""
    tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def clause_address(self) -> str:
        """Перечень пунктов фрагмента для подписи цитаты.

        Returns:
            Строка вида «3.4» либо «3.1, 3.4» — не более трёх номеров,
            дальше ставится многоточие, иначе подпись перестаёт читаться.
        """
        numbers = self.clause_numbers or ([self.clause_number] if self.clause_number else [])
        if not numbers:
            return ""
        if len(numbers) <= 3:
            return ", ".join(numbers)
        return ", ".join(numbers[:3]) + " …"

    @property
    def citation_label(self) -> str:
        """Человекочитаемая подпись цитаты.

        Returns:
            Строка вида «п. 3.4.2» либо «Приложение 2», либо название раздела.
        """
        address = self.clause_address
        if self.annex_number:
            base = f"Приложение {self.annex_number}"
            return f"{base}, п. {address}" if address else base
        if address:
            return f"п. {address}"
        if self.section_title:
            return self.section_title
        return f"фрагмент {self.index + 1}"


@dataclass(slots=True)
class _Block:
    """Промежуточный блок текста до приведения к целевому размеру."""

    lines: list[str] = field(default_factory=list)
    clause_number: str = ""
    clause_numbers: list[str] = field(default_factory=list)
    section_title: str = ""
    annex_number: str = ""
    page_number: int | None = None
    primary_length: int = 0

    @property
    def text(self) -> str:
        """Текст блока."""
        return "\n".join(self.lines).strip()


def estimate_tokens(text: str) -> int:
    """Оценивает число токенов без загрузки токенизатора.

    Для русского текста эмпирически выходит около 2.8 символа на токен.
    Точность здесь не критична: оценка нужна лишь для контроля размера
    контекстного окна.

    Args:
        text: Текст.

    Returns:
        Приблизительное число токенов.
    """
    return max(1, int(len(text) / 2.8))


class ClauseAwareChunker:
    """Сплиттер, сохраняющий привязку фрагментов к пунктам инструкции."""

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        min_chunk_size: int = 220,
    ) -> None:
        """Инициализирует сплиттер.

        Args:
            chunk_size: Целевой размер фрагмента в символах.
            chunk_overlap: Размер перекрытия между соседними фрагментами.
            min_chunk_size: Фрагменты короче этого значения объединяются с соседними.
        """
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP
        self.min_chunk_size = min_chunk_size

    # ─────────────────── Проход 1–2: разметка и блоки ───────────────────
    def _split_into_blocks(self, pages: list[tuple[int, str]]) -> list[_Block]:
        """Разбивает страницы на блоки по границам пунктов.

        Args:
            pages: Список пар ``(номер страницы, текст)``.

        Returns:
            Список блоков с заполненной структурной разметкой.
        """
        blocks: list[_Block] = []
        current = _Block()
        section_title = ""
        annex_number = ""

        for page_number, page_text in pages:
            for raw_line in page_text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue

                annex_match = ANNEX_RE.match(line)
                if annex_match:
                    if current.lines:
                        blocks.append(current)
                    annex_number = annex_match.group(1)
                    section_title = line
                    current = _Block(
                        section_title=section_title,
                        annex_number=annex_number,
                        page_number=page_number,
                    )
                    current.lines.append(line)
                    continue

                section_match = SECTION_RE.match(line)
                if section_match and not CLAUSE_RE.match(line):
                    if current.lines:
                        blocks.append(current)
                    section_title = (
                        section_match.group(1) or section_match.group(2) or line
                    ).strip()
                    current = _Block(
                        section_title=section_title,
                        annex_number=annex_number,
                        page_number=page_number,
                    )
                    continue

                clause_match = CLAUSE_RE.match(line)
                if clause_match:
                    if current.lines:
                        blocks.append(current)
                    current = _Block(
                        clause_number=clause_match.group(1),
                        clause_numbers=[clause_match.group(1)],
                        section_title=section_title,
                        annex_number=annex_number,
                        page_number=page_number,
                    )
                    current.lines.append(line)
                    continue

                if current.page_number is None:
                    current.page_number = page_number
                current.lines.append(line)

        if current.lines:
            blocks.append(current)
        return [block for block in blocks if block.text]

    # ─────────────────── Проход 3: размер ───────────────────
    def _split_long_text(self, text: str) -> list[str]:
        """Режет длинный текст по предложениям с перекрытием.

        Args:
            text: Текст блока.

        Returns:
            Список частей, каждая не длиннее ``chunk_size``.
        """
        if len(text) <= self.chunk_size:
            return [text]

        sentences = SENTENCE_RE.split(text)
        parts: list[str] = []
        buffer: list[str] = []
        length = 0

        for sentence in sentences:
            sentence_len = len(sentence)
            if length + sentence_len > self.chunk_size and buffer:
                parts.append(" ".join(buffer).strip())
                overlap: list[str] = []
                overlap_len = 0
                for item in reversed(buffer):
                    if overlap_len + len(item) > self.chunk_overlap:
                        break
                    overlap.insert(0, item)
                    overlap_len += len(item)
                buffer = overlap
                length = overlap_len
            buffer.append(sentence)
            length += sentence_len

        if buffer:
            parts.append(" ".join(buffer).strip())

        result: list[str] = []
        for part in parts:
            if len(part) <= self.chunk_size * 1.5:
                result.append(part)
                continue
            step = self.chunk_size - self.chunk_overlap
            for start in range(0, len(part), step):
                piece = part[start : start + self.chunk_size].strip()
                if piece:
                    result.append(piece)
        return [item for item in result if item]

    def _merge_small_blocks(self, blocks: list[_Block]) -> list[_Block]:
        """Объединяет слишком короткие блоки в пределах одного раздела.

        Короткие пункты («3.1. Общие положения.») по отдельности бесполезны
        для поиска: они дают шум в выдаче.

        Args:
            blocks: Исходные блоки.

        Returns:
            Блоки, доведённые до минимального размера.
        """
        merged: list[_Block] = []
        for block in blocks:
            if (
                merged
                and len(merged[-1].text) < self.min_chunk_size
                and merged[-1].section_title == block.section_title
                and merged[-1].annex_number == block.annex_number
                and len(merged[-1].text) + len(block.text) <= self.chunk_size
            ):
                previous = merged[-1]
                primary_length = previous.primary_length or len(previous.text)
                incoming_length = len(block.text)
                previous.lines.extend(block.lines)

                for number in block.clause_numbers:
                    if number not in previous.clause_numbers:
                        previous.clause_numbers.append(number)

                if block.clause_number and incoming_length > primary_length:
                    previous.clause_number = block.clause_number
                    previous.primary_length = incoming_length
                else:
                    previous.primary_length = primary_length

                if not previous.clause_number:
                    previous.clause_number = block.clause_number
                continue
            merged.append(block)
        return merged

    # ─────────────────── Публичный интерфейс ───────────────────
    def split(self, pages: list[tuple[int, str]]) -> list[Chunk]:
        """Нарезает документ на фрагменты.

        Args:
            pages: Список пар ``(номер страницы, текст)``.

        Returns:
            Список фрагментов с заполненной структурной разметкой.
        """
        blocks = self._merge_small_blocks(self._split_into_blocks(pages))
        chunks: list[Chunk] = []
        counter = 0

        for block in blocks:
            clause_address = ", ".join(block.clause_numbers[:3]) or block.clause_number
            breadcrumbs = " → ".join(
                item
                for item in (
                    f"Приложение {block.annex_number}" if block.annex_number else "",
                    block.section_title,
                    f"п. {clause_address}" if clause_address else "",
                )
                if item
            )

            for part in self._split_long_text(block.text):
                if not WORD_RE.search(part):
                    continue

                prefix = ""
                if block.clause_numbers and not part.lstrip().startswith(block.clause_number):
                    prefix = f"[п. {', '.join(block.clause_numbers)}] "
                content = f"{prefix}{part}".strip()

                extra: dict[str, Any] = {
                    "has_table": "[Таблица]" in part,
                    "has_deadline": bool(
                        re.search(r"\b(срок|в течение|не позднее|рабочи[хе] дн)", part, re.I)
                    ),
                    "has_subitems": bool(SUBITEM_RE.search(part)),
                }
                if block.clause_numbers:
                    extra["clauses"] = list(block.clause_numbers)

                chunks.append(
                    Chunk(
                        index=counter,
                        content=content,
                        clause_number=block.clause_number,
                        clause_numbers=list(block.clause_numbers),
                        section_title=block.section_title,
                        breadcrumbs=breadcrumbs,
                        page_number=block.page_number,
                        annex_number=block.annex_number,
                        tokens=estimate_tokens(content),
                        extra=extra,
                    )
                )
                counter += 1

        logger.info(
            "document_chunked",
            chunks=len(chunks),
            with_clause=sum(1 for c in chunks if c.clause_number),
            annexes=len({c.annex_number for c in chunks if c.annex_number}),
        )
        return chunks

    def split_text(self, text: str) -> list[Chunk]:
        """Нарезает одну строку текста (для загруженных черновиков).

        Args:
            text: Текст документа.

        Returns:
            Список фрагментов.
        """
        return self.split([(1, text)])


def get_chunker() -> ClauseAwareChunker:
    """Возвращает сплиттер с настройками из конфигурации.

    Returns:
        Экземпляр :class:`ClauseAwareChunker`.
    """
    return ClauseAwareChunker()
