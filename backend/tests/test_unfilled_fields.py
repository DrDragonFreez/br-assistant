"""Незаполненные поля модуля в проекте документа.

Пустой модуль проходил проверку как соответствующий: дата и подпись в
проекте законно отмечаются сведением о следующем этапе, а других
формальных признаков пустоты проверка не знала. Теперь оставшиеся поля
шаблона и прочерки — критичное замечание со ссылкой на методические
рекомендации [п. 3.2, 3.3].
"""

from __future__ import annotations

from app.services.doc_validator import check_unfilled_fields, compute_verdict

# Начало шаблона MOD-ORDER-IS-DEPLOY.docx в том виде, как его читает загрузчик.
TEMPLATE_TEXT = """НАИМЕНОВАНИЕ ОРГАНИЗАЦИИ
ПРИКАЗ
ДАТА / РЕГИСТРАЦИОННЫЙ НОМЕР (НЕ УДАЛЯТЬ)
г. Москва
О вводе в эксплуатацию {{ component_title }}
На основании {{ readiness_doc }}, содержащего решение о готовности к вводу
ПРИКАЗЫВАЮ:
1. Ввести «{{ component_short }}» в эксплуатацию с {{ start_date }}
{% if repealed %}Признать утратившим силу {{ repealed }}{% endif %}

[Таблица 1] Состав столбцов: ГЕРБ
"""

# Исходный модуль организаторов: поля обозначены прочерками.
MODULE_TEXT = """ПРИКАЗ
О вводе в эксплуатацию ____________________
На основании ________________________, содержащего решение о готовности,
ПРИКАЗЫВАЮ:
1. Ввести «______________» в эксплуатацию с «___» ____________ 20___ г.
"""

# Собранный проект: значения на месте, метки таблиц от загрузчика.
FILLED_TEXT = """НАИМЕНОВАНИЕ ОРГАНИЗАЦИИ
ПРИКАЗ
ДАТА / РЕГИСТРАЦИОННЫЙ НОМЕР (НЕ УДАЛЯТЬ)
О вводе в эксплуатацию подсистемы электронного архива
ПРИКАЗЫВАЮ:
1. Ввести «ПЭА» в эксплуатацию с 01.11.2026.

[Таблица 1] Состав столбцов: ГЕРБ
[Таблица 2] Состав столбцов: Заместитель Председателя Организации | И.И. Иванов
"""


class TestUnfilledFields:
    """Признаки незаполненного модуля."""

    def test_template_fields_are_critical(self) -> None:
        """Поля docxtpl в проекте — критичное замечание с примерами полей."""
        violations = check_unfilled_fields(TEMPLATE_TEXT)

        assert len(violations) == 1
        assert violations[0].severity == "critical"
        assert violations[0].clause == "3.2"
        assert "{{ component_title }}" in violations[0].problem
        assert "Методические рекомендации" in violations[0].requirement

    def test_module_blanks_are_critical(self) -> None:
        """Прочерки исходного модуля тоже считаются пропусками."""
        violations = check_unfilled_fields(MODULE_TEXT)

        assert len(violations) == 1
        assert violations[0].severity == "critical"

    def test_filled_draft_passes(self) -> None:
        """В собранном проекте пропусков нет, метки таблиц не мешают."""
        assert check_unfilled_fields(FILLED_TEXT) == []

    def test_date_zone_is_not_a_blank(self) -> None:
        """Служебная зона даты и номера сохраняется по п. 3.3."""
        text = FILLED_TEXT + "\nот «___» ____________ 20___ г. № ______\n"

        assert check_unfilled_fields(text) == []

    def test_empty_module_is_not_compliant(self) -> None:
        """Пустой модуль больше не получает вердикт «соответствует»."""
        verdict, score = compute_verdict(check_unfilled_fields(TEMPLATE_TEXT))

        assert verdict == "major_issues"
        assert score < 1.0
