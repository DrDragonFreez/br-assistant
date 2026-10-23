"""Разреженные векторы BM25 для гибридного поиска.

Плотные эмбеддинги хорошо ловят смысл, но плохо — точные формулировки и
номера пунктов. В нормативных текстах это существенно: запрос «что сказано в
пункте 4.2» должен находить именно этот пункт, а не похожий по смыслу.

Поэтому к плотному вектору добавляется разреженный, построенный по схеме BM25.
Словарь собирается при индексации и сохраняется вместе с коллекцией, чтобы
запросы векторизовались в том же пространстве.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"[а-яёa-z0-9]+(?:\.[0-9]+)*", re.IGNORECASE)
STOPWORDS: frozenset[str] = frozenset(
    """
    и в во не что он на я с со как а то все она так его но да ты к у же вы за бы
    по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг
    ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
    ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без
    будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой
    совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех
    никогда можно при наконец два об другой хоть после над больше тот через эти нас
    про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда
    лучше чуть том нельзя такой им более всегда конечно всю между также либо
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Разбивает текст на токены.

    Номера пунктов («3.4.2») сохраняются целиком: это ключевой идентификатор
    положения, разбивать его по точкам нельзя.

    Args:
        text: Исходный текст.

    Returns:
        Список нормализованных токенов.
    """
    tokens = [token.lower() for token in TOKEN_RE.findall(text)]
    return [
        token
        for token in tokens
        if token not in STOPWORDS and (len(token) > 2 or token.replace(".", "").isdigit())
    ]


@dataclass(slots=True)
class SparseVector:
    """Разреженный вектор в формате Qdrant."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True, если вектор не содержит ни одного значения."""
        return not self.indices


class BM25Encoder:
    """Кодировщик текста в разреженный BM25-вектор.

    Attributes:
        k1: Параметр насыщения частоты терма.
        b: Параметр нормализации по длине документа.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        """Инициализирует кодировщик с пустым словарём.

        Args:
            k1: Параметр насыщения частоты терма.
            b: Параметр нормализации по длине документа.
        """
        self.k1 = k1
        self.b = b
        self.vocabulary: dict[str, int] = {}
        self.doc_frequencies: dict[int, int] = {}
        self.total_documents: int = 0
        self.average_length: float = 1.0
        self._fitted: bool = False
        self._state_mtime: float = 0.0

    @property
    def is_fitted(self) -> bool:
        """True, если словарь обучен и готов к кодированию."""
        return self._fitted

    def fit(self, corpus: list[str]) -> BM25Encoder:
        """Строит словарь и статистику по корпусу документов.

        Args:
            corpus: Тексты всех фрагментов базы знаний.

        Returns:
            Сам кодировщик (для цепочек вызовов).
        """
        self.vocabulary.clear()
        self.doc_frequencies.clear()
        total_length = 0

        for document in corpus:
            tokens = tokenize(document)
            total_length += len(tokens)
            for token in set(tokens):
                term_id = self.vocabulary.setdefault(token, len(self.vocabulary))
                self.doc_frequencies[term_id] = self.doc_frequencies.get(term_id, 0) + 1

        self.total_documents = max(len(corpus), 1)
        self.average_length = max(total_length / self.total_documents, 1.0)
        self._fitted = True

        logger.info(
            "bm25_fitted",
            vocabulary_size=len(self.vocabulary),
            documents=self.total_documents,
            avg_length=round(self.average_length, 1),
        )
        return self

    def _idf(self, term_id: int) -> float:
        """Считает обратную документную частоту терма.

        Args:
            term_id: Идентификатор терма в словаре.

        Returns:
            Значение IDF (неотрицательное).
        """
        df = self.doc_frequencies.get(term_id, 0)
        return max(
            math.log(1 + (self.total_documents - df + 0.5) / (df + 0.5)),
            0.0,
        )

    def encode_document(self, text: str) -> SparseVector:
        """Кодирует фрагмент документа.

        Args:
            text: Текст фрагмента.

        Returns:
            Разреженный вектор с BM25-весами.
        """
        if not self._fitted:
            return SparseVector()

        tokens = tokenize(text)
        if not tokens:
            return SparseVector()

        counts = Counter(tokens)
        length_norm = self.k1 * (1 - self.b + self.b * len(tokens) / self.average_length)

        indices: list[int] = []
        values: list[float] = []
        for token, frequency in counts.items():
            term_id = self.vocabulary.get(token)
            if term_id is None:
                continue
            weight = self._idf(term_id) * frequency * (self.k1 + 1) / (frequency + length_norm)
            if weight > 0:
                indices.append(term_id)
                values.append(round(weight, 5))

        return SparseVector(indices=indices, values=values)

    def encode_query(self, text: str) -> SparseVector:
        """Кодирует поисковый запрос.

        Для запроса нормализация по длине не применяется: вес терма
        определяется только его редкостью.

        Args:
            text: Текст запроса.

        Returns:
            Разреженный вектор запроса.
        """
        if not self._fitted:
            return SparseVector()

        tokens = tokenize(text)
        counts = Counter(tokens)

        indices: list[int] = []
        values: list[float] = []
        for token, frequency in counts.items():
            term_id = self.vocabulary.get(token)
            if term_id is None:
                continue
            weight = self._idf(term_id) * frequency
            if weight > 0:
                indices.append(term_id)
                values.append(round(weight, 5))

        return SparseVector(indices=indices, values=values)

    # ─────────────────── Сохранение словаря ───────────────────
    def _state_path(self) -> Path:
        """Путь к файлу состояния кодировщика."""
        return Path(settings.ARTIFACTS_DIR) / "bm25_state.json"

    def save(self, path: str | Path | None = None) -> Path:
        """Сохраняет словарь и статистику на диск.

        Args:
            path: Путь к файлу; по умолчанию ``artifacts/bm25_state.json``.

        Returns:
            Путь, по которому сохранено состояние.
        """
        target = Path(path) if path else self._state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "k1": self.k1,
                    "b": self.b,
                    "vocabulary": self.vocabulary,
                    "doc_frequencies": {str(k): v for k, v in self.doc_frequencies.items()},
                    "total_documents": self.total_documents,
                    "average_length": self.average_length,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._state_mtime = target.stat().st_mtime
        logger.info("bm25_state_saved", path=str(target), terms=len(self.vocabulary))
        return target

    def load(self, path: str | Path | None = None) -> bool:
        """Загружает словарь с диска.

        Args:
            path: Путь к файлу состояния.

        Returns:
            True, если состояние успешно загружено.
        """
        source = Path(path) if path else self._state_path()
        if not source.exists():
            logger.warning("bm25_state_missing", path=str(source))
            return False
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            self.k1 = data["k1"]
            self.b = data["b"]
            self.vocabulary = data["vocabulary"]
            self.doc_frequencies = {int(k): v for k, v in data["doc_frequencies"].items()}
            self.total_documents = data["total_documents"]
            self.average_length = data["average_length"]
            self._fitted = bool(self.vocabulary)
            self._state_mtime = source.stat().st_mtime
        except (KeyError, json.JSONDecodeError, OSError) as exc:
            logger.error("bm25_state_load_failed", error=str(exc))
            return False
        logger.info("bm25_state_loaded", terms=len(self.vocabulary))
        return self._fitted

    def reload_if_stale(self) -> bool:
        """Перечитывает словарь, если файл состояния обновился.

        Индексация выполняется в процессе Celery-воркера, а запросы
        обслуживает процесс API: без этой проверки API продолжал бы работать
        со словарём, загруженным при старте, и гибридный поиск молча
        деградировал бы до плотного до перезапуска сервиса.

        Returns:
            True, если состояние было перечитано.
        """
        source = self._state_path()
        try:
            mtime = source.stat().st_mtime
        except OSError:
            return False

        if mtime <= self._state_mtime:
            return False

        logger.info("bm25_state_changed", path=str(source))
        return self.load(source)

    def update(self, new_documents: list[str]) -> None:
        """Дополняет словарь новыми документами без полного пересчёта.

        Используется при добавлении новой редакции инструкции: полная
        переиндексация всей базы ради одного документа избыточна.

        Args:
            new_documents: Тексты новых фрагментов.
        """
        if not self._fitted:
            self.fit(new_documents)
            return

        total_length = self.average_length * self.total_documents
        for document in new_documents:
            tokens = tokenize(document)
            total_length += len(tokens)
            for token in set(tokens):
                term_id = self.vocabulary.setdefault(token, len(self.vocabulary))
                self.doc_frequencies[term_id] = self.doc_frequencies.get(term_id, 0) + 1

        self.total_documents += len(new_documents)
        self.average_length = max(total_length / max(self.total_documents, 1), 1.0)
        logger.info("bm25_updated", added=len(new_documents), terms=len(self.vocabulary))


_encoder: BM25Encoder | None = None


def get_bm25_encoder() -> BM25Encoder:
    """Возвращает singleton-кодировщик BM25.

    При первом обращении пытается загрузить сохранённый словарь: воркер
    индексации и API работают в разных процессах, и состояние должно
    переживать перезапуск. При последующих обращениях словарь перечитывается,
    если файл состояния изменился — иначе после ``make index`` работающий
    API продолжал бы искать по устаревшему словарю.

    Returns:
        Общий экземпляр :class:`BM25Encoder`.
    """
    global _encoder
    if _encoder is None:
        _encoder = BM25Encoder()
        _encoder.load()
    else:
        _encoder.reload_if_stale()
    return _encoder


def reset_bm25_encoder() -> None:
    """Сбрасывает кеш кодировщика (после переиндексации)."""
    global _encoder
    _encoder = None
