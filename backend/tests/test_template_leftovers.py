"""Сценарий организаторов №3: остатки шаблона и порядок дальнейших действий.

Организаторы ждут, что проверка выявит «незаполненные поля, лишние
элементы шаблона и возможные ошибки оформления» и подскажет «дальнейший
порядок действий: согласование, визирование, подписание и регистрацию».
"""

from __future__ import annotations

from app.services.doc_validator import (
    ORDER_NEXT_STEPS,
    check_template_leftovers,
    next_steps_for,
)

# Начало модуля организаторов в том виде, как его читает загрузчик.
MODULE_HEAD = """МОДУЛЬНЫЙ ДОКУМЕНТ
НАИМЕНОВАНИЕ ОРГАНИЗАЦИИ
РАСПОРЯЖЕНИЕ
ДАТА / РЕГИСТРАЦИОННЫЙ НОМЕР (НЕ УДАЛЯТЬ)
О создании проектного комитета по [проекту / проектам] Цифровой архив [1]
для обеспечения контроля реализации проекта, реализуемого(ых) ДИТ,
Примечания по заполнению модульного документа
1. Указывается полное наименование проекта.

[Таблица 1] Состав столбцов: УТВЕРЖДАЮ | Директор Департамента ДОУ
"""

# Собранный черновик: служебных элементов нет, метки таблиц от загрузчика.
FILLED = """НАИМЕНОВАНИЕ ОРГАНИЗАЦИИ
РАСПОРЯЖЕНИЕ
ДАТА / РЕГИСТРАЦИОННЫЙ НОМЕР (НЕ УДАЛЯТЬ)
О создании проектного комитета по проекту Цифровой архив
для обеспечения контроля реализации проекта, реализуемого ДИТ,
ОБЯЗЫВАЮ:
1. Создать Проектный комитет.

[Таблица 1] Состав столбцов: ГЕРБ
[Таблица 2] Состав столбцов: Заместитель Председателя Организации | И.И. Иванов
"""


class TestTemplateLeftovers:
    """Служебные элементы модуля по п. 3.3 Методических рекомендаций."""

    def test_module_elements_are_reported(self) -> None:
        """Все шесть видов остатков попадают в одно замечание."""
        violations = check_template_leftovers(MODULE_HEAD, "order")

        assert len(violations) == 1
        problem = violations[0].problem
        assert violations[0].severity == "major"
        assert violations[0].clause == "3.3"
        for label in ("МОДУЛЬНЫЙ ДОКУМЕНТ", "УТВЕРЖДАЮ", "Примечания", "[1]", "квадратных", "(ых)"):
            assert label in problem

    def test_filled_draft_is_clean(self) -> None:
        """В собранном черновике остатков нет."""
        assert check_template_leftovers(FILLED, "order") == []

    def test_template_field_index_is_not_a_note_number(self) -> None:
        """Индекс внутри поля docxtpl — не номер примечания."""
        assert check_template_leftovers("{{ persons_rows[0] }}", "order") == []

    def test_approval_stamp_counts_only_for_orders(self) -> None:
        """У положения гриф «УТВЕРЖДАЮ» свой, остатком модуля он не считается."""
        assert check_template_leftovers("УТВЕРЖДАЮ\nДиректор", "") == []
        assert check_template_leftovers("УТВЕРЖДАЮ\nДиректор", "order")


class TestNextSteps:
    """Порядок действий после проверки."""

    def test_order_steps_cover_full_route(self) -> None:
        """Согласование, сроки, замечания, визирование, подписание, регистрация."""
        steps = next_steps_for("order", "compliant")

        assert [item["clause"] for item in steps] == ["10.2", "10.3", "10.5", "10.6", "10.1", "7.1"]
        assert [item["step"] for item in steps] == list(range(1, 7))

    def test_issues_come_first(self) -> None:
        """При замечаниях первым шагом идёт доработка."""
        steps = next_steps_for("order", "major_issues")

        assert steps[0]["clause"] == ""
        assert "Устраните" in steps[0]["action"]
        assert len(steps) == len(ORDER_NEXT_STEPS) + 1

    def test_other_kinds_get_no_invented_route(self) -> None:
        """Для писем и записок порядок не достраивается."""
        assert next_steps_for("letter", "compliant") == []
        assert next_steps_for("memo", "minor_issues") == []
