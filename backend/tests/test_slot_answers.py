"""Тесты распознавания ответа на вопрос о значении поля.

Заполнение модуля диалогом целиком зависело от того, разберёт ли
языковая модель реплику сотрудника. Если не разбирала — значение не
попадало никуда, счётчик заполненных полей стоял на месте, и помощник
задавал тот же вопрос снова. Повтор ответа ничего не менял: выхода из
этого состояния не было.

Запасной путь детерминирован: вопрос задавался про одно поле, значит
короткая реплика без просьбы изготовить документ — ответ на него.
Проверяется именно граница между ответом и новой просьбой.
"""

from __future__ import annotations

import pytest

from app.agents.clarifier import (
    PLAIN_ANSWER_MAX_LENGTH,
    fill_template_slots,
    looks_like_plain_answer,
)


class TestPlainAnswers:
    """Реплики, которые следует записать в ожидаемое поле."""

    @pytest.mark.parametrize(
        "message",
        [
            "Автоматизированная система управления казначейскими операциями «Казначейство»",
            "протокол приёмочных испытаний от 14.10.2026 № 17-ПИ",
            "АС «Казначейство»",
            "Департамент финансового мониторинга",
            "И.И. Иванов",
            "01.11.2026",
        ],
    )
    def test_value_is_accepted(self, message: str) -> None:
        """Значение реквизита принимается как ответ."""
        assert looks_like_plain_answer(message) is True


class TestNotAnswers:
    """Реплики, которые ответом не являются."""

    @pytest.mark.parametrize(
        "message",
        [
            "Мне нужно подготовить приказ о вводе системы в эксплуатацию",
            "Составь распоряжение о создании рабочей группы",
            "Заполни шаблон служебной записки",
            "Требуется приказ по основной деятельности",
        ],
    )
    def test_request_is_rejected(self, message: str) -> None:
        """Просьба изготовить документ в поле не записывается.

        Иначе первая же реплика сотрудника попала бы в первый реквизит
        документа целиком.
        """
        assert looks_like_plain_answer(message) is False

    def test_empty_message_is_rejected(self) -> None:
        """Пустая реплика значением не является."""
        assert looks_like_plain_answer("   ") is False

    def test_long_reasoning_is_rejected(self) -> None:
        """Длинное рассуждение — не значение реквизита.

        Реквизит документа такой длины не бывает, а попадание в документ
        случайного абзаца хуже повторного вопроса.
        """
        assert looks_like_plain_answer("а" * (PLAIN_ANSWER_MAX_LENGTH + 1)) is False

    def test_boundary_length_is_accepted(self) -> None:
        """На самой границе длины ответ ещё принимается."""
        assert looks_like_plain_answer("а" * PLAIN_ANSWER_MAX_LENGTH) is True


class _StubGenerator:
    """Генератор с заранее заданным разбором реплики."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def generate_json(self, system: str, user: str, stage: str = "") -> dict[str, object]:
        """Возвращает подготовленный разбор, не обращаясь к модели."""
        return self.payload


FIELDS = [
    {"name": "responsible", "label": "Ответственный", "required": True},
    {"name": "signer", "label": "Подписант", "required": True},
]


class TestAnswerGoesToTheAskedField:
    """Ответ попадает в то поле, о котором спросили.

    Разбор моделью ошибается двумя способами, и оба наблюдались на
    живом заполнении: она либо не извлекает ничего, либо кладёт ответ
    в соседнее поле, заполненное шагом раньше. В первом случае счётчик
    стоял на месте, во втором — прежнее значение оказывалось перетёрто,
    а спрошенное поле пустым. Выйти из обоих состояний сотрудник не мог.
    """

    @pytest.fixture(autouse=True)
    def _no_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Подменяет генератор, чтобы разбор был предсказуем."""
        self.payload: dict[str, object] = {}
        monkeypatch.setattr(
            "app.agents.clarifier.get_generator",
            lambda: _StubGenerator(self.payload),
        )

    @pytest.mark.asyncio
    async def test_unparsed_answer_fills_the_asked_field(self) -> None:
        """Нераспознанный ответ записывается в спрошенное поле."""
        self.payload = {"extracted": {}}

        updated, missing, _ = await fill_template_slots(
            template_name="Приказ",
            fields=FIELDS,
            filled={"responsible": "Соколов А.В."},
            message="И.И. Иванов",
        )

        assert updated["signer"] == "И.И. Иванов"
        assert missing == []

    @pytest.mark.asyncio
    async def test_wrong_field_does_not_overwrite_earlier_answer(self) -> None:
        """Ошибка модели не затирает собранное раньше значение."""
        self.payload = {"extracted": {"responsible": "И.И. Иванов"}}

        updated, missing, _ = await fill_template_slots(
            template_name="Приказ",
            fields=FIELDS,
            filled={"responsible": "Соколов А.В."},
            message="И.И. Иванов",
        )

        assert updated["responsible"] == "Соколов А.В."
        assert updated["signer"] == "И.И. Иванов"
        assert missing == []

    @pytest.mark.asyncio
    async def test_correct_extraction_is_kept(self) -> None:
        """Верный разбор моделью даёт тот же результат."""
        self.payload = {"extracted": {"signer": "И.И. Иванов"}}

        updated, _, _ = await fill_template_slots(
            template_name="Приказ",
            fields=FIELDS,
            filled={"responsible": "Соколов А.В."},
            message="И.И. Иванов",
        )

        assert updated == {"responsible": "Соколов А.В.", "signer": "И.И. Иванов"}

    @pytest.mark.asyncio
    async def test_unasked_field_is_not_guessed(self) -> None:
        """Поле, о котором не спрашивали, не заполняется догадкой.

        Из ответа про ответственного модель выводила фамилию и
        записывала её в подписанты. Реквизит, которого сотрудник не
        называл, попадал в документ и выглядел заполненным — проверять
        его никто бы не стал.
        """
        self.payload = {
            "extracted": {
                "responsible": "заместитель директора Соколов А.В.",
                "signer": "Соколов А.В.",
            }
        }

        updated, missing, _ = await fill_template_slots(
            template_name="Приказ",
            fields=FIELDS,
            filled={"responsible": ""},
            message="заместитель директора Департамента ИТ Соколов А.В.",
        )

        assert updated["responsible"] == "заместитель директора Департамента ИТ Соколов А.В."
        assert not updated.get("signer")
        assert missing == ["signer"]
