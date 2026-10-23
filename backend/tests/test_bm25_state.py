"""Тесты словаря BM25 и его переноса между процессами.

Индексацию выполняет воркер Celery, а запросы обслуживает процесс API —
это разные процессы, и словарь передаётся между ними через файл состояния.
Если API не перечитывает файл, после ``make index`` гибридный поиск молча
остаётся плотным: разреженный вектор запроса оказывается пустым, и ошибки
при этом нет нигде. Поэтому поведение проверяется тестом.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.rag.bm25 import BM25Encoder, tokenize

CORPUS_FIRST = [
    "Служебная записка согласуется непосредственным руководителем.",
    "Срок согласования в Центре документационного обеспечения — 2 рабочих дня.",
]
CORPUS_SECOND = [
    "Доверенность оформляется на бланке организации и регистрируется в журнале.",
]


@pytest.fixture
def artifacts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Переносит каталог состояния в temp, чтобы не трогать рабочий словарь."""
    monkeypatch.setattr(settings, "ARTIFACTS_DIR", tmp_path)
    return tmp_path


def _touch_later(path: Path, seconds: int = 2) -> None:
    """Сдвигает время изменения файла вперёд.

    Разрешение ``st_mtime`` в тесте может оказаться грубее, чем интервал
    между двумя записями, поэтому время выставляется явно.

    Args:
        path: Файл состояния.
        seconds: На сколько секунд сдвинуть отметку.
    """
    stamp = os.stat(path).st_mtime + seconds
    os.utime(path, (stamp, stamp))


# ─────────────────────────── ТОКЕНИЗАЦИЯ ───────────────────────────
def test_tokenizer_drops_stopwords_and_short_tokens() -> None:
    """Служебные слова и обрывки в словарь не попадают."""
    tokens = tokenize("Записка в Центр и на бланке")

    assert "в" not in tokens
    assert "и" not in tokens
    assert "записка" in tokens


def test_tokenizer_keeps_clause_number_whole() -> None:
    """Номер пункта — единый токен: разбивать его по точкам нельзя."""
    assert "3.4.2" in tokenize("Согласно пункту 3.4.2 инструкции")


# ─────────────────────────── СОСТОЯНИЕ ───────────────────────────
def test_empty_encoder_returns_empty_query_vector() -> None:
    """Необученный кодировщик отдаёт пустой вектор, а не падает.

    На этом держится деградация поиска до плотного: ретривер смотрит на
    ``is_empty`` и переключается на dense-ветку.
    """
    encoder = BM25Encoder()

    assert not encoder.is_fitted
    assert encoder.encode_query("срок согласования").is_empty


def test_state_survives_process_boundary(artifacts_dir: Path) -> None:
    """Словарь, сохранённый одним процессом, читается другим."""
    writer = BM25Encoder().fit(CORPUS_FIRST)
    saved = writer.save()

    assert saved.exists()

    reader = BM25Encoder()

    assert reader.load()
    assert reader.is_fitted
    assert reader.vocabulary == writer.vocabulary
    assert not reader.encode_query("срок согласования").is_empty


def test_reload_if_stale_picks_up_reindexed_vocabulary(artifacts_dir: Path) -> None:
    """Обновление файла состояния подхватывается без перезапуска процесса."""
    writer = BM25Encoder().fit(CORPUS_FIRST)
    writer.save()

    reader = BM25Encoder()
    reader.load()
    before = len(reader.vocabulary)

    writer.update(CORPUS_SECOND)
    state_path = writer.save()
    _touch_later(state_path)

    assert reader.reload_if_stale()
    assert len(reader.vocabulary) > before
    assert not reader.encode_query("доверенность на бланке").is_empty


def test_reload_if_stale_is_quiet_without_changes(artifacts_dir: Path) -> None:
    """Без изменений файл не перечитывается: проверка идёт на каждый запрос."""
    BM25Encoder().fit(CORPUS_FIRST).save()

    reader = BM25Encoder()
    reader.load()

    assert not reader.reload_if_stale()
    assert not reader.reload_if_stale()


def test_reload_if_stale_tolerates_missing_state(artifacts_dir: Path) -> None:
    """Отсутствие файла состояния — не ошибка: база могла быть не проиндексирована."""
    encoder = BM25Encoder()

    assert not encoder.reload_if_stale()
    assert not encoder.is_fitted
