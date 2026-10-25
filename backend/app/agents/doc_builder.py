"""Узел формирования черновика документа.

Реализует сценарий защиты: «как система самостоятельно заполняет шаблон
документа, задавая уточняющие вопросы сотруднику».

Логика узла:

* извлечь из реплики всё, что сотрудник уже сообщил;
* если обязательные поля не собраны — задать вопрос по одному следующему
  полю и сохранить промежуточное состояние;
* когда всё собрано — передать заполнение в фоновую задачу генерации DOCX
  и вернуть сотруднику маршрут согласования со ссылками на пункты.

Состояние заполнения хранится в Redis: диалог может прерваться и
продолжиться позже, и сотруднику не придётся отвечать на вопросы заново.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.clarifier import fill_template_slots
from app.agents.state import AnswerStatus, AssistantState
from app.agents.template_matcher import format_approval_route
from app.core.logging import get_logger
from app.core.metrics import graph_node_errors_total, graph_node_latency_seconds
from app.db.redis import slots_clear, slots_get, slots_set

logger = get_logger(__name__)

NODE_NAME = "doc_builder"


def format_progress(
    *, template_name: str, filled: dict[str, Any], fields: list[dict[str, Any]]
) -> str:
    """Формирует сводку по собранным полям.

    Показывать прогресс важно: сотрудник видит, что система действительно
    накапливает данные, а не спрашивает по кругу.

    Args:
        template_name: Название шаблона.
        filled: Собранные значения.
        fields: Схема полей шаблона.

    Returns:
        Текст сводки.
    """
    required = [f for f in fields if f.get("required")]
    done = sum(1 for f in required if filled.get(f["name"]))

    lines = [f"**{template_name}** — заполнено {done} из {len(required)} обязательных полей."]
    if filled:
        lines.append("")
        for field in fields:
            value = filled.get(field["name"])
            if not value:
                continue
            label = field.get("label") or field["name"]
            shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
            if len(shown) > 120:
                shown = shown[:117] + "…"
            lines.append(f"- {label}: {shown}")
    return "\n".join(lines)


def format_next_question(question: str, field: dict[str, Any] | None, progress: str) -> str:
    """Формирует сообщение со следующим вопросом по шаблону.

    Args:
        question: Текст вопроса.
        field: Описание запрашиваемого поля.
        progress: Сводка по уже собранным данным.

    Returns:
        Текст сообщения сотруднику.
    """
    lines = [progress, "", question]
    if field and field.get("type") == "list":
        lines.append("_Можно перечислить через запятую или с новой строки._")
    if field and not field.get("required"):
        lines.append("_Поле необязательное — можете ответить «пропустить»._")
    return "\n".join(lines)


def format_ready_message(
    *,
    template_name: str,
    filled: dict[str, Any],
    fields: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    related_clauses: list[str],
) -> str:
    """Формирует сообщение о готовности черновика.

    Args:
        template_name: Название шаблона.
        filled: Собранные значения.
        fields: Схема полей.
        approvals: Маршрут согласования.
        related_clauses: Пункты инструкции, определяющие требования к документу.

    Returns:
        Текст сообщения с итогом и дальнейшими шагами.
    """
    lines = [
        f"Все обязательные поля для документа «{template_name}» собраны. "
        f"Готовлю файл — он появится здесь через несколько секунд.",
        "",
        format_progress(template_name=template_name, filled=filled, fields=fields),
    ]

    route = format_approval_route(approvals)
    if route:
        lines.append(route)

    if related_clauses:
        lines.append("")
        lines.append(
            f"_Требования к оформлению: {', '.join(related_clauses)} "
            "инструкции по документообороту._"
        )

    lines.append("")
    lines.append(
        "Проверьте реквизиты перед отправкой на согласование. "
        "Если нужно что-то исправить — скажите, какое поле изменить."
    )
    return "\n".join(lines)


def _extract_related_clauses(state: AssistantState) -> list[str]:
    """Достаёт пункты инструкции, связанные с выбранным шаблоном.

    Args:
        state: Состояние графа.

    Returns:
        Список номеров пунктов.
    """
    candidates = state.get("template_candidates") or []
    if not candidates:
        return []
    raw = candidates[0].get("related_clauses", [])
    return [item["code"] if isinstance(item, dict) else str(item) for item in raw]


async def build_document_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: собирает данные и формирует черновик документа.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния: либо следующий вопрос, либо готовность черновика.
    """
    started = time.perf_counter()

    try:
        template_code = state.get("template_code", "")
        template_name = state.get("template_name", "документ")
        fields = state.get("template_fields", [])
        session_id = state.get("session_id", "")

        if not template_code or not fields:
            return {
                "answer": "Сначала нужно определить, какой бланк использовать.",
                "answer_status": AnswerStatus.CLARIFICATION.value,
                "node_trace": [f"{NODE_NAME}:no_template"],
            }

        stored = await slots_get(session_id) if session_id else {}
        filled: dict[str, Any] = {**stored, **state.get("filled_fields", {})}

        updated, missing, next_question = await fill_template_slots(
            template_name=template_name,
            fields=fields,
            filled=filled,
            message=state.get("question", ""),
        )

        if session_id:
            await slots_set(session_id, updated)

        progress = format_progress(template_name=template_name, filled=updated, fields=fields)

        if missing:
            target_field = next((f for f in fields if f["name"] == missing[0]), None)
            question_text = next_question or (
                f"Укажите: {target_field.get('label', missing[0]) if target_field else missing[0]}"
            )

            logger.info(
                "template_filling_in_progress",
                template_code=template_code,
                filled_count=len(updated),
                missing_count=len(missing),
            )
            return {
                "answer": format_next_question(question_text, target_field, progress),
                "answer_status": AnswerStatus.TEMPLATE_FILLING.value,
                "filled_fields": updated,
                "missing_fields": missing,
                "clarifying_questions": [
                    {
                        "question": question_text,
                        "field": missing[0],
                        "options": (target_field or {}).get("options", []),
                    }
                ],
                "node_trace": [f"{NODE_NAME}:filling"],
            }

        if session_id:
            await slots_clear(session_id)

        logger.info(
            "template_ready_for_generation",
            template_code=template_code,
            fields_filled=len(updated),
        )
        return {
            "answer": format_ready_message(
                template_name=template_name,
                filled=updated,
                fields=fields,
                approvals=state.get("approval_route", []),
                related_clauses=_extract_related_clauses(state),
            ),
            "answer_status": AnswerStatus.TEMPLATE_READY.value,
            "filled_fields": updated,
            "missing_fields": [],
            "node_trace": [f"{NODE_NAME}:ready"],
        }

    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("doc_builder_failed")
        return {
            "answer": "Не удалось сформировать черновик документа. Повторите запрос.",
            "answer_status": AnswerStatus.ERROR.value,
            "error": str(exc),
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)
