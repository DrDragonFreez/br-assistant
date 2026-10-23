"""Узел подбора шаблона модульного документа.

Критерий №3 оценки — поиск подходящих шаблонов и помощь в заполнении.
Подбор идёт по трём сигналам, от надёжного к приблизительному:

1. тип документа, определённый маршрутизатором;
2. совпадение ключевых слов шаблона с обращением;
3. нечёткое сходство названия шаблона с текстом обращения.

Если уверенного совпадения нет, сотруднику предлагается выбор из нескольких
кандидатов — это честнее, чем молча взять первый попавшийся бланк.
"""

from __future__ import annotations

import time
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select

from app.agents.state import AnswerStatus, AssistantState
from app.core.logging import get_logger
from app.core.metrics import graph_node_errors_total, graph_node_latency_seconds
from app.core.rbac import allowed_access_levels
from app.db.models import DocumentTemplate
from app.db.postgres import session_scope
from app.rag.bm25 import tokenize

logger = get_logger(__name__)

NODE_NAME = "template_matcher"

CONFIDENT_THRESHOLD = 0.6


def score_template(template: DocumentTemplate, *, question: str, document_kind: str) -> float:
    """Оценивает соответствие шаблона обращению.

    Args:
        template: Шаблон из реестра.
        question: Текст обращения.
        document_kind: Тип документа, определённый маршрутизатором.

    Returns:
        Оценка в диапазоне 0–1.
    """
    text = question.lower()
    score = 0.0

    if document_kind and template.doc_kind == document_kind:
        score += 0.55

    keywords = [str(item).lower() for item in (template.keywords or [])]
    matched = sum(1 for keyword in keywords if keyword in text)
    if keywords and matched:
        score += 0.3 * min(matched / len(keywords) * 2, 1.0)

    name_similarity = SequenceMatcher(None, template.name.lower(), text).ratio()
    score += 0.15 * name_similarity

    name_tokens = set(tokenize(template.name))
    question_tokens = set(tokenize(question))
    if name_tokens & question_tokens:
        score += 0.1

    return min(score, 1.0)


async def find_templates(
    *, question: str, document_kind: str, user_role: str, limit: int = 4
) -> list[tuple[DocumentTemplate, float]]:
    """Подбирает шаблоны, подходящие обращению.

    Args:
        question: Текст обращения.
        document_kind: Тип документа.
        user_role: Роль пользователя (для фильтрации по грифу).
        limit: Сколько кандидатов вернуть.

    Returns:
        Пары ``(шаблон, оценка)``, отсортированные по убыванию оценки.
    """
    levels = allowed_access_levels(user_role)

    async with session_scope() as session:
        result = await session.scalars(
            select(DocumentTemplate).where(
                DocumentTemplate.is_active.is_(True),
                DocumentTemplate.access_level.in_(levels),
            )
        )
        templates = list(result)

    if not templates:
        return []

    scored = [
        (template, score_template(template, question=question, document_kind=document_kind))
        for template in templates
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [item for item in scored[:limit] if item[1] > 0.1]


def template_to_payload(template: DocumentTemplate, score: float = 0.0) -> dict[str, Any]:
    """Сериализует шаблон для передачи на фронтенд.

    Args:
        template: Шаблон из реестра.
        score: Оценка соответствия.

    Returns:
        Словарь с описанием шаблона.
    """
    return {
        "code": template.code,
        "name": template.name,
        "description": template.description,
        "doc_kind": template.doc_kind,
        "fields": list(template.fields_schema or []),
        "required_approvals": list(template.required_approvals or []),
        "related_clauses": list(template.related_clauses or []),
        "match_score": round(score, 3),
        "match_percent": max(0, min(100, round(score * 100))),
    }


def format_template_choice(candidates: list[dict[str, Any]]) -> str:
    """Формирует сообщение с выбором подходящего шаблона.

    Args:
        candidates: Кандидаты-шаблоны.

    Returns:
        Текст сообщения для сотрудника.
    """
    lines = ["Нашёл несколько подходящих бланков. Какой из них нужен?", ""]
    for index, item in enumerate(candidates, start=1):
        lines.append(f"{index}. **{item['name']}** — {item['description'] or 'типовой бланк'}")
        if item.get("related_clauses"):
            lines.append(f"   _Требования: {', '.join(item['related_clauses'])}_")
    lines.append("")
    lines.append("Назовите номер или название — и я начну заполнение.")
    return "\n".join(lines)


def format_approval_route(approvals: list[dict[str, Any]]) -> str:
    """Форматирует маршрут согласования документа.

    Args:
        approvals: Состав согласующих подразделений.

    Returns:
        Текст с порядком и сроками согласования.
    """
    if not approvals:
        return ""

    ordered = sorted(approvals, key=lambda item: item.get("order", 0))
    lines = ["", "**Маршрут согласования:**"]
    total_days = 0
    for item in ordered:
        days = int(item.get("term_days", 0))
        total_days += days
        term = f" — до {days} раб. дн." if days else ""
        lines.append(f"{item.get('order', '•')}. {item.get('department', '')}{term}")
    if total_days:
        lines.append(f"\nОбщий срок согласования: **до {total_days} рабочих дней**.")
    return "\n".join(lines)


async def match_template_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: подбирает шаблон документа.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с выбранным шаблоном либо списком кандидатов.
    """
    started = time.perf_counter()

    try:
        if state.get("template_code"):
            async with session_scope() as session:
                template = await session.scalar(
                    select(DocumentTemplate).where(
                        DocumentTemplate.code == state["template_code"]
                    )
                )
            if template is not None:
                return {
                    "template_code": template.code,
                    "template_name": template.name,
                    "template_fields": list(template.fields_schema or []),
                    "approval_route": list(template.required_approvals or []),
                    "node_trace": [f"{NODE_NAME}:resumed"],
                }

        candidates = await find_templates(
            question=state.get("question", ""),
            document_kind=state.get("document_kind", ""),
            user_role=state.get("user_role", "employee"),
        )

        if not candidates:
            logger.info("template_not_found", document_kind=state.get("document_kind", ""))
            return {
                "answer": (
                    "В реестре нет подходящего бланка для этого документа. "
                    "Уточните тип документа или обратитесь в Центр документационного "
                    "обеспечения — возможно, бланк ещё не загружен в систему."
                ),
                "answer_status": AnswerStatus.NO_CONTEXT.value,
                "template_candidates": [],
                "node_trace": [f"{NODE_NAME}:not_found"],
            }

        best_template, best_score = candidates[0]

        if best_score >= CONFIDENT_THRESHOLD:
            logger.info(
                "template_matched",
                template_code=best_template.code,
                score=round(best_score, 3),
            )
            return {
                "template_code": best_template.code,
                "template_name": best_template.name,
                "template_fields": list(best_template.fields_schema or []),
                "approval_route": list(best_template.required_approvals or []),
                "template_candidates": [template_to_payload(best_template, best_score)],
                "node_trace": [f"{NODE_NAME}:matched"],
            }

        payloads = [template_to_payload(item, score) for item, score in candidates]
        logger.info("template_ambiguous", candidates=len(payloads))
        return {
            "answer": format_template_choice(payloads),
            "answer_status": AnswerStatus.CLARIFICATION.value,
            "template_candidates": payloads,
            "clarifying_questions": [
                {
                    "question": "Какой бланк использовать?",
                    "field": "template_code",
                    "options": [item["name"] for item in payloads],
                }
            ],
            "node_trace": [f"{NODE_NAME}:ambiguous"],
        }

    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("template_matcher_failed")
        return {
            "answer": "Не удалось обратиться к реестру шаблонов. Повторите запрос.",
            "answer_status": AnswerStatus.ERROR.value,
            "error": str(exc),
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)
