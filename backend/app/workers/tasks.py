"""Фоновые задачи.

Каждая задача идемпотентна: повторный запуск после сбоя не создаёт дубликатов
и не ломает состояние. Это важно при ``task_acks_late=True`` — задача может
быть перезапущена, если воркер упал во время выполнения.

Статус задач отражается в PostgreSQL (поля ``status`` и ``task_id``), поэтому
фронтенд опрашивает состояние через API приложения, а не через Celery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, func, select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import kb_chunks_indexed, kb_documents_total
from app.db.models import (
    ChatSession,
    DocumentTemplate,
    DocumentVersion,
    EvalRun,
    GeneratedDocument,
    KnowledgeDocument,
    User,
    ValidationReport,
)
from app.db.postgres import session_scope
from app.db.qdrant_client import count_points
from app.services.doc_validator import get_validator
from app.services.docx_generator import get_docx_generator
from app.services.document_indexer import get_indexer
from app.services.ragas_eval import run_evaluation
from app.services.template_registry import get_template_registry
from app.workers.celery_app import celery_app, run_async

logger = get_logger(__name__)


class BaseTask(Task):
    """Базовый класс задач с логированием исхода."""

    autoretry_for = (ConnectionError, TimeoutError)
    retry_backoff = True
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = 3

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        """Логирует неудачное завершение задачи."""
        logger.error(
            "task_failed",
            task_name=self.name,
            task_id=task_id,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )

    def on_success(
        self, retval: Any, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """Логирует успешное завершение задачи."""
        logger.info("task_succeeded", task_name=self.name, task_id=task_id)


# ═══════════════════════════ ИНДЕКСАЦИЯ ═══════════════════════════
@celery_app.task(base=BaseTask, bind=True, name="app.workers.tasks.index_document")
def index_document(
    self: Task,
    *,
    document_code: str,
    file_path: str,
    uploaded_by: str | None = None,
    version_label: str = "",
    changelog: str = "",
    activate: bool = True,
) -> dict[str, Any]:
    """Индексирует файл как новую версию документа базы знаний.

    Args:
        document_code: Код документа базы знаний.
        file_path: Путь к загруженному файлу.
        uploaded_by: Идентификатор загрузившего пользователя.
        version_label: Метка редакции.
        changelog: Описание изменений.
        activate: Активировать версию после индексации.

    Returns:
        Сводка по результату индексации.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет индексацию в асинхронном контексте."""
        async with session_scope() as session:
            return await get_indexer().index_file(
                session,
                document_code=document_code,
                file_path=file_path,
                uploaded_by=uuid.UUID(uploaded_by) if uploaded_by else None,
                version_label=version_label,
                changelog=changelog,
                activate=activate,
            )

    logger.info("indexing_started", document_code=document_code, task_id=self.request.id)
    try:
        return run_async(_run())
    except SoftTimeLimitExceeded:
        logger.error("indexing_timeout", document_code=document_code)
        raise


@celery_app.task(base=BaseTask, bind=True, name="app.workers.tasks.reindex_knowledge_base")
def reindex_knowledge_base(self: Task, *, rebuild_vocabulary: bool = True) -> dict[str, Any]:
    """Переиндексирует все действующие версии документов.

    Args:
        rebuild_vocabulary: Пересобрать словарь BM25 с нуля.

    Returns:
        Сводка по переиндексированным документам.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет полную переиндексацию."""
        async with session_scope() as session:
            return await get_indexer().reindex_all(
                session, rebuild_vocabulary=rebuild_vocabulary
            )

    logger.info("reindex_started", task_id=self.request.id)
    return run_async(_run())


# ═══════════════════════════ ДОКУМЕНТЫ ═══════════════════════════
@celery_app.task(base=BaseTask, bind=True, name="app.workers.tasks.generate_document")
def generate_document(
    self: Task,
    *,
    document_id: str,
    template_code: str,
    values: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    """Формирует черновик документа по шаблону.

    Args:
        document_id: Идентификатор записи о документе.
        template_code: Код шаблона.
        values: Заполненные значения полей.
        user_id: Идентификатор пользователя.

    Returns:
        Сведения о сформированном файле.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет генерацию документа."""
        async with session_scope() as session:
            record = await session.get(GeneratedDocument, uuid.UUID(document_id))
            if record is None:
                return {"error": "Запись о документе не найдена", "document_id": document_id}

            template = await session.scalar(
                select(DocumentTemplate).where(DocumentTemplate.code == template_code)
            )
            if template is None:
                record.status = "failed"
                return {"error": "Шаблон не найден", "template_code": template_code}

            user = await session.get(User, uuid.UUID(user_id))
            registry = get_template_registry()

            try:
                path, size = await get_docx_generator().generate(
                    template_code=template.code,
                    template_name=template.name,
                    fields_schema=list(template.fields_schema or []),
                    values=values,
                    template_file=registry.resolve_file(template),
                    approvals=list(template.required_approvals or []),
                    related_clauses=[str(c) for c in (template.related_clauses or [])],
                    author_name=user.full_name if user else "",
                    author_position=user.position if user else "",
                    department=user.department if user else "",
                )
            except Exception as exc:  # noqa: BLE001
                record.status = "failed"
                logger.exception("document_generation_failed", template_code=template_code)
                return {"error": str(exc), "document_id": document_id}

            record.filename = path.name
            record.file_path = str(path)
            record.file_size = size
            record.status = "ready"
            record.filled_fields = values
            record.missing_fields = registry.missing_fields(template, values)
            record.expires_at = datetime.now(UTC) + timedelta(days=7)

            return {
                "document_id": document_id,
                "filename": path.name,
                "file_size": size,
                "status": "ready",
            }

    logger.info("document_generation_started", template_code=template_code)
    return run_async(_run())


@celery_app.task(base=BaseTask, bind=True, name="app.workers.tasks.validate_document")
def validate_document(
    self: Task,
    *,
    report_id: str,
    document_text: str,
    allowed_levels: list[str],
    doc_kind: str = "",
) -> dict[str, Any]:
    """Проверяет проект документа на соответствие требованиям инструкции.

    Args:
        report_id: Идентификатор записи отчёта.
        document_text: Текст проверяемого документа.
        allowed_levels: Грифы, доступные роли пользователя.
        doc_kind: Тип документа, если известен.

    Returns:
        Итог проверки.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет проверку документа."""
        async with session_scope() as session:
            report = await session.get(ValidationReport, uuid.UUID(report_id))
            if report is None:
                return {"error": "Отчёт не найден", "report_id": report_id}

            try:
                result = await get_validator().validate(
                    document_text=document_text,
                    allowed_levels=allowed_levels,
                    doc_kind=doc_kind,
                )
            except Exception as exc:  # noqa: BLE001
                report.verdict = "failed"
                logger.exception("validation_failed", report_id=report_id)
                return {"error": str(exc), "report_id": report_id}

            report.verdict = result.verdict
            report.score = result.score
            report.violations = [item.to_dict() for item in result.violations]
            report.recommendations = result.recommendations
            report.checked_rules = result.checked_rules
            report.detected_doc_kind = result.detected_doc_kind

            return {
                "report_id": report_id,
                "verdict": result.verdict,
                "violations_count": len(result.violations),
                "critical_count": result.critical_count,
            }

    logger.info("validation_started", report_id=report_id)
    return run_async(_run())


# ═══════════════════════════ ОЦЕНКА КАЧЕСТВА ═══════════════════════════
@celery_app.task(base=BaseTask, bind=True, name="app.workers.tasks.run_quality_eval")
def run_quality_eval(
    self: Task,
    *,
    run_id: str,
    eval_set_path: str = "",
    model_name: str = "",
    limit: int | None = None,
    skip_ragas: bool = False,
) -> dict[str, Any]:
    """Прогоняет offline-оценку качества ответов.

    Args:
        run_id: Идентификатор записи о прогоне.
        eval_set_path: Путь к эталонному набору.
        model_name: Имя оцениваемой модели.
        limit: Ограничение числа вопросов.
        skip_ragas: Пропустить метрики RAGAS.

    Returns:
        Отчёт с метриками.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет оценку качества."""
        path = eval_set_path or str(Path(settings.DATA_DIR) / "eval_set" / "golden.jsonl")

        async with session_scope() as session:
            record = await session.get(EvalRun, uuid.UUID(run_id))
            if record is None:
                return {"error": "Запись о прогоне не найдена", "run_id": run_id}

            try:
                report = await run_evaluation(
                    eval_set_path=path,
                    model_name=model_name,
                    limit=limit,
                    skip_ragas=skip_ragas,
                )
            except Exception as exc:  # noqa: BLE001
                record.status = "failed"
                record.details = {"error": str(exc)[:1000]}
                logger.exception("eval_run_failed", run_id=run_id)
                return {"error": str(exc), "run_id": run_id}

            metrics = report["metrics"]
            record.status = "completed"
            record.model_name = report["model"]
            record.eval_set = report["eval_set"]
            record.questions_total = report["questions_processed"]
            record.faithfulness = metrics.get("faithfulness")
            record.answer_relevancy = metrics.get("answer_relevancy")
            record.context_precision = metrics.get("context_precision")
            record.context_recall = metrics.get("context_recall")
            record.citation_accuracy = metrics.get("citation_hit_rate")
            record.no_answer_rate = metrics.get("no_answer_rate")
            record.duration_seconds = int(report["duration_seconds"])
            record.details = {
                "by_category": report["by_category"],
                "failed_cases": report["failed_cases"],
                "all_metrics": metrics,
            }

            return {"run_id": run_id, "status": "completed", "metrics": metrics}

    logger.info("eval_run_started", run_id=run_id)
    return run_async(_run())


# ═══════════════════════════ ОБСЛУЖИВАНИЕ ═══════════════════════════
@celery_app.task(base=BaseTask, name="app.workers.tasks.cleanup_expired_documents")
def cleanup_expired_documents() -> dict[str, Any]:
    """Удаляет просроченные черновики документов.

    Черновики содержат рабочие данные сотрудников, поэтому не хранятся
    бессрочно: по истечении срока файл удаляется с диска, а запись
    помечается как истёкшая.

    Returns:
        Количество удалённых файлов и записей.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет очистку."""
        now = datetime.now(UTC)
        removed_files = 0

        async with session_scope() as session:
            expired = await session.scalars(
                select(GeneratedDocument).where(
                    GeneratedDocument.expires_at.is_not(None),
                    GeneratedDocument.expires_at < now,
                    GeneratedDocument.status != "expired",
                )
            )
            records = list(expired)

            for record in records:
                if record.file_path:
                    try:
                        Path(record.file_path).unlink(missing_ok=True)
                        removed_files += 1
                    except OSError as exc:
                        logger.warning("file_cleanup_failed", error=str(exc))
                record.status = "expired"
                record.file_path = ""

            cutoff = now - timedelta(days=30)
            old_reports = await session.execute(
                delete(ValidationReport).where(ValidationReport.created_at < cutoff)
            )

        result = {
            "expired_documents": len(records),
            "removed_files": removed_files,
            "removed_reports": old_reports.rowcount or 0,
        }
        logger.info("cleanup_completed", **result)
        return result

    return run_async(_run())


@celery_app.task(base=BaseTask, name="app.workers.tasks.refresh_kb_metrics")
def refresh_kb_metrics() -> dict[str, Any]:
    """Обновляет метрики состояния базы знаний.

    Gauge-метрики не меняются сами по себе, поэтому периодически
    пересчитываются по фактическому состоянию хранилищ.

    Returns:
        Текущие показатели базы знаний.
    """

    async def _run() -> dict[str, Any]:
        """Собирает показатели."""
        points = await count_points()
        kb_chunks_indexed.labels(collection=settings.QDRANT_COLLECTION).set(points)

        async with session_scope() as session:
            total_docs = await session.scalar(select(func.count(KnowledgeDocument.id))) or 0
            active_versions = (
                await session.scalar(
                    select(func.count(DocumentVersion.id)).where(
                        DocumentVersion.is_active.is_(True)
                    )
                )
                or 0
            )
            failed_versions = (
                await session.scalar(
                    select(func.count(DocumentVersion.id)).where(
                        DocumentVersion.status == "failed"
                    )
                )
                or 0
            )

        kb_documents_total.labels(status="total").set(total_docs)
        kb_documents_total.labels(status="active").set(active_versions)
        kb_documents_total.labels(status="failed").set(failed_versions)

        result = {
            "points": points,
            "documents": int(total_docs),
            "active_versions": int(active_versions),
            "failed_versions": int(failed_versions),
        }
        logger.info("kb_metrics_refreshed", **result)
        return result

    return run_async(_run())


@celery_app.task(base=BaseTask, name="app.workers.tasks.archive_stale_sessions")
def archive_stale_sessions(*, days: int = 30) -> dict[str, Any]:
    """Архивирует давно неактивные диалоги.

    Args:
        days: Через сколько дней бездействия архивировать диалог.

    Returns:
        Количество заархивированных диалогов.
    """

    async def _run() -> dict[str, Any]:
        """Выполняет архивацию."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with session_scope() as session:
            result = await session.execute(
                update(ChatSession)
                .where(
                    ChatSession.is_archived.is_(False),
                    ChatSession.last_message_at.is_not(None),
                    ChatSession.last_message_at < cutoff,
                )
                .values(is_archived=True)
            )
        archived = result.rowcount or 0
        logger.info("sessions_archived", count=archived, days=days)
        return {"archived": archived}

    return run_async(_run())
