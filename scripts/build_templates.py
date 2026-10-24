#!/usr/bin/env python3
"""Превращает модули организаторов в шаблоны docxtpl.

Организаторы передали семь модульных документов — приказы и распоряжения
с постоянными формулировками, подчёркиваниями для заполнения и
нумерованными примечаниями. Это бланки для человека, а не шаблоны для
подстановки: в них нет ни одного места, которое docxtpl мог бы заполнить.

Скрипт приводит их к машинному виду, выполняя ровно то, что §3.3
Методических рекомендаций требует от исполнителя при подготовке проекта:

* подчёркивания заменяются полями ``{{ имя }}``;
* удаляются номера примечаний, сами примечания и общие правила;
* удаляется гриф утверждения вверху первой страницы и пометка
  «МОДУЛЬНЫЙ ДОКУМЕНТ»;
* неиспользуемые варианты через «/» становятся полями выбора;
* необязательные пункты в квадратных скобках — условными блоками;
* **сохраняются** служебные зоны: дата, регистрационный номер, герб
  и блок визуализации электронной подписи.

Карта правок для каждого модуля описана в
``backend/app/db/module_templates.py`` — там же, где схема полей, по которой
помощник задаёт вопросы. Один источник правды: расхождение имён полей
между шаблоном и схемой проявилось бы пустым реквизитом в готовом
документе, то есть уже на защите.

Запуск::

    python scripts/build_templates.py
    python scripts/build_templates.py --source data/templates --check
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_PATH))

from app.db.module_templates import MODULE_TEMPLATES, ModuleTemplate  # noqa: E402

DEFAULT_SOURCE = Path("data/templates")

# Подчёркивание как место для заполнения: не короче четырёх символов,
# чтобы не задеть «20___ г.» и ««___»» в датах.
BLANK_RE = re.compile(r"_{4,}")
# Дата в бланке: «___» месяц 20___ г.
DATE_RE = re.compile(r"«___»\s*_{4,}\s*20___\s*г\.")
# Номер примечания в тексте: « [12]».
NOTE_MARKER_RE = re.compile(r"\s*\[\d{1,2}\]")
# Лишние пробелы после удаления фрагментов.
SPACES_RE = re.compile(r"[ \t]{2,}")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


class BuildError(Exception):
    """Сборка шаблона невозможна."""


def set_paragraph_text(paragraph: Any, text: str) -> None:
    """Заменяет текст абзаца, сохраняя его стиль.

    Текст записывается в первый прогон, остальные удаляются: иначе
    подстановка разъехалась бы по прогонам, на которые Word делит абзац
    при правках. Внутреннее форматирование абзаца при этом теряется,
    поэтому служебные блоки с пометкой «НЕ УДАЛЯТЬ» скрипт не трогает.

    Args:
        paragraph: Абзац python-docx.
        text: Новый текст.
    """
    if not paragraph.runs:
        paragraph.add_run(text)
        return

    paragraph.runs[0].text = text
    for run in paragraph.runs[1:]:
        run.text = ""


def rewrite_text(text: str, spec: dict[str, Any], used: set[str]) -> str:
    """Применяет к тексту карту правок одного места.

    Порядок фаз важен: сначала нестандартные конструкции целиком, затем
    даты, затем одиночные подчёркивания — иначе дата ««___» ____ 20___ г.»
    была бы разобрана как отдельный пропуск.

    Args:
        text: Исходный текст абзаца или ячейки.
        spec: Карта правок: custom, dates, blanks, variants, literals.
        used: Множество, куда добавляются использованные поля.

    Returns:
        Текст с подстановками docxtpl.
    """
    # Номера примечаний убираются первыми: они выглядят как «[2]», и после
    # подстановки неотличимы от обращения к элементу списка «{{ row[0] }}»
    # в разметке docxtpl — чистка в конце съела бы индекс.
    result = NOTE_MARKER_RE.sub("", text)

    for pattern, replacement in spec.get("custom", []):
        result = re.sub(pattern, replacement, result)
        for name in re.findall(r"\{\{\s*(\w+)\s*\}\}", replacement):
            used.add(name)

    for literal, replacement in (spec.get("literals") or {}).items():
        result = result.replace(literal, replacement)
        for name in re.findall(r"\{\{\s*(\w+)\s*\}\}", replacement):
            used.add(name)

    for literal, replacement in (spec.get("variants") or {}).items():
        result = result.replace(literal, replacement)
        for name in re.findall(r"\{\{\s*(\w+)\s*\}\}", replacement):
            used.add(name)

    for name in spec.get("dates", []):
        result = DATE_RE.sub(f"{{{{ {name} }}}}", result, count=1)
        used.add(name)

    for name in spec.get("blanks", []):
        replacement = f"{{{{ {name} }}}}" if name else ""
        if not BLANK_RE.search(result):
            raise BuildError(f"не найден пропуск для поля «{name}» в тексте: {text[:70]}")
        result = BLANK_RE.sub(replacement, result, count=1)
        if name:
            used.add(name)

    # Необязательный пункт: оборачивается условием и лишается скобок.
    optional = spec.get("optional")
    if optional:
        result = result.replace("[", "").replace("]", "")
        result = f"{{% if {optional} %}}{result}{{% endif %}}"
        used.add(optional)

    # Поля, которые подставляются не напрямую, а через производное
    # значение (например, persons → persons_rows в цикле таблицы).
    used.update(spec.get("uses", []))

    result = SPACES_RE.sub(" ", result).strip()
    return result


def drop_element(element: Any) -> None:
    """Удаляет элемент документа из дерева XML.

    Args:
        element: Абзац, таблица или строка таблицы python-docx.
    """
    node = element._element
    node.getparent().remove(node)


def resolve_source(source_dir: Path, name: str) -> Path:
    """Находит исходный файл модуля, не придираясь к нормализации Unicode.

    Имена файлов приходят от организаторов и содержат «й». В архивах,
    собранных на macOS, эта буква записана разложенной формой (``и`` плюс
    комбинирующая кратка), а в коде — составной. Побайтовое сравнение таких
    имён не совпадает, хотя на экране они выглядят одинаково, поэтому
    сравнение идёт по нормализованному виду.

    Args:
        source_dir: Каталог с модулями.
        name: Имя файла из описания модуля.

    Returns:
        Путь к найденному файлу.

    Raises:
        BuildError: Файл не найден.
    """
    candidate = source_dir / name
    if candidate.exists():
        return candidate

    wanted = unicodedata.normalize("NFC", name)
    for item in source_dir.glob("*.docx"):
        if unicodedata.normalize("NFC", item.name) == wanted:
            return item

    raise BuildError(f"нет исходного файла {name}")


def build_template(module: ModuleTemplate, source_dir: Path) -> tuple[Path, set[str]]:
    """Собирает шаблон docxtpl из модуля организаторов.

    Args:
        module: Описание модуля с картой правок.
        source_dir: Каталог с исходными файлами модулей.

    Returns:
        Кортеж ``(путь к шаблону, использованные поля)``.

    Raises:
        BuildError: Исходный файл отсутствует либо карта не совпала
            с содержимым документа.
    """
    from docx import Document

    source = resolve_source(source_dir, module.source)
    document = Document(str(source))
    layout = module.layout
    used: set[str] = set()

    # ── Абзацы: подстановки по карте ──
    paragraphs = document.paragraphs
    for index, spec in (layout.get("paragraphs") or {}).items():
        if index >= len(paragraphs):
            raise BuildError(f"в документе нет абзаца {index}")

        paragraph = paragraphs[index]
        anchor = spec.get("anchor", "")
        if anchor and anchor not in paragraph.text:
            raise BuildError(
                f"абзац {index} не содержит «{anchor}»: {paragraph.text[:70]!r}. "
                "Модуль изменился — проверьте карту в module_templates.py"
            )
        set_paragraph_text(paragraph, rewrite_text(paragraph.text, spec, used))

    # ── Ячейки таблиц ──
    tables = document.tables
    for (table_index, row_index, column_index), spec in (layout.get("cells") or {}).items():
        if table_index >= len(tables):
            raise BuildError(f"в документе нет таблицы {table_index}")

        rows = tables[table_index].rows
        if row_index >= len(rows):
            raise BuildError(f"в таблице {table_index} нет строки {row_index}")

        cells = rows[row_index].cells
        if column_index >= len(cells):
            raise BuildError(f"в таблице {table_index} нет столбца {column_index}")

        cell = cells[column_index]
        new_text = rewrite_text(cell.text, spec, used)
        set_paragraph_text(cell.paragraphs[0], new_text)
        for extra in cell.paragraphs[1:]:
            drop_element(extra)

    # ── Примечания: от маркера и до конца текста ──
    notes_marker = layout.get("notes_marker")
    if notes_marker:
        removing = False
        for paragraph in list(document.paragraphs):
            if not removing and paragraph.text.strip().startswith(notes_marker):
                removing = True
            if removing:
                drop_element(paragraph)

    # ── Пометка «МОДУЛЬНЫЙ ДОКУМЕНТ» и гриф утверждения ──
    for index in sorted(layout.get("drop_paragraphs") or [], reverse=True):
        if index < len(paragraphs):
            drop_element(paragraphs[index])

    for table_index, row_indexes in (layout.get("drop_table_rows") or {}).items():
        table_rows = tables[table_index].rows
        for row_index in sorted(row_indexes, reverse=True):
            if row_index < len(table_rows):
                drop_element(table_rows[row_index])

    for index in sorted(layout.get("drop_tables") or [], reverse=True):
        if index < len(tables):
            drop_element(tables[index])

    # ── Остатки номеров примечаний в нетронутых местах ──
    for paragraph in document.paragraphs:
        if NOTE_MARKER_RE.search(paragraph.text):
            set_paragraph_text(paragraph, NOTE_MARKER_RE.sub("", paragraph.text))

    target = source_dir / module.filename
    document.save(str(target))
    return target, used


def verify_template(module: ModuleTemplate, path: Path) -> list[str]:
    """Проверяет шаблон пробным рендером со значениями-заполнителями.

    Это главная проверка: расхождение между схемой полей и шаблоном
    иначе обнаружилось бы только при формировании документа на защите.

    Args:
        module: Описание модуля.
        path: Путь к собранному шаблону.

    Returns:
        Список замечаний; пустой, если шаблон отрендерился и все
        обязательные поля в него попали.
    """
    from docx import Document
    from docxtpl import DocxTemplate

    problems: list[str] = []
    context: dict[str, Any] = {}
    for item in module.fields:
        name = item["name"]
        if item.get("options"):
            context[name] = item["options"][0]
        elif item["type"] == "date":
            context[name] = "15 марта 2026 г."
        else:
            context[name] = f"ЗНАЧЕНИЕ_{name.upper()}"
    # Производные значения, которые подставляет генератор документов.
    context["persons_rows"] = [["Департамент ДОУ", "начальник отдела Иванов И. И."]]

    template = DocxTemplate(str(path))

    # Какие переменные шаблон на самом деле использует. Поле может
    # подставляться не напрямую, а через производное значение
    # (persons → persons_rows в цикле строк таблицы).
    declared = template.get_undeclared_template_variables()
    for item in module.fields:
        if not item["required"]:
            continue
        name = item["name"]
        if name not in declared and f"{name}_rows" not in declared:
            problems.append(f"обязательное поле «{name}» не размещено в шаблоне")

    try:
        template.render(context)
    except Exception as error:  # noqa: BLE001 — показываем причину как есть
        return [*problems, f"шаблон не рендерится: {error}"]

    rendered = path.with_name(f"{path.stem}.render.docx")
    template.save(str(rendered))

    document = Document(str(rendered))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            text += "\n" + " ".join(cell.text for cell in row.cells)

    for item in module.fields:
        name = item["name"]
        if not item["required"] or name not in declared:
            continue
        placeholder = context[name]
        if isinstance(placeholder, str) and placeholder not in text:
            problems.append(f"значение поля «{name}» не попало в документ")

    if BLANK_RE.search(text):
        problems.append("в заполненном документе остались линии для заполнения")
    if NOTE_MARKER_RE.search(text):
        problems.append("в заполненном документе остались номера примечаний")
    if "МОДУЛЬНЫЙ ДОКУМЕНТ" in text:
        problems.append("осталась пометка «МОДУЛЬНЫЙ ДОКУМЕНТ»")
    if "УТВЕРЖДАЮ" in text:
        problems.append("остался гриф утверждения")
    if "Примечания по заполнению" in text:
        problems.append("остались примечания по заполнению")

    # Служебные зоны обязаны сохраниться — это прямое требование §3.3.
    for required_marker in ("ДАТА / РЕГИСТРАЦИОННЫЙ НОМЕР", "ВИЗУАЛИЗАЦИЯ ЭП"):
        if required_marker not in text:
            problems.append(f"утрачена служебная зона «{required_marker}»")

    rendered.unlink(missing_ok=True)
    return problems


def main() -> int:
    """Собирает шаблоны для всех модулей.

    Returns:
        Код выхода процесса.
    """
    parser = argparse.ArgumentParser(description="Сборка шаблонов docxtpl из модулей")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Только проверить уже собранные шаблоны, не пересобирая",
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"{RED}✗{RESET} каталог {args.source} не найден")
        return 2

    failures = 0
    print(f"Шаблоны модульных документов: {args.source}")
    print("-" * 70)

    for module in MODULE_TEMPLATES:
        target = args.source / module.filename
        try:
            if not args.check:
                target, used = build_template(module, args.source)
                declared = {item["name"] for item in module.fields}
                unused = declared - used - {"signer"}
                if unused:
                    print(
                        f"{YELLOW}!{RESET} {module.code}: поля не размещены в шаблоне: "
                        f"{', '.join(sorted(unused))}"
                    )
            elif not target.exists():
                raise BuildError("шаблон не собран")

            problems = verify_template(module, target)
            if problems:
                failures += 1
                print(f"{RED}✗{RESET} {module.code}")
                for problem in problems:
                    print(f"    {problem}")
            else:
                print(f"{GREEN}✓{RESET} {module.code} — {target.name}, полей {len(module.fields)}")

        except BuildError as error:
            failures += 1
            print(f"{RED}✗{RESET} {module.code}: {error}")

    print("-" * 70)
    if failures:
        print(f"{RED}Не собрано шаблонов: {failures} из {len(MODULE_TEMPLATES)}{RESET}")
        return 1

    print(f"{GREEN}Готово: {len(MODULE_TEMPLATES)} шаблонов{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
