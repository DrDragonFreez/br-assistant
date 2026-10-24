"""Диалог с помощником.

Два режима ответа:

* ``POST /chat`` — обычный запрос, возвращает готовый ответ с цитатами.
  Используется, когда нужен полный результат: цитаты, вердикт критика,
  подобранные шаблоны.
* ``POST /chat/stream`` — потоковая выдача через SSE. Первые слова приходят
  за секунду вместо 20–40, что принципиально меняет восприятие при
  демонстрации. Цитаты досылаются отдельным событием после текста.

История диалога передаётся в граф, поэтому помощник учитывает контекст
предыдущих реплик — требование критерия №2.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.agents.graph import run_assistant
from app.agents.state import AnswerStatus, AssistantState
from app.api.v1.deps import (
    AccessLevels,
    RequestId,
    SessionDep,
    require,
    write_audit,
)
from app.api.v1.schemas import (
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    MessageResponse,
    SessionCreateRequest,
    SessionDetailResponse,
    SessionResponse,
)
from app.core.config import settings
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.rbac import Permission, Role
from app.db.models import (
    ChatMessage,
    ChatSession,
    DocumentChunk,
    MessageCitation,
    MessageFeedback,
    User,
)
from app.db.postgres import session_scope
from app.db.redis import cache_get, cache_set, slots_get
from app.rag.generator import extract_citations, get_generator
from app.rag.prompts import NO_ANSWER_MARKER, NO_ANSWER_MESSAGE, build_answer_prompt
from app.rag.reranker import get_reranker
from app.rag.retriever import get_retriever

logger = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["Диалог"])


def _answer_cache_key(question: str, access_levels: list[str]) -> str:
    """Строит ключ кеша ответа.

    В ключ обязательно входят доступные роли грифы: один и тот же вопрос
    от сотрудника и от методолога опирается на разный состав положений,
    и общий ключ раздал бы сотруднику ответ с закрытыми для него
    требованиями. Имя активной модели тоже учитывается — иначе сравнение
    базовой и дообученной модели читало бы ответы друг друга.

    Args:
        question: Текст обращения.
        access_levels: Грифы, доступные роли пользователя.

    Returns:
        Ключ кеша (без префикса пространства имён).
    """
    normalized = " ".join(question.lower().split())
    material = "|".join(
        [normalized, ",".join(sorted(access_levels)), settings.OLLAMA_MODEL_ACTIVE]
    )
    return f"answer:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _cacheable_state(state: AssistantState) -> dict[str, Any]:
    """Отбирает из состояния графа то, что имеет смысл кешировать.

    Фрагменты поиска и трасса узлов в кеш не кладутся: они весят больше
    самого ответа и при повторном обращении не нужны.

    Args:
        state: Итоговое состояние графа.

    Returns:
        Сериализуемый в JSON срез состояния.
    """
    keys = (
        "answer",
        "answer_status",
        "citations",
        "intent",
        "top_score",
        "critic_verdict",
        "model_name",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
    )
    return {key: state.get(key) for key in keys if state.get(key) is not None}


async def _get_session_or_404(
    db: AsyncSession, session_id: uuid.UUID, user: User
) -> ChatSession:
    """Находит диалог с проверкой владельца.

    Args:
        db: Сессия БД.
        session_id: Идентификатор диалога.
        user: Текущий пользователь.

    Returns:
        Диалог пользователя.

    Raises:
        NotFoundError: Диалог не найден.
        PermissionDeniedError: Диалог принадлежит другому пользователю.
    """
    chat_session = await db.get(ChatSession, session_id)
    if chat_session is None:
        raise NotFoundError("Диалог не найден")

    if chat_session.user_id != user.id and user.role not in {Role.AUDITOR, Role.ADMIN}:
        # Аудитор и администратор вправе просматривать чужие диалоги.
        raise PermissionDeniedError("Диалог принадлежит другому пользователю")
    return chat_session


async def _load_history(db: AsyncSession, session_id: uuid.UUID) -> list[dict[str, str]]:
    """Загружает последние реплики диалога.

    Args:
        db: Сессия БД.
        session_id: Идентификатор диалога.

    Returns:
        Список сообщений в формате, понятном графу.
    """
    result = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(settings.MAX_HISTORY_MESSAGES)
    )
    messages = list(result)[::-1]
    return [{"role": item.role, "content": item.content} for item in messages]


async def _ensure_session(
    db: AsyncSession, *, session_id: uuid.UUID | None, user: User, first_message: str
) -> ChatSession:
    """Возвращает существующий диалог или создаёт новый.

    Args:
        db: Сессия БД.
        session_id: Идентификатор диалога, если задан.
        user: Текущий пользователь.
        first_message: Первое сообщение — из него формируется заголовок.

    Returns:
        Диалог для записи сообщений.
    """
    if session_id is not None:
        return await _get_session_or_404(db, session_id, user)

    title = first_message[:60] + ("…" if len(first_message) > 60 else "")
    chat_session = ChatSession(user_id=user.id, title=title)
    db.add(chat_session)
    await db.flush()
    return chat_session


async def _persist_citations(
    db: AsyncSession, message: ChatMessage, citations: list[dict[str, Any]]
) -> None:
    """Прикрепляет цитаты к сообщению.

    Идентификаторы фрагментов приходят из payload Qdrant и совпадают с
    ``document_chunks.id``. Совпадение проверяется одним запросом: после
    переиндексации в векторном хранилище могут остаться точки, которых уже
    нет в PostgreSQL, и внешний ключ отклонил бы вставку всей цитаты.
    Текст положения при этом сохраняется в самой цитате, поэтому ответ
    остаётся проверяемым даже без ссылки на фрагмент.

    Args:
        db: Сессия БД для проверки существования фрагментов.
        message: Сообщение помощника.
        citations: Цитаты из состояния графа.
    """
    requested: dict[int, uuid.UUID] = {}
    for position, item in enumerate(citations):
        raw_chunk_id = item.get("chunk_id", "")
        if not raw_chunk_id:
            continue
        try:
            requested[position] = uuid.UUID(str(raw_chunk_id))
        except ValueError:
            continue

    known: set[uuid.UUID] = set()
    if requested:
        result = await db.scalars(
            select(DocumentChunk.id).where(DocumentChunk.id.in_(set(requested.values())))
        )
        known = set(result)
        missing = len(set(requested.values())) - len(known)
        if missing:
            logger.warning("citation_chunks_not_in_database", count=missing)

    for position, item in enumerate(citations):
        chunk_id = requested.get(position)
        if chunk_id is not None and chunk_id not in known:
            chunk_id = None

        message.citations.append(
            MessageCitation(
                chunk_id=chunk_id,
                rank=int(item.get("rank", 0)),
                document_code=str(item.get("document_code", "")),
                document_title=str(item.get("document_title", "")),
                version_label=str(item.get("version_label", "")),
                clause_number=str(item.get("clause_number", "")),
                section_title=str(item.get("section_title", "")),
                page_number=item.get("page_number"),
                quote=str(item.get("quote", ""))[:2000],
                relevance=float(item.get("relevance", 0.0)),
            )
        )


@router.post("", response_model=ChatResponse, summary="Задать вопрос помощнику")
async def ask(
    request: Request,
    payload: ChatRequest,
    db: SessionDep,
    access_levels: AccessLevels,
    request_id: RequestId,
    user: Annotated[User, Depends(require(Permission.CHAT_ASK))],
) -> ChatResponse:
    """Обрабатывает обращение сотрудника.

    Args:
        request: HTTP-запрос.
        payload: Текст обращения и контекст диалога.
        db: Сессия БД.
        access_levels: Грифы, доступные роли пользователя.
        request_id: Сквозной идентификатор запроса.
        user: Текущий пользователь.

    Returns:
        Ответ помощника со ссылками на положения инструкции.
    """
    chat_session = await _ensure_session(
        db, session_id=payload.session_id, user=user, first_message=payload.message
    )
    history = await _load_history(db, chat_session.id)

    user_message = ChatMessage(
        session_id=chat_session.id,
        role="user",
        content=payload.message,
        request_id=request_id,
    )
    db.add(user_message)
    await db.flush()

    stored_slots = await slots_get(str(chat_session.id))

    # Кеш отвечает только на самостоятельные вопросы: заполнение бланка,
    # уточнения и ответы с опорой на историю диалога зависят от состояния,
    # и переиспользовать их нельзя.
    cache_key = (
        _answer_cache_key(payload.message, access_levels)
        if not payload.template_code and not stored_slots and not history
        else ""
    )
    state: AssistantState | None = None
    from_cache = False

    if cache_key:
        cached = await cache_get(cache_key)
        if cached:
            state = cast(AssistantState, cached)
            from_cache = True
            logger.info("answer_served_from_cache", role=user.role.value)

    if state is None:
        state = await run_assistant(
            question=payload.message,
            session_id=str(chat_session.id),
            user_id=str(user.id),
            user_role=user.role.value,
            allowed_levels=access_levels,
            history=history,
            request_id=request_id,
            filled_fields=stored_slots,
            template_code=payload.template_code,
        )

        # В кеш попадают только состоявшиеся ответы: отказы, уточнения
        # и ошибки дешевле пересчитать, чем объяснять, почему помощник
        # повторяет отказ после загрузки новой редакции инструкции.
        if cache_key and state.get("answer_status") == AnswerStatus.ANSWERED.value:
            await cache_set(cache_key, _cacheable_state(state))

    assistant_message = ChatMessage(
        session_id=chat_session.id,
        role="assistant",
        content=state.get("answer", ""),
        intent=state.get("intent", ""),
        answer_status=state.get("answer_status", ""),
        model_name=state.get("model_name", ""),
        top_score=state.get("top_score"),
        critic_verdict=state.get("critic_verdict", ""),
        latency_ms=int(state.get("latency_ms", 0)),
        prompt_tokens=int(state.get("prompt_tokens", 0)),
        completion_tokens=int(state.get("completion_tokens", 0)),
        request_id=request_id,
        extra={
            "node_trace": state.get("node_trace", []),
            "used_fallback": state.get("used_fallback", False),
            "template_code": state.get("template_code", ""),
            # Признак нужен при разборе метрик: ответ из кеша не проходил
            # ни поиск, ни модель, и его задержку нельзя смешивать
            # с задержкой полного контура.
            "from_cache": from_cache,
        },
    )
    await _persist_citations(db, assistant_message, state.get("citations", []))
    db.add(assistant_message)

    chat_session.messages_count += 2
    chat_session.last_message_at = datetime.now(UTC)
    await db.flush()

    await write_audit(
        db,
        user=user,
        action="chat.ask",
        resource_type="chat_session",
        resource_id=str(chat_session.id),
        request=request,
        details={
            "intent": state.get("intent", ""),
            "status": state.get("answer_status", ""),
            "citations": len(state.get("citations", [])),
        },
    )

    top_score = float(state.get("top_score", 0.0))

    return ChatResponse(
        message_id=assistant_message.id,
        session_id=chat_session.id,
        answer=state.get("answer", ""),
        answer_status=state.get("answer_status", ""),
        citations=state.get("citations", []),  # type: ignore[arg-type]
        clarifying_questions=state.get("clarifying_questions", []),  # type: ignore[arg-type]
        template_candidates=state.get("template_candidates", []),  # type: ignore[arg-type]
        intent=state.get("intent", ""),
        top_relevance_percent=max(0, min(100, round(top_score * 100))),
        critic_verdict=state.get("critic_verdict", ""),
        model_name=state.get("model_name", ""),
        latency_ms=int(state.get("latency_ms", 0)),
        from_cache=from_cache,
        filled_fields=state.get("filled_fields", {}),
        missing_fields=state.get("missing_fields", []),
    )


@router.post("/stream", summary="Задать вопрос с потоковой выдачей ответа")
async def ask_stream(
    request: Request,
    payload: ChatRequest,
    db: SessionDep,
    access_levels: AccessLevels,
    request_id: RequestId,
    user: Annotated[User, Depends(require(Permission.CHAT_ASK))],
) -> EventSourceResponse:
    """Обрабатывает обращение с потоковой выдачей текста.

    Поиск и реранжирование выполняются до начала потока, поэтому цитаты
    известны заранее и отправляются первым событием: интерфейс показывает
    источники уже во время генерации текста.

    Args:
        request: HTTP-запрос.
        payload: Текст обращения.
        db: Сессия БД.
        access_levels: Доступные грифы.
        request_id: Идентификатор запроса.
        user: Текущий пользователь.

    Returns:
        Поток событий SSE.
    """
    chat_session = await _ensure_session(
        db, session_id=payload.session_id, user=user, first_message=payload.message
    )
    history = await _load_history(db, chat_session.id)

    user_message = ChatMessage(
        session_id=chat_session.id,
        role="user",
        content=payload.message,
        request_id=request_id,
    )
    db.add(user_message)
    await db.flush()
    session_id = chat_session.id

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        """Формирует поток событий SSE.

        Yields:
            События ``meta``, ``citations``, ``token``, ``done`` или ``error``.
        """
        collected: list[str] = []
        citations: list[dict[str, Any]] = []
        status_value = AnswerStatus.ANSWERED.value

        try:
            yield {"event": "meta", "data": json.dumps({"session_id": str(session_id)})}

            chunks = await get_retriever().search_with_clause_boost(
                payload.message, allowed_levels=access_levels
            )
            reranked = await get_reranker().rerank(payload.message, chunks)
            top_score = reranked[0].score if reranked else 0.0

            if not reranked or top_score < settings.ANSWER_MIN_RELEVANCE:
                status_value = AnswerStatus.NO_CONTEXT.value
                collected.append(NO_ANSWER_MESSAGE)
                yield {"event": "token", "data": json.dumps({"text": NO_ANSWER_MESSAGE})}
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {"status": status_value, "citations": [], "relevance_percent": 0}
                    ),
                }
                return

            citations = [chunk.to_payload() for chunk in reranked]
            for rank, item in enumerate(citations, start=1):
                item["rank"] = rank

            yield {"event": "citations", "data": json.dumps(citations, ensure_ascii=False)}

            system, user_prompt = build_answer_prompt(payload.message, reranked, history)
            async for piece in get_generator().stream(system, user_prompt):
                collected.append(piece)
                yield {"event": "token", "data": json.dumps({"text": piece})}

            answer_text = "".join(collected)

            if NO_ANSWER_MARKER in answer_text:
                status_value = AnswerStatus.NO_CONTEXT.value
                answer_text = NO_ANSWER_MESSAGE
                collected = [answer_text]
                citations = []
                yield {"event": "replace", "data": json.dumps({"text": answer_text})}

            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "status": status_value,
                        "citations": citations,
                        "cited_clauses": extract_citations(answer_text),
                        "relevance_percent": max(0, min(100, round(top_score * 100))),
                    },
                    ensure_ascii=False,
                ),
            }

        except Exception:  # noqa: BLE001
            logger.exception("stream_failed")
            status_value = AnswerStatus.ERROR.value
            collected.append("Произошла ошибка при формировании ответа.")
            yield {
                "event": "error",
                "data": json.dumps(
                    {"detail": "Не удалось сформировать ответ", "code": "stream_error"}
                ),
            }

        finally:
            try:
                async with session_scope() as db_session:
                    message = ChatMessage(
                        session_id=session_id,
                        role="assistant",
                        content="".join(collected),
                        answer_status=status_value,
                        model_name=settings.OLLAMA_MODEL_ACTIVE,
                        request_id=request_id,
                        extra={"streamed": True},
                    )
                    await _persist_citations(db_session, message, citations)
                    db_session.add(message)

                    stored = await db_session.get(ChatSession, session_id)
                    if stored is not None:
                        stored.messages_count += 2
                        stored.last_message_at = datetime.now(UTC)
            except Exception as exc:  # noqa: BLE001
                logger.error("stream_persist_failed", error=str(exc))

    return EventSourceResponse(event_generator(), ping=15)


@router.get("/sessions", response_model=list[SessionResponse], summary="Список диалогов")
async def list_sessions(
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.CHAT_HISTORY_READ))],
    include_archived: Annotated[bool, Query(description="Включить архивные диалоги")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[SessionResponse]:
    """Возвращает диалоги текущего пользователя.

    Args:
        db: Сессия БД.
        user: Текущий пользователь.
        include_archived: Включать ли архивные диалоги.
        limit: Ограничение количества.

    Returns:
        Диалоги, отсортированные по дате последнего сообщения.
    """
    query = select(ChatSession).where(ChatSession.user_id == user.id)
    if not include_archived:
        query = query.where(ChatSession.is_archived.is_(False))

    result = await db.scalars(
        query.order_by(ChatSession.last_message_at.desc().nullslast()).limit(limit)
    )
    return [SessionResponse.model_validate(item) for item in result]


@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать диалог",
)
async def create_session(
    payload: SessionCreateRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.CHAT_ASK))],
) -> SessionResponse:
    """Создаёт новый диалог.

    Args:
        payload: Заголовок диалога.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Созданный диалог.
    """
    chat_session = ChatSession(user_id=user.id, title=payload.title)
    db.add(chat_session)
    await db.flush()
    return SessionResponse.model_validate(chat_session)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetailResponse,
    summary="Диалог с историей сообщений",
)
async def get_session_detail(
    session_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.CHAT_HISTORY_READ))],
) -> SessionDetailResponse:
    """Возвращает диалог вместе с сообщениями и цитатами.

    Args:
        session_id: Идентификатор диалога.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Диалог с полной историей.
    """
    chat_session = await _get_session_or_404(db, session_id, user)

    result = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .options(selectinload(ChatMessage.citations))
        .order_by(ChatMessage.created_at)
    )
    messages = [MessageResponse.model_validate(item) for item in result]

    detail = SessionDetailResponse.model_validate(chat_session)
    detail.messages = messages
    return detail


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Без явного response_model=None FastAPI принимает аннотацию
    # возврата «-> None» за модель ответа и отказывается
    # регистрировать маршрут: тело при 204 недопустимо.
    response_model=None,
    summary="Удалить диалог",
)
async def delete_session(
    request: Request,
    session_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.CHAT_HISTORY_READ))],
) -> None:
    """Удаляет диалог вместе с историей.

    Args:
        request: HTTP-запрос.
        session_id: Идентификатор диалога.
        db: Сессия БД.
        user: Текущий пользователь.

    Raises:
        PermissionDeniedError: Попытка удалить чужой диалог.
    """
    chat_session = await _get_session_or_404(db, session_id, user)
    if chat_session.user_id != user.id:
        raise PermissionDeniedError("Удалять можно только собственные диалоги")

    await db.delete(chat_session)
    await write_audit(
        db,
        user=user,
        action="chat.session_delete",
        resource_type="chat_session",
        resource_id=str(session_id),
        request=request,
    )


@router.post(
    "/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    # Без явного response_model=None FastAPI принимает аннотацию
    # возврата «-> None» за модель ответа и отказывается
    # регистрировать маршрут: тело при 204 недопустимо.
    response_model=None,
    summary="Оценить ответ помощника",
)
async def submit_feedback(
    message_id: uuid.UUID,
    payload: FeedbackRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.CHAT_ASK))],
) -> None:
    """Сохраняет обратную связь по ответу.

    Накопленные оценки используются для отбора примеров при дообучении
    и для выявления типов вопросов, на которых помощник ошибается.

    Args:
        message_id: Идентификатор сообщения.
        payload: Оценка и комментарий.
        db: Сессия БД.
        user: Текущий пользователь.

    Raises:
        NotFoundError: Сообщение не найдено.
    """
    message = await db.get(ChatMessage, message_id)
    if message is None:
        raise NotFoundError("Сообщение не найдено")

    existing = await db.scalar(
        select(MessageFeedback).where(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == user.id,
        )
    )
    if existing is not None:
        existing.is_helpful = payload.is_helpful
        existing.reason = payload.reason
        existing.comment = payload.comment
    else:
        db.add(
            MessageFeedback(
                message_id=message_id,
                user_id=user.id,
                is_helpful=payload.is_helpful,
                reason=payload.reason,
                comment=payload.comment,
            )
        )

    logger.info("feedback_submitted", is_helpful=payload.is_helpful, reason=payload.reason)
