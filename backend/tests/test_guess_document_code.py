"""Опознание документа базы знаний по имени файла при `make index`.

Форма загрузки в админке сохраняет файл как «<КОД>_<хеш>.docx» в
каталог инструкций. Без опознания по префиксу такой файл считался
инструкцией, и индексация делала его действующей Инструкцией по ДОУ.
"""

from __future__ import annotations

import pytest

from app.workers.cli import guess_document_code


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("GUIDE-MODULES_0736c53b.docx", "GUIDE-MODULES"),
        ("MATRIX-APPROVAL_ec946696.docx", "MATRIX-APPROVAL"),
        ("INSTR-DOC_1a2b3c4d.pdf", "INSTR-DOC"),
    ],
)
def test_uploaded_file_is_recognised_by_code_prefix(filename: str, expected: str) -> None:
    """Файл из формы загрузки опознаётся по коду в начале имени."""
    assert guess_document_code(filename) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Инструкция_по_ДОУ_учебный_пример.pdf", "INSTR-DOC"),
        ("Матрица_обязательного_визирования_внутренних_и_внешних_документов.docx", "MATRIX-APPROVAL"),
        ("Методические_рекомендации_по_работе_с_модульными_документами.docx", "GUIDE-MODULES"),
    ],
)
def test_case_materials_keep_marker_recognition(filename: str, expected: str) -> None:
    """Материалы кейса по-прежнему опознаются по словам в имени."""
    assert guess_document_code(filename) == expected


def test_unknown_prefix_falls_back_to_markers() -> None:
    """Префикс, не совпадающий с кодом документа, не перехватывает опознание."""
    assert guess_document_code("Проект_рекомендаций.docx") == "GUIDE-MODULES"
