"""Узел уточняющих вопросов.

Требование слайда 9: помощник должен уточнять недостающую информацию, а не
отвечать наугад. Узел работает в двух режимах:

* **Уточнение вопроса** — когда обращение слишком общее, чтобы определить
  применимые требования.
* **Заполнение шаблона** — когда сотруднику нужен документ, и система
  последовательно собирает значения полей.

Во втором режиме вопросы берутся из схемы шаблона, а не придумываются моделью:
это гарантирует, что будут собраны ровно те реквизиты, которых требует бланк.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.agents.state import AnswerStatus, AssistantState
from app.core.logging import get_logger
from app.core.metrics import (
    graph_node_errors_total,
    graph_node_latency_seconds,
    rag_clarification_total,
)
from app.rag.generator import get_generator
from app.rag.prompts import build_clarifier_prompt, build_slot_filling_prompt

logger = get_logger(__name__)

NODE_NAME = "clarifier"

PRESET_QUESTIONS: dict[str, dict[str, Any]] = {
    "тип документа": {
        "question": "О каком документе идёт речь?",
        "field": "document_kind",
        "options": ["Служебная записка", "Приказ", "Деловое письмо", "Протокол совещания"],
    },
    "направление документа": {
        "question": "Документ входящий или исходящий?",
        "field": "direction",
        "options": ["Входящий", "Исходящий", "Внутренний"],
    },
    "форма документа": {
        "question": "Документ оформляется на бумаге или в электронном виде?",
        "field": "form",
        "options": ["Бумажный", "Электронный"],
    },
    "суть вопроса": {
        "question": (
            "Уточните, пожалуйста, что именно нужно выяснить — "
            "какой этап работы с документом вас интересует?"
        ),
        "field": "topic",
        "options": ["Подготовка и оформление", "Согласование", "Регистрация", "Сроки исполнения"],
    },
}


def build_preset_questions(missing_info: list[str]) -> list[dict[str, Any]]:
    """Подбирает готовые уточняющие вопросы по списку пробелов.

    Args:
        missing_info: Чего не хватает для ответа.

    Returns:
        Список вопросов; пустой, если готовых формулировок нет.
    """
    questions: list[dict[str, Any]] = []
    for item in missing_info:
        preset = PRESET_QUESTIONS.get(item.strip().lower())
        if preset and preset not in questions:
            questions.append(dict(preset))
    return questions[:3]


def format_clarification_message(questions: list[dict[str, Any]], reason: str = "") -> str:
    """Формирует текст сообщения с уточняющими вопросами.

    Args:
        questions: Уточняющие вопросы.
        reason: Пояснение, зачем нужны уточнения.

    Returns:
        Готовый текст для отображения сотруднику.
    """
    if not questions:
        return (
            "Уточните, пожалуйста, ваш вопрос — так я смогу найти применимые "
            "требования инструкции."
        )

    lines: list[str] = []
    if reason:
        lines.append(reason)
    elif len(questions) == 1:
        lines.append("Чтобы дать точный ответ, уточните один момент:")
    else:
        lines.append("Чтобы дать точный ответ, уточните несколько моментов:")

    lines.append("")
    for index, item in enumerate(questions, start=1):
        prefix = f"{index}. " if len(questions) > 1 else ""
        lines.append(f"{prefix}{item['question']}")
        options = item.get("options") or []
        if options:
            lines.append(f"   _Варианты: {' · '.join(options)}_")

    return "\n".join(lines)


async def generate_clarifying_questions(
    question: str,
    missing_info: list[str],
    history: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Формирует уточняющие вопросы.

    Сначала применяются готовые формулировки; если их не хватает, остальные
    вопросы генерирует модель.

    Args:
        question: Обращение сотрудника.
        missing_info: Выявленные пробелы.
        history: История диалога.

    Returns:
        Кортеж ``(вопросы, пояснение)``.
    """
    preset = build_preset_questions(missing_info)
    if len(preset) >= 2:
        return preset, ""

    system, user = build_clarifier_prompt(question, missing_info, history)
    try:
        payload = await get_generator().generate_json(system, user, stage="clarifier")
    except Exception as exc:  # noqa: BLE001
        logger.warning("clarifier_llm_failed", error=str(exc))
        return preset or [PRESET_QUESTIONS["суть вопроса"]], ""

    raw_questions = payload.get("clarifying_questions", [])
    generated: list[dict[str, Any]] = []
    if isinstance(raw_questions, list):
        for item in raw_questions[:3]:
            if not isinstance(item, dict) or not item.get("question"):
                continue
            options = item.get("options", [])
            generated.append(
                {
                    "question": str(item["question"]),
                    "field": str(item.get("field", "")),
                    "options": [str(o) for o in options] if isinstance(options, list) else [],
                }
            )

    combined = preset + [q for q in generated if q not in preset]
    return combined[:3] or [PRESET_QUESTIONS["суть вопроса"]], str(payload.get("reason", ""))


# Длиннее этого ответ на вопрос об одном реквизите не бывает: это уже
# не значение поля, а рассуждение, и записывать его в документ нельзя.
PLAIN_ANSWER_MAX_LENGTH = 300

# Побуждение изготовить документ. Такая реплика не является ответом на
# вопрос о значении поля: с неё заполнение начинается.
REQUEST_PATTERN = re.compile(
    r"\b(подготов|соста|сформир|созда|сдела|напиш|оформ|заполни|нужен|нужна|нужно|"
    r"требуется|надо)\w*",
    re.IGNORECASE,
)


def looks_like_plain_answer(message: str) -> bool:
    """Проверяет, что реплика — значение поля, а не новая просьба.

    Args:
        message: Реплика сотрудника.

    Returns:
        True, если реплику можно записать в ожидаемое поле.
    """
    text = message.strip()
    if not text or len(text) > PLAIN_ANSWER_MAX_LENGTH:
        return False
    return not REQUEST_PATTERN.search(text)


async def fill_template_slots(
    *,
    template_name: str,
    fields: list[dict[str, Any]],
    filled: dict[str, Any],
    message: str,
) -> tuple[dict[str, Any], list[str], str]:
    """Извлекает значения полей шаблона из реплики сотрудника.

    Args:
        template_name: Название шаблона.
        fields: Схема полей.
        filled: Уже собранные значения.
        message: Последняя реплика.

    Returns:
        Кортеж ``(обновлённые значения, незаполненные обязательные поля,
        следующий вопрос)``.
    """
    system, user = build_slot_filling_prompt(template_name, fields, filled, message)

    try:
        payload = await get_generator().generate_json(system, user, stage="slot_filling")
    except Exception as exc:  # noqa: BLE001
        logger.warning("slot_filling_failed", error=str(exc))
        payload = {}

    # Поле, о котором помощник спрашивал в прошлой реплике: вопрос
    # всегда задаётся про первое незаполненное обязательное поле.
    awaited = next(
        (
            field["name"]
            for field in fields
            if field.get("required") and not filled.get(field["name"])
        ),
        "",
    )

    extracted = payload.get("extracted", {})
    updated = dict(filled)
    if isinstance(extracted, dict):
        for key, value in extracted.items():
            if value in (None, "", []):
                continue
            if any(field["name"] == key for field in fields):
                updated[key] = value

    # Ответ на заданный вопрос принимается детерминированно.
    #
    # Полагаться на разбор моделью нельзя, и ошибается она тремя
    # способами, каждый из которых наблюдался на живом заполнении:
    # не извлекает ничего — счётчик стоит, и помощник повторяет тот же
    # вопрос; кладёт ответ в соседнее поле, заполненное шагом раньше,
    # и перетирает его; выводит из ответа значение поля, о котором не
    # спрашивала, — из должности ответственного вытащила фамилию и
    # записала её в подписанты.
    #
    # Последнее опаснее всего: реквизит, которого сотрудник не
    # называл, попадает в документ и выглядит заполненным, так что
    # проверять его никто не станет.
    #
    # Поэтому в цикле «вопрос → ответ» принимается ровно одно поле —
    # то, о котором спросили. Остальной разбор реплики отбрасывается.
    # Назвал сотрудник несколько значений сразу — помощник задаст
    # следующий вопрос: лишний шаг дешевле выдуманного реквизита.
    if awaited and looks_like_plain_answer(message):
        if filled:
            updated = {**filled, awaited: message.strip()}
            logger.info("slot_filled_from_answer", field=awaited)
        elif not updated.get(awaited):
            # Первая реплика: разбирать её на несколько полей можно и
            # нужно, здесь сотрудник описывает задачу целиком.
            updated[awaited] = message.strip()
            logger.info("slot_filled_verbatim", field=awaited)

    missing = [
        field["name"]
        for field in fields
        if field.get("required") and not updated.get(field["name"])
    ]

    next_question = ""
    if missing:
        target = next((f for f in fields if f["name"] == missing[0]), None)
        if target:
            next_question = target.get("question") or (
                f"Укажите: {target.get('label', target['name'])}"
            )
    if not next_question:
        next_question = str(payload.get("next_question", ""))

    return updated, missing, next_question


async def clarify_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: задаёт уточняющий вопрос.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с текстом уточнения.
    """
    started = time.perf_counter()

    try:
        missing_info = state.get("missing_info", [])
        questions, reason = await generate_clarifying_questions(
            state.get("question", ""),
            missing_info,
            state.get("history"),
        )

        for item in missing_info[:3]:
            rag_clarification_total.labels(missing_slot=item[:40] or "unknown").inc()

        message = format_clarification_message(questions, reason)

        logger.info("clarification_requested", questions=len(questions))

        return {
            "answer": message,
            "answer_status": AnswerStatus.CLARIFICATION.value,
            "clarifying_questions": questions,
            "citations": [],
            "critic_verdict": "",
            "node_trace": [f"{NODE_NAME}:asked"],
        }
    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("clarifier_node_failed")
        return {
            "answer": "Уточните, пожалуйста, ваш вопрос.",
            "answer_status": AnswerStatus.CLARIFICATION.value,
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)
