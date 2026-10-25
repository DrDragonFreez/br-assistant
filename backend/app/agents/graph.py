"""Сборка графа обработки обращений на LangGraph.

Маршрут обращения::

                        router
                          |
        +-----------+-----+------+-------------+
        v           v            v             v
   clarifier    retriever   template_match   static
                    |            |
                    v            v
                reranker    doc_builder
                    |
                    v
                generator <----+
                    |          | повторная генерация
                    v          |
                 critic -------+
                    |
                    v
                  offer  (переход от ответа к действию)

Ключевые решения:

* **Проверка достаточности контекста до генерации.** Если лучший фрагмент
  не дотягивает до порога, модель не вызывается вовсе — вместо этого сразу
  выдаётся честный отказ. Это и быстрее, и надёжнее, чем надеяться на промпт.
* **Цикл критика ограничен.** Одна повторная попытка, дальше — отказ.
  Бесконечная саморефлексия съела бы время ответа.
* **Ошибки узлов не роняют граф.** Каждый узел ловит исключения и возвращает
  состояние с признаком ошибки.
* **Ответ заканчивается действием.** Узел ``offer`` дополняет разъяснение
  подходящим модулем, перечнем визирующих подразделений и предложением
  подготовить документ. Отдельным узлом, а не внутри генератора: текст
  добавляется детерминированно и проверять его критиком незачем —
  данные берутся из реестра шаблонов, выдумать там нечего.
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.clarifier import clarify_node
from app.agents.critic import critic_node, should_regenerate
from app.agents.doc_builder import build_document_node
from app.agents.router import route_node, wants_document_template
from app.agents.state import AnswerStatus, AssistantState, Intent, create_initial_state
from app.agents.template_matcher import (
    find_templates,
    match_template_node,
    template_to_payload,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import (
    graph_node_errors_total,
    graph_node_latency_seconds,
    rag_citations_per_answer,
    rag_no_answer_total,
    rag_queries_total,
    rag_top_score,
)
from app.rag.generator import get_generator
from app.rag.prompts import (
    NO_ANSWER_MARKER,
    NO_ANSWER_MESSAGE,
    OUT_OF_SCOPE_RESPONSE,
    SMALLTALK_RESPONSE,
    build_answer_prompt,
)
from app.rag.reranker import get_reranker
from app.rag.retriever import get_retriever

logger = get_logger(__name__)


# ═══════════════════════ УЗЛЫ ПОИСКА И ГЕНЕРАЦИИ ═══════════════════════
async def retrieve_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: ищет релевантные положения инструкции.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с найденными фрагментами.
    """
    started = time.perf_counter()
    try:
        chunks = await get_retriever().search_with_clause_boost(
            state.get("question", ""),
            allowed_levels=state.get("allowed_levels", ["public"]),
        )
        return {
            "retrieved_chunks": chunks,
            "retrieval_mode": chunks[0].retrieval_source if chunks else "none",
            "node_trace": [f"retriever:{len(chunks)}"],
        }
    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node="retriever", error_type=type(exc).__name__).inc()
        logger.exception("retrieve_node_failed")
        return {
            "retrieved_chunks": [],
            "error": str(exc),
            "node_trace": ["retriever:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node="retriever").observe(
            time.perf_counter() - started
        )


async def rerank_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: переранжирует найденные фрагменты.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с отобранными фрагментами и лучшей оценкой.
    """
    started = time.perf_counter()
    candidates = state.get("retrieved_chunks", [])

    if not candidates:
        return {"reranked_chunks": [], "top_score": 0.0, "node_trace": ["reranker:empty"]}

    try:
        reranked = await get_reranker().rerank(state.get("question", ""), candidates)
        top_score = reranked[0].score if reranked else 0.0
        rag_top_score.observe(top_score)
        return {
            "reranked_chunks": reranked,
            "top_score": top_score,
            "node_trace": [f"reranker:{len(reranked)}"],
        }
    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node="reranker", error_type=type(exc).__name__).inc()
        logger.exception("rerank_node_failed")
        # Деградация: работаем с исходной выдачей поиска.
        fallback = candidates[: settings.RERANKER_TOP_N]
        return {
            "reranked_chunks": fallback,
            "top_score": fallback[0].score if fallback else 0.0,
            "node_trace": ["reranker:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node="reranker").observe(time.perf_counter() - started)


def _build_citations(chunks: list[Any], cited_clauses: list[str]) -> list[dict[str, Any]]:
    """Собирает список цитат для ответа.

    Приоритет отдаётся фрагментам, на которые модель действительно сослалась;
    остальные добавляются как дополнительные источники.

    Args:
        chunks: Фрагменты, переданные модели.
        cited_clauses: Пункты, упомянутые в тексте ответа.

    Returns:
        Список цитат, упорядоченный по релевантности.
    """
    cited: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []

    for chunk in chunks:
        payload = chunk.to_payload()
        address = chunk.clause_number or (
            f"Приложение {chunk.annex_number}" if chunk.annex_number else ""
        )
        if address and address in cited_clauses:
            cited.append(payload)
        else:
            others.append(payload)

    ordered = [*cited, *others]
    for rank, item in enumerate(ordered, start=1):
        item["rank"] = rank
    return ordered


async def generate_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: формирует ответ на основе найденных положений.

    Перед обращением к модели проверяется достаточность контекста: если
    релевантность ниже порога, ответ не генерируется вовсе.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с текстом ответа и цитатами.
    """
    started = time.perf_counter()
    chunks = state.get("reranked_chunks", [])
    top_score = state.get("top_score", 0.0)

    try:
        if not chunks:
            rag_no_answer_total.labels(reason="no_chunks").inc()
            logger.info("no_answer_returned", reason="no_chunks")
            return {
                "answer": NO_ANSWER_MESSAGE,
                "answer_status": AnswerStatus.NO_CONTEXT.value,
                "citations": [],
                "node_trace": ["generator:no_chunks"],
            }

        if top_score < settings.ANSWER_MIN_RELEVANCE:
            rag_no_answer_total.labels(reason="low_relevance").inc()
            logger.info(
                "no_answer_returned",
                reason="low_relevance",
                top_score=round(top_score, 4),
                threshold=settings.ANSWER_MIN_RELEVANCE,
            )
            return {
                "answer": NO_ANSWER_MESSAGE,
                "answer_status": AnswerStatus.NO_CONTEXT.value,
                "citations": [],
                "node_trace": ["generator:low_relevance"],
            }

        system, user = build_answer_prompt(
            state.get("question", ""), chunks, state.get("history")
        )

        regenerations = state.get("regeneration_count", 0)
        if regenerations > 0:
            issues: list[str] = []
            if state.get("invalid_citations"):
                issues.append(
                    "в предыдущей версии были ссылки на отсутствующие пункты: "
                    + ", ".join(state["invalid_citations"])
                )
            if state.get("unsupported_claims"):
                issues.append(
                    "не подтверждены утверждения: "
                    + "; ".join(state["unsupported_claims"][:3])
                )
            if state.get("critic_comment"):
                issues.append(state["critic_comment"])
            if issues:
                user += (
                    "\n\nВАЖНО — исправь замечания предыдущей версии ответа:\n"
                    + "\n".join(f"- {item}" for item in issues)
                    + f"\nЕсли подтвердить утверждения фрагментами невозможно — "
                    f"верни {NO_ANSWER_MARKER}."
                )

        result = await get_generator().generate(system, user, stage="answer")

        if result.is_no_answer:
            rag_no_answer_total.labels(reason="model_declined").inc()
            logger.info("no_answer_returned", reason="model_declined")
            return {
                "answer": NO_ANSWER_MESSAGE,
                "answer_status": AnswerStatus.NO_CONTEXT.value,
                "citations": [],
                "model_name": result.model,
                "node_trace": ["generator:declined"],
            }

        citations = _build_citations(chunks, result.cited_clauses)
        rag_citations_per_answer.observe(len(result.cited_clauses))

        return {
            "answer": result.text,
            "answer_status": AnswerStatus.ANSWERED.value,
            "citations": citations,
            "model_name": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "used_fallback": result.used_fallback,
            "node_trace": [f"generator:{len(result.cited_clauses)}cit"],
        }

    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node="generator", error_type=type(exc).__name__).inc()
        logger.exception("generate_node_failed")
        return {
            "answer": "Модель временно недоступна. Повторите запрос через несколько секунд.",
            "answer_status": AnswerStatus.ERROR.value,
            "error": str(exc),
            "node_trace": ["generator:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node="generator").observe(
            time.perf_counter() - started
        )


async def static_response_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: отвечает на приветствия и вопросы вне компетенции.

    Такие обращения не требуют ни поиска, ни модели — это экономит
    секунды ответа и исключает выдумывание.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с готовым текстом.
    """
    intent = state.get("intent", "")
    answer = (
        OUT_OF_SCOPE_RESPONSE if intent == Intent.OUT_OF_SCOPE.value else SMALLTALK_RESPONSE
    )
    return {
        "answer": answer,
        "answer_status": AnswerStatus.INFO.value,
        "citations": [],
        "node_trace": [f"static:{intent}"],
    }


# Приглашение загрузить документ на проверку. Проверка выполняется по
# файлу, поэтому без загрузки помощник может только объяснить порядок.
UPLOAD_INVITATION = (
    "Загрузите проект документа на вкладке «Проверка» справа — сверю его с "
    "требованиями инструкции и с использованным модулем, покажу незаполненные "
    "поля, оставшиеся элементы шаблона и ошибки оформления.\n\n"
    "Пока — порядок дальнейших действий по инструкции:"
)


def format_approval_route(route: list[dict[str, Any]]) -> str:
    """Формирует перечень обязательного визирования.

    Строки берутся из матрицы обязательного визирования: порядок,
    подразделение и срок рассмотрения. Срок выводится рядом с
    подразделением — по нему сотрудник планирует согласование.

    Строки без названия подразделения пропускаются: пункт с пустым
    подразделением хуже короткого перечня.

    Args:
        route: Строки матрицы визирования из реестра шаблонов.

    Returns:
        Текст перечня либо пустая строка.
    """
    rows = [item for item in route if isinstance(item, dict)]
    rows.sort(key=lambda item: item.get("order") or 0)

    lines: list[str] = []
    for position, item in enumerate(rows, start=1):
        unit = str(item.get("department") or item.get("unit") or item.get("name") or "").strip()
        if not unit:
            continue
        line = f"{position}. {unit}"
        term = item.get("term_days")
        if term:
            line += f" — {term} раб. дн."
        lines.append(line)

    if not lines:
        return ""
    return (
        "Обязательное визирование по матрице — "
        f"{len(lines)} подразделения:\n" + "\n".join(lines)
    )


async def offer_action_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: дополняет разъяснение переходом к действию.

    Сотруднику, который готовит документ, нужен не только порядок, но и
    бланк. Подходящий модуль и перечень визирующих подразделений берутся
    из реестра шаблонов, поэтому выдумать их невозможно: это данные, а
    не генерация.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с дополненным ответом и списком модулей.
    """
    started = time.perf_counter()
    answer = state.get("answer", "")
    intent = state.get("intent", "")

    try:
        if intent == Intent.DOCUMENT_CHECK.value:
            return {
                "answer": f"{UPLOAD_INVITATION}\n\n{answer}".strip(),
                "node_trace": ["offer:upload"],
            }

        document_kind = state.get("document_kind", "")
        candidates = await find_templates(
            question=state.get("question", ""),
            document_kind=document_kind,
            user_role=state.get("user_role", "employee"),
        )
        # Вид подобранного модуля должен совпадать с видом документа из
        # обращения. Подбор возвращает лучший по оценке шаблон из всего
        # реестра, и без этой сверки на вопрос о письме предлагался
        # модуль приказа — предложение не по делу хуже его отсутствия.
        candidates = [
            item for item in candidates if item[0].doc_kind == document_kind
        ]
        if not candidates:
            logger.info("offer_template_kind_mismatch", document_kind=document_kind)
            return {"node_trace": ["offer:no_template"]}

        best_template, best_score = candidates[0]
        payload = template_to_payload(best_template, best_score)

        parts = [
            answer.rstrip(),
            "",
            f"Для такого документа в реестре есть модульный документ — "
            f"«{best_template.name}». Он содержит обязательные элементы, "
            f"структуру заново набирать не нужно.",
        ]

        route = format_approval_route(payload["required_approvals"])
        if route:
            parts.extend(["", route])

        parts.extend(
            [
                "",
                "Заполните модуль самостоятельно — кнопка «Заполнить» ниже, — либо "
                "подготовлю проект вместе с вами: задам необходимые вопросы и "
                "соберу готовый файл. Как удобнее?",
            ]
        )

        logger.info(
            "offer_template_attached",
            template_code=best_template.code,
            score=round(best_score, 3),
        )
        return {
            "answer": "\n".join(parts).strip(),
            "template_candidates": [payload],
            "approval_route": payload["required_approvals"],
            "node_trace": ["offer:template"],
        }
    except Exception as exc:  # noqa: BLE001
        # Разъяснение уже готово и ценно само по себе: сбой подбора
        # бланка не должен отменять ответ.
        logger.warning("offer_action_failed", error=str(exc))
        graph_node_errors_total.labels(node="offer").inc()
        return {"node_trace": ["offer:error"]}
    finally:
        graph_node_latency_seconds.labels(node="offer").observe(time.perf_counter() - started)


async def regenerate_counter_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: увеличивает счётчик повторных генераций.

    Вынесен отдельно, чтобы цикл критика был явно виден в структуре графа.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление счётчика.
    """
    return {
        "regeneration_count": state.get("regeneration_count", 0) + 1,
        "node_trace": ["regenerate"],
    }


# ═══════════════════════════ МАРШРУТИЗАЦИЯ ═══════════════════════════
def route_after_router(state: AssistantState) -> str:
    """Выбирает ветку после классификации обращения.

    Args:
        state: Состояние графа.

    Returns:
        Имя следующего узла.
    """
    if state.get("needs_clarification"):
        return "clarify"

    intent = state.get("intent", Intent.QUESTION.value)
    if intent in {Intent.SMALLTALK.value, Intent.OUT_OF_SCOPE.value}:
        return "static"
    if intent == Intent.TEMPLATE_REQUEST.value:
        return "match_template"
    return "retrieve"


def route_after_template_match(state: AssistantState) -> str:
    """Выбирает ветку после подбора шаблона.

    Args:
        state: Состояние графа.

    Returns:
        ``build_document`` либо ``__end__``.
    """
    if state.get("answer_status") in {
        AnswerStatus.CLARIFICATION.value,
        AnswerStatus.NO_CONTEXT.value,
        AnswerStatus.ERROR.value,
    }:
        return END
    return "build_document"


def needs_action_offer(state: AssistantState) -> bool:
    """Проверяет, нужно ли дополнять ответ переходом к действию.

    Дополнение уместно только при состоявшемся ответе: к честному отказу
    «в базе знаний нет информации» предлагать бланк нельзя — вышло бы,
    что помощник отказал и тут же начал что-то предлагать.

    Args:
        state: Состояние графа.

    Returns:
        True, если ответ нужно дополнить.
    """
    if state.get("answer_status") != AnswerStatus.ANSWERED.value:
        return False
    if state.get("template_candidates"):
        return False

    intent = state.get("intent", "")
    if intent == Intent.DOCUMENT_CHECK.value:
        return True
    if intent not in {Intent.QUESTION.value, Intent.APPROVAL_ROUTE.value}:
        return False
    return wants_document_template(state.get("question", ""), state.get("document_kind", ""))


def route_after_critic(state: AssistantState) -> str:
    """Выбирает ветку после проверки ответа.

    Args:
        state: Состояние графа.

    Returns:
        ``regenerate``, ``offer`` либо ``__end__``.
    """
    if should_regenerate(state):
        return "regenerate"
    return "offer" if needs_action_offer(state) else END


# ═══════════════════════════ СБОРКА ГРАФА ═══════════════════════════
def build_graph() -> Any:
    """Собирает и компилирует граф обработки обращений.

    Returns:
        Скомпилированный граф LangGraph.
    """
    graph = StateGraph(AssistantState)

    graph.add_node("route", route_node)
    graph.add_node("clarify", clarify_node)
    graph.add_node("static", static_response_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("rerank", rerank_node)
    graph.add_node("generate", generate_node)
    graph.add_node("critic", critic_node)
    graph.add_node("regenerate", regenerate_counter_node)
    graph.add_node("match_template", match_template_node)
    graph.add_node("offer", offer_action_node)
    graph.add_node("build_document", build_document_node)

    graph.add_edge(START, "route")

    graph.add_conditional_edges(
        "route",
        route_after_router,
        {
            "clarify": "clarify",
            "static": "static",
            "retrieve": "retrieve",
            "match_template": "match_template",
        },
    )

    graph.add_edge("clarify", END)
    graph.add_edge("static", END)

    graph.add_edge("retrieve", "rerank")
    graph.add_edge("rerank", "generate")
    graph.add_edge("generate", "critic")

    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {"regenerate": "regenerate", "offer": "offer", END: END},
    )
    graph.add_edge("regenerate", "generate")
    graph.add_edge("offer", END)

    graph.add_conditional_edges(
        "match_template",
        route_after_template_match,
        {"build_document": "build_document", END: END},
    )
    graph.add_edge("build_document", END)

    compiled = graph.compile()
    logger.info("graph_compiled", nodes=10)
    return compiled


_graph: Any | None = None


def get_graph() -> Any:
    """Возвращает скомпилированный граф (singleton).

    Компиляция занимает время, поэтому выполняется один раз на процесс.

    Returns:
        Скомпилированный граф.
    """
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


async def run_assistant(
    *,
    question: str,
    session_id: str,
    user_id: str,
    user_role: str,
    allowed_levels: list[str],
    history: list[dict[str, str]] | None = None,
    request_id: str = "",
    attached_document_text: str = "",
    filled_fields: dict[str, Any] | None = None,
    template_code: str = "",
) -> AssistantState:
    """Обрабатывает обращение сотрудника через граф.

    Args:
        question: Текст обращения.
        session_id: Идентификатор диалога.
        user_id: Идентификатор пользователя.
        user_role: Роль пользователя.
        allowed_levels: Доступные грифы документов.
        history: История диалога.
        request_id: Сквозной идентификатор запроса.
        attached_document_text: Текст загруженного черновика.
        filled_fields: Уже собранные поля шаблона.
        template_code: Код выбранного шаблона.

    Returns:
        Итоговое состояние с ответом, цитатами и трассой прохождения узлов.
    """
    started = time.perf_counter()

    initial = create_initial_state(
        question=question,
        session_id=session_id,
        user_id=user_id,
        user_role=user_role,
        allowed_levels=allowed_levels,
        history=history,
        request_id=request_id,
        attached_document_text=attached_document_text,
        filled_fields=filled_fields,
        template_code=template_code,
    )

    try:
        final_state: AssistantState = await get_graph().ainvoke(initial)
    except Exception as exc:  # noqa:
        logger.exception("graph_execution_failed")
        final_state = {
            **initial,
            "answer": "Произошла ошибка при обработке обращения. Повторите запрос.",
            "answer_status": AnswerStatus.ERROR.value,
            "error": str(exc),
        }

    final_state["latency_ms"] = int((time.perf_counter() - started) * 1000)

    rag_queries_total.labels(
        intent=final_state.get("intent", "unknown") or "unknown",
        role=user_role,
    ).inc()

    logger.info(
        "assistant_completed",
        intent=final_state.get("intent", ""),
        status=final_state.get("answer_status", ""),
        citations=len(final_state.get("citations", [])),
        verdict=final_state.get("critic_verdict", ""),
        latency_ms=final_state["latency_ms"],
        trace=final_state.get("node_trace", []),
    )
    return final_state
