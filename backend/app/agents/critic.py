"""Узел контроля качества ответа — основной барьер против галлюцинаций.

Проверка идёт в два этапа:

1. **Детерминированные проверки** — не требуют модели и работают всегда:
   сверка упомянутых в ответе пунктов с теми, что реально были в контексте;
   контроль наличия хотя бы одной ссылки; поиск признаков фантазирования.
   Выдуманная ссылка на пункт выявляется здесь гарантированно.
2. **Проверка моделью** — отлавливает утверждения, которых нет в источниках,
   но которые выглядят правдоподобно.

Если вердикт отрицательный, граф возвращается к генерации с более строгой
инструкцией; при повторной неудаче выдаётся честный отказ, а не сомнительный
ответ. Это прямо отвечает на вопрос защиты «как снижаете риск ошибочных ответов».
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.agents.state import AnswerStatus, AssistantState, CriticVerdict
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    critic_regenerations_total,
    critic_verdicts_total,
    graph_node_errors_total,
    graph_node_latency_seconds,
)
from app.rag.generator import extract_citations, get_generator
from app.rag.prompts import NO_ANSWER_MARKER, NO_ANSWER_MESSAGE, build_critic_prompt

logger = get_logger(__name__)

NODE_NAME = "critic"

SPECULATION_PATTERNS: tuple[str, ...] = (
    r"\bобычно\b",
    r"\bкак\s+правило\b",
    r"\bв\s+большинстве\s+(случаев|организаций)\b",
    r"\bскорее\s+всего\b",
    r"\bвероятно\b",
    r"\bпринято\s+считать\b",
    r"\bна\s+практике\s+часто\b",
    r"\bя\s+(думаю|полагаю|предполагаю)\b",
    r"\bдолжно\s+быть\b",
)


def collect_context_clauses(chunks: list[Any]) -> set[str]:
    """Собирает номера пунктов, присутствующие в контексте.

    Args:
        chunks: Фрагменты, переданные модели.

    Короткие пункты инструкции объединяются в один фрагмент, поэтому
    допустимым считается каждый номер, текст которого попал во фрагмент,
    а не только его основной адрес. Иначе корректная ссылка на положение,
    дословно присутствующее в контексте, была бы отмечена как выдуманная.

    Returns:
        Множество допустимых адресов цитирования.
    """
    allowed: set[str] = set()
    for chunk in chunks:
        if getattr(chunk, "clause_number", ""):
            allowed.add(chunk.clause_number)
        if getattr(chunk, "annex_number", ""):
            allowed.add(f"Приложение {chunk.annex_number}")

        # Chunk из чанкера несёт список в поле clause_numbers, а
        # RetrievedChunk из Qdrant — в payload, попадающем в extra.
        for number in getattr(chunk, "clause_numbers", None) or []:
            allowed.add(str(number))
        extra = getattr(chunk, "extra", None) or {}
        for number in extra.get("clauses") or []:
            allowed.add(str(number))
    return allowed


def find_invalid_citations(answer: str, chunks: list[Any]) -> list[str]:
    """Находит ссылки на пункты, отсутствующие в контексте.

    Это детерминированная проверка: ссылка на несуществующий пункт —
    однозначная ошибка, которую нельзя пропускать ни при каких условиях.

    Args:
        answer: Текст ответа.
        chunks: Фрагменты контекста.

    Returns:
        Список некорректных ссылок.
    """
    allowed = collect_context_clauses(chunks)
    if not allowed:
        return []

    invalid: list[str] = []
    for cited in extract_citations(answer):
        if cited in allowed:
            continue
        if any(item.startswith(f"{cited}.") for item in allowed):
            continue
        invalid.append(cited)
    return invalid


def find_speculation(answer: str) -> list[str]:
    """Находит обороты, указывающие на домысливание.

    Args:
        answer: Текст ответа.

    Returns:
        Список найденных оборотов.
    """
    found: list[str] = []
    for pattern in SPECULATION_PATTERNS:
        match = re.search(pattern, answer, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    return found


def run_deterministic_checks(answer: str, chunks: list[Any]) -> dict[str, Any]:
    """Выполняет проверки, не требующие языковой модели.

    Args:
        answer: Сгенерированный ответ.
        chunks: Фрагменты контекста.

    Returns:
        Словарь с результатами проверок и предварительным вердиктом.
    """
    citations = extract_citations(answer)
    invalid = find_invalid_citations(answer, chunks)
    speculation = find_speculation(answer)

    verdict = CriticVerdict.APPROVED
    reasons: list[str] = []

    if invalid:
        verdict = CriticVerdict.REJECTED
        reasons.append(f"ссылки на отсутствующие пункты: {', '.join(invalid)}")
    elif not citations and chunks:
        # Ответ по инструкции обязан опираться на конкретные положения.
        verdict = CriticVerdict.REVISED
        reasons.append("в ответе нет ни одной ссылки на пункт инструкции")
    elif speculation:
        verdict = CriticVerdict.REVISED
        reasons.append(f"признаки домысливания: {', '.join(speculation[:3])}")

    return {
        "verdict": verdict,
        "citations": citations,
        "invalid_citations": invalid,
        "speculation": speculation,
        "reasons": reasons,
    }


async def run_llm_check(question: str, answer: str, chunks: list[Any]) -> dict[str, Any]:
    """Проверяет обоснованность ответа языковой моделью.

    Args:
        question: Исходный вопрос.
        answer: Сгенерированный ответ.
        chunks: Фрагменты контекста.

    Returns:
        Результат проверки; при сбое модели — вердикт ``approved``,
        поскольку детерминированные проверки уже пройдены.
    """
    system, user = build_critic_prompt(question, answer, chunks)
    try:
        payload = await get_generator().generate_json(system, user, stage="critic")
    except Exception as exc:  # noqa: BLE001 — критик не должен ронять ответ
        logger.warning("critic_llm_failed", error=str(exc))
        return {
            "verdict": CriticVerdict.APPROVED.value,
            "grounded": True,
            "citations_valid": True,
            "unsupported_claims": [],
            "invalid_citations": [],
            "comment": "",
        }

    raw_verdict = str(payload.get("verdict", "")).strip().lower()
    valid = {item.value for item in CriticVerdict}
    verdict = raw_verdict if raw_verdict in valid else CriticVerdict.APPROVED.value

    unsupported = payload.get("unsupported_claims", [])
    invalid = payload.get("invalid_citations", [])

    return {
        "verdict": verdict,
        "grounded": bool(payload.get("grounded", True)),
        "citations_valid": bool(payload.get("citations_valid", True)),
        "unsupported_claims": (
            [str(i) for i in unsupported] if isinstance(unsupported, list) else []
        ),
        "invalid_citations": [str(i) for i in invalid] if isinstance(invalid, list) else [],
        "comment": str(payload.get("comment", "")),
    }


async def critic_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: проверяет сгенерированный ответ.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с вердиктом и, при необходимости, заменой ответа
        на честный отказ.
    """
    started = time.perf_counter()
    answer = state.get("answer", "")
    chunks = state.get("reranked_chunks", [])

    try:
        if (
            NO_ANSWER_MARKER in answer
            or state.get("answer_status") == AnswerStatus.NO_CONTEXT.value
        ):
            critic_verdicts_total.labels(verdict="no_answer_approved").inc()
            return {
                "critic_verdict": CriticVerdict.APPROVED.value,
                "node_trace": [f"{NODE_NAME}:no_answer"],
            }

        if not settings.CRITIC_ENABLED:
            critic_verdicts_total.labels(verdict=CriticVerdict.SKIPPED.value).inc()
            return {
                "critic_verdict": CriticVerdict.SKIPPED.value,
                "node_trace": [f"{NODE_NAME}:skipped"],
            }

        deterministic = run_deterministic_checks(answer, chunks)

        if deterministic["verdict"] is CriticVerdict.REJECTED:
            verdict = CriticVerdict.REJECTED.value
            comment = "; ".join(deterministic["reasons"])
            unsupported: list[str] = []
            invalid: list[str] = deterministic["invalid_citations"]
        else:
            llm_check = await run_llm_check(state.get("question", ""), answer, chunks)
            verdict = llm_check["verdict"]
            comment = llm_check["comment"] or "; ".join(deterministic["reasons"])
            unsupported = llm_check["unsupported_claims"]
            invalid = llm_check["invalid_citations"]

            if (
                deterministic["verdict"] is CriticVerdict.REVISED
                and verdict == CriticVerdict.APPROVED.value
            ):
                verdict = CriticVerdict.REVISED.value

        critic_verdicts_total.labels(verdict=verdict).inc()

        regenerations = state.get("regeneration_count", 0)
        update: dict[str, Any] = {
            "critic_verdict": verdict,
            "critic_comment": comment,
            "unsupported_claims": unsupported,
            "invalid_citations": invalid,
            "node_trace": [f"{NODE_NAME}:{verdict}"],
        }

        if (
            verdict == CriticVerdict.REJECTED.value
            and regenerations >= settings.CRITIC_MAX_REGENERATIONS
        ):
            logger.warning(
                "critic_rejected_final",
                regenerations=regenerations,
                invalid_citations=len(invalid),
            )
            update.update(
                {
                    "answer": NO_ANSWER_MESSAGE,
                    "answer_status": AnswerStatus.NO_CONTEXT.value,
                    "citations": [],
                }
            )

        logger.info(
            "critic_completed",
            verdict=verdict,
            invalid_citations=len(invalid),
            unsupported=len(unsupported),
        )
        return update

    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("critic_node_failed")
        return {
            "critic_verdict": CriticVerdict.SKIPPED.value,
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)


def should_regenerate(state: AssistantState) -> bool:
    """Определяет, нужна ли повторная генерация ответа.

    Args:
        state: Состояние графа после работы критика.

    Returns:
        True, если ответ следует перегенерировать.
    """
    verdict = state.get("critic_verdict", "")
    regenerations = state.get("regeneration_count", 0)

    if verdict not in {CriticVerdict.REJECTED.value, CriticVerdict.REVISED.value}:
        return False
    if regenerations >= settings.CRITIC_MAX_REGENERATIONS:
        return False
    if state.get("answer_status") == AnswerStatus.NO_CONTEXT.value:
        return False

    critic_regenerations_total.inc()
    logger.info("regeneration_requested", verdict=verdict, attempt=regenerations + 1)
    return True
