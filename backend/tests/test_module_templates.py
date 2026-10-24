"""Тесты каталога модульных документов.

Каталог — единый источник правды для двух разных вещей: схемы вопросов,
по которой помощник заполняет бланк, и карты правок, по которой
``scripts/build_templates.py`` превращает файл организаторов в шаблон
docxtpl. Расхождение между ними даёт пустой реквизит в готовом документе
и обнаруживается только при генерации, поэтому согласованность
проверяется тестом.
"""

from __future__ import annotations

import re

import pytest

from app.agents.router import DOC_KIND_PATTERNS
from app.core.rbac import AccessLevel
from app.db.module_templates import MODULE_TEMPLATES, MODULES_BY_CODE, ModuleTemplate

# Типы полей, которые умеет отрисовать интерфейс (frontend/lib/types.ts).
ALLOWED_TYPES = {"string", "text", "date", "list", "number"}


PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)")
# Переменная цикла docxtpl ({%tr for row in persons_rows %}) — не поле бланка.
LOOP_VARIABLE_RE = re.compile(r"\bfor\s+(\w+)\s+in\b")


def layout_field_names(module: ModuleTemplate) -> set[str]:
    """Собирает имена полей, упомянутые в карте правок.

    Поле попадает в бланк четырьмя способами: подстановкой в пропуск
    (``blanks``), подстановкой даты (``dates``), заменой литерала или
    варианта через «/» (``literals``, ``variants``, ``custom``) и через
    производное значение (``uses``). Учитываются все, иначе проверка
    размещения полей ругалась бы на корректную карту.

    Args:
        module: Описание модуля.

    Returns:
        Множество имён полей, размещённых в бланке.
    """
    names: set[str] = set()
    loop_variables: set[str] = set()
    specs = list((module.layout.get("paragraphs") or {}).values())
    specs += list((module.layout.get("cells") or {}).values())

    for spec in specs:
        names.update(name for name in spec.get("blanks", []) if name)
        names.update(spec.get("dates", []))
        names.update(spec.get("uses", []))
        if spec.get("optional"):
            names.add(spec["optional"])

        replacements = list((spec.get("variants") or {}).values())
        replacements += list((spec.get("literals") or {}).values())
        replacements += [replacement for _, replacement in spec.get("custom", [])]
        for replacement in replacements:
            names.update(PLACEHOLDER_RE.findall(replacement))
            loop_variables.update(LOOP_VARIABLE_RE.findall(replacement))

    return names - loop_variables


def test_catalogue_is_not_empty() -> None:
    """Все семь модулей кейса описаны."""
    assert len(MODULE_TEMPLATES) == 7


def test_codes_are_unique() -> None:
    """Код шаблона — первичный ключ реестра, дубликаты недопустимы."""
    codes = [module.code for module in MODULE_TEMPLATES]

    assert len(codes) == len(set(codes))
    assert len(MODULES_BY_CODE) == len(codes)


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_field_names_are_unique(module: ModuleTemplate) -> None:
    """Имена полей внутри бланка не повторяются."""
    names = [item["name"] for item in module.fields]

    assert len(names) == len(set(names))


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_fields_are_described_for_dialogue(module: ModuleTemplate) -> None:
    """У каждого поля есть подпись, вопрос и допустимый тип.

    Вопрос — это то, что помощник произносит при заполнении: без него
    пошаговый сценарий превратится в «укажите значение поля subject».
    """
    for item in module.fields:
        assert item["label"], f"{module.code}: у поля {item['name']} нет подписи"
        assert item["question"], f"{module.code}: у поля {item['name']} нет вопроса"
        assert item["type"] in ALLOWED_TYPES, f"{module.code}: тип {item['type']} не отображается"
        if item.get("options"):
            assert len(item["options"]) >= 2, "выбор из одного значения не нужен"


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_layout_references_declared_fields(module: ModuleTemplate) -> None:
    """Карта правок не ссылается на поля, которых нет в схеме."""
    declared = {item["name"] for item in module.fields}
    referenced = layout_field_names(module)

    unknown = referenced - declared
    assert not unknown, f"{module.code}: в карте есть незаявленные поля: {sorted(unknown)}"


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_required_fields_are_placed_in_layout(module: ModuleTemplate) -> None:
    """Каждое обязательное поле размещено в бланке.

    Поле подписанта — исключение: оно подставляется в блок подписи
    заменой литерала «И. О. Фамилия», а не в пропуск.
    """
    referenced = layout_field_names(module)

    for item in module.fields:
        if not item["required"]:
            continue
        name = item["name"]
        assert name in referenced, (
            f"{module.code}: обязательное поле «{name}» не размещено в бланке"
        )


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_approval_route_is_ordered(module: ModuleTemplate) -> None:
    """Маршрут визирования пронумерован подряд и со сроками.

    Состав подразделений взят из Матрицы обязательного визирования,
    сроки — из [п. 10.3] инструкции.
    """
    route = module.required_approvals

    assert route, f"{module.code}: маршрут визирования пуст"
    assert [step["order"] for step in route] == list(range(1, len(route) + 1))
    for step in route:
        assert step["department"], "в маршруте есть шаг без подразделения"
        assert step["term_days"] > 0, "срок рассмотрения должен быть положительным"


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_doc_kind_is_recognised_by_router(module: ModuleTemplate) -> None:
    """Вид документа шаблона маршрутизатор умеет определять.

    Иначе подбор бланка по обращению «нужно распоряжение» не найдёт
    ни один шаблон: фильтр по виду документа не совпадёт.
    """
    assert module.doc_kind in DOC_KIND_PATTERNS


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_module_refers_to_instruction_clauses(module: ModuleTemplate) -> None:
    """Шаблон ссылается на пункты инструкции и имеет слова для поиска."""
    assert module.related_clauses, f"{module.code}: не указаны пункты инструкции"
    assert "9.16" in module.related_clauses, "модульный документ описан в [п. 9.16]"
    assert len(module.keywords) >= 3, "по двум словам бланк не найдётся"


@pytest.mark.parametrize("module", MODULE_TEMPLATES, ids=lambda item: item.code)
def test_db_payload_matches_model(module: ModuleTemplate) -> None:
    """Запись для реестра содержит все поля модели шаблона."""
    payload = module.to_db_payload(templates_dir="/app/data/templates")

    assert payload["code"] == module.code
    assert payload["file_path"].endswith(f"{module.code}.docx")
    assert payload["access_level"] is AccessLevel.INTERNAL
    assert payload["is_active"] is True
    assert len(payload["fields_schema"]) == len(module.fields)
    # Копии, а не ссылки: изменение записи в БД не должно править каталог.
    payload["fields_schema"][0]["label"] = "изменено"
    assert module.fields[0]["label"] != "изменено"


def test_service_zones_are_never_dropped() -> None:
    """Карта правок не удаляет служебные зоны бланка.

    §3.3 Методических рекомендаций требует сохранить зоны даты,
    регистрационного номера и визуализации электронной подписи. В бланках
    это абзац 3 и последняя таблица, поэтому их индексы не должны
    попадать в списки удаления.
    """
    for module in MODULE_TEMPLATES:
        dropped_paragraphs = set(module.layout.get("drop_paragraphs") or [])
        assert 3 not in dropped_paragraphs, f"{module.code}: удалена зона даты и номера"

        dropped_tables = set(module.layout.get("drop_tables") or [])
        # Удаляется только гриф утверждения — первая таблица бланка.
        assert dropped_tables <= {0}, f"{module.code}: удалена лишняя таблица"
