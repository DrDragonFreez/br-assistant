"""Узел маршрутизации обращений.

Классификация выполняется в два слоя. Сначала работают быстрые правила: они
покрывают большинство типовых формулировок, не тратят время на вызов модели
и дают предсказуемый результат. Если правила не дали уверенного ответа,
подключается языковая модель в режиме структурированного вывода.

Такой порядок важен для 48-часового прототипа: правила отвечают за
воспроизводимость демонстрации, модель — за гибкость на произвольных запросах.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.agents.state import AssistantState, Intent
from app.core.logging import get_logger
from app.core.metrics import graph_node_errors_total, graph_node_latency_seconds
from app.rag.generator import get_generator
from app.rag.prompts import build_router_prompt
from app.rag.retriever import ANNEX_MENTION_RE, CLAUSE_MENTION_RE

logger = get_logger(__name__)

NODE_NAME = "router"

# ─────────────────────────── Быстрые правила ───────────────────────────
TEMPLATE_PATTERNS = (
    r"\b(шаблон|бланк|форм[ауы]|образец)\b",
    r"\b(подготов|соста|сформир|созда|сдела|напиш)\w*\s+(мне\s+)?"
    r"(проект\s+)?(служебн|приказ|письм|протокол|записк|распоряж)",
    r"\bчернови[кч]\w*\s+(документа|приказа|письма|записки)",
    r"\bзаполни\w*\s+(шаблон|бланк|форму)",
)
CHECK_PATTERNS = (
    r"\bпровер\w+\s+(мой|этот|документ|проект|чернови|приказ|письм|записк)",
    r"\b(соответству\w+|правильно\s+ли)\s+.*(требовани|инструкц|регламент)",
    r"\bчто\s+не\s+так\s+с\s+(документ|проект|приказ|письм)",
    r"\bнайди\s+ошибки",
)
APPROVAL_PATTERNS = (
    r"\b(кто|с\s+кем|какие\s+подразделени\w+)\s+(согласу|визиру|подписыва)",
    r"\b(порядок|маршрут|схем[ау]|цепочк[ау])\s+согласовани",
    r"\bсрок\w*\s+согласовани",
    r"\bматриц[ауы]\s+согласовани",
)
SMALLTALK_PATTERNS = (
    r"^\s*(привет|здравствуй|добрый\s+(день|вечер|утро)|хай|ку)\b",
    r"^\s*(спасибо|благодарю|понятно|ясно|ок|окей)[\s!.]*$",
    r"\b(что\s+ты\s+(уме|можешь)|твои\s+возможности|как\s+ты\s+работаешь|помощь|help)\b",
)
OUT_OF_SCOPE_PATTERNS = (
    r"\b(погод[ауы]|курс\s+(валют|доллара|рубля)|ставк[ауи]\s+цб|анекдот|рецепт)\b",
    r"\b(напиши\s+(код|программу|скрипт)|реши\s+(задачу|пример)|переведи\s+на\s+английский)\b",
)

DOC_KIND_PATTERNS: dict[str, str] = {
    "memo": r"\bслужебн\w*\s+записк|служебк",
    "order": r"\bприказ|распоряжени",
    "letter": r"\b(деловое\s+)?письм|исходящ\w+\s+документ",
    "protocol": r"\bпротокол|совещани",
}

VAGUE_PATTERNS = (
    r"^\s*\w{1,3}\s*$",
    r"^\s*(документ|приказ|письмо|записка|согласование|регламент|инструкция)\s*[?.]?\s*$",
)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    """Проверяет совпадение текста хотя бы с одним шаблоном.

    Args:
        text: Проверяемый текст в нижнем регистре.
        patterns: Регулярные выражения.

    Returns:
        True при первом совпадении.
    """
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def detect_document_kind(text: str) -> str:
    """Определяет тип документа, упомянутый в обращении.

    Args:
        text: Текст обращения.

    Returns:
        Код типа документа или пустая строка.
    """
    for kind, pattern in DOC_KIND_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return kind
    return ""


def classify_by_rules(question: str) -> dict[str, Any] | None:
    """Классифицирует обращение по правилам.

    Args:
        question: Текст обращения.

    Returns:
        Результат классификации либо None, если правила не сработали
        и нужна языковая модель.
    """
    text = question.strip().lower()
    if not text:
        return {
            "intent": Intent.SMALLTALK.value,
            "needs_clarification": False,
            "missing_info": [],
            "document_kind": "",
            "mentioned_clauses": [],
            "source": "rules",
        }

    document_kind = detect_document_kind(text)
    clauses = CLAUSE_MENTION_RE.findall(question)
    annexes = ANNEX_MENTION_RE.findall(question)

    if _matches_any(text, OUT_OF_SCOPE_PATTERNS):
        intent = Intent.OUT_OF_SCOPE
    elif _matches_any(text, SMALLTALK_PATTERNS) and len(text) < 60:
        intent = Intent.SMALLTALK
    elif _matches_any(text, CHECK_PATTERNS):
        intent = Intent.DOCUMENT_CHECK
    elif _matches_any(text, TEMPLATE_PATTERNS):
        intent = Intent.TEMPLATE_REQUEST
    elif _matches_any(text, APPROVAL_PATTERNS):
        intent = Intent.APPROVAL_ROUTE
    elif clauses or annexes:
        intent = Intent.QUESTION
    else:
        return None

    missing: list[str] = []
    needs_clarification = False

    if intent is Intent.TEMPLATE_REQUEST and not document_kind:
        needs_clarification = True
        missing.append("тип документа")

    if _matches_any(text, VAGUE_PATTERNS):
        needs_clarification = True
        missing.append("суть вопроса")

    return {
        "intent": intent.value,
        "needs_clarification": needs_clarification,
        "missing_info": missing,
        "document_kind": document_kind,
        "mentioned_clauses": [*clauses, *(f"Приложение {a}" for a in annexes)],
        "source": "rules",
    }


async def classify_by_llm(
    question: str, history: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Классифицирует обращение языковой моделью.

    Args:
        question: Текст обращения.
        history: История диалога.

    Returns:
        Результат классификации; при сбое — безопасное значение
        ``question``, чтобы обращение всё равно дошло до поиска.
    """
    system, user = build_router_prompt(question, history)
    try:
        payload = await get_generator().generate_json(system, user, stage="router")
    except Exception as exc:  # noqa: BLE001 — маршрутизация не должна ронять запрос
        logger.warning("router_llm_failed", error=str(exc))
        return {
            "intent": Intent.QUESTION.value,
            "needs_clarification": False,
            "missing_info": [],
            "document_kind": detect_document_kind(question),
            "mentioned_clauses": [],
            "source": "fallback",
        }

    raw_intent = str(payload.get("intent", "")).strip().lower()
    valid_intents = {item.value for item in Intent}
    intent = raw_intent if raw_intent in valid_intents else Intent.QUESTION.value

    missing_raw = payload.get("missing_info", [])
    missing = [str(item) for item in missing_raw] if isinstance(missing_raw, list) else []

    clauses_raw = payload.get("mentioned_clauses", [])
    clauses = [str(item) for item in clauses_raw] if isinstance(clauses_raw, list) else []

    return {
        "intent": intent,
        "needs_clarification": bool(payload.get("needs_clarification", False)),
        "missing_info": missing,
        "document_kind": str(payload.get("document_kind", "")) or detect_document_kind(question),
        "mentioned_clauses": clauses,
        "source": "llm",
    }


async def route_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: определяет намерение обращения.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с намерением и признаком необходимости уточнения.
    """
    started = time.perf_counter()
    question = state.get("question", "")

    try:
        classification = classify_by_rules(question)
        if classification is None:
            classification = await classify_by_llm(question, state.get("history"))

        if state.get("attached_document_text"):
            classification["intent"] = Intent.DOCUMENT_CHECK.value
            classification["needs_clarification"] = False

        if state.get("template_code") and state.get("filled_fields") is not None:
            classification["intent"] = Intent.TEMPLATE_REQUEST.value
            classification["needs_clarification"] = False

        logger.info(
            "intent_classified",
            intent=classification["intent"],
            source=classification.get("source", ""),
            needs_clarification=classification["needs_clarification"],
        )

        return {
            "intent": classification["intent"],
            "needs_clarification": classification["needs_clarification"],
            "missing_info": classification["missing_info"],
            "document_kind": classification["document_kind"],
            "mentioned_clauses": classification.get("mentioned_clauses", []),
            "node_trace": [f"{NODE_NAME}:{classification['intent']}"],
        }
    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("router_node_failed")
        return {
            "intent": Intent.QUESTION.value,
            "needs_clarification": False,
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)
