"""Администрирование: база знаний, версии, пользователи, оценка качества.

Эндпоинты разделены по правам, а не по ролям: методолог управляет базой
знаний и шаблонами, аудитор читает журнал, администратор работает с
учётными записями. Такое разделение позволяет добавлять роли без изменения
кода эндпоинтов — достаточно изменить матрицу прав в ``core/rbac.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.v1.deps import SessionDep, require, write_audit
from app.api.v1.health import collect_components, overall_status
from app.api.v1.schemas import (
    AuditLogResponse,
    DocumentRegisterRequest,
    DocumentVersionResponse,
    EvalRunRequest,
    EvalRunResponse,
    HealthResponse,
    KnowledgeBaseStatsResponse,
    KnowledgeDocumentResponse,
    TaskAcceptedResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.rbac import Permission
from app.core.security import hash_password, validate_password_strength
from app.db.models import (
    AuditLog,
    DocumentTemplate,
    DocumentVersion,
    EvalRun,
    KnowledgeDocument,
    User,
)
from app.db.qdrant_client import collection_stats
from app.rag.versioning import delete_version, list_versions, rollback_to_version
from app.workers.tasks import index_document, reindex_knowledge_base, run_quality_eval

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["Администрирование"])


# ═══════════════════════════ БАЗА ЗНАНИЙ ═══════════════════════════
@router.get(
    "/knowledge/stats",
    response_model=KnowledgeBaseStatsResponse,
    summary="Показатели базы знаний",
)
async def knowledge_stats(
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.KB_READ))],
) -> KnowledgeBaseStatsResponse:
    """Возвращает сводные показатели базы знаний.

    Args:
        db: Сессия БД.
        _user: Текущий пользователь (проверка прав).

    Returns:
        Состояние коллекции и количество документов.
    """
    stats = await collection_stats()

    documents_count = await db.scalar(select(func.count(KnowledgeDocument.id))) or 0
    active_versions = (
        await db.scalar(
            select(func.count(DocumentVersion.id)).where(DocumentVersion.is_active.is_(True))
        )
        or 0
    )
    failed_versions = (
        await db.scalar(
            select(func.count(DocumentVersion.id)).where(DocumentVersion.status == "failed")
        )
        or 0
    )
    templates_count = (
        await db.scalar(
            select(func.count(DocumentTemplate.id)).where(DocumentTemplate.is_active.is_(True))
        )
        or 0
    )

    return KnowledgeBaseStatsResponse(
        collection=str(stats.get("collection", settings.QDRANT_COLLECTION)),
        collection_status=str(stats.get("status", "unknown")),
        points_count=int(stats.get("points_count", 0)),
        documents_count=int(documents_count),
        active_versions=int(active_versions),
        failed_versions=int(failed_versions),
        templates_count=int(templates_count),
    )


@router.get(
    "/knowledge/documents",
    response_model=list[KnowledgeDocumentResponse],
    summary="Документы базы знаний",
)
async def list_knowledge_documents(
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.KB_READ))],
) -> list[KnowledgeDocumentResponse]:
    """Возвращает документы базы знаний вместе с историей редакций.

    Args:
        db: Сессия БД.
        _user: Текущий пользователь.

    Returns:
        Документы с перечнем версий.
    """
    result = await db.scalars(
        select(KnowledgeDocument)
        .options(selectinload(KnowledgeDocument.versions))
        .order_by(KnowledgeDocument.code)
    )
    return [KnowledgeDocumentResponse.model_validate(item) for item in result]


@router.post(
    "/knowledge/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зарегистрировать документ базы знаний",
)
async def register_document(
    request: Request,
    payload: DocumentRegisterRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.KB_VERSION_MANAGE))],
) -> KnowledgeDocumentResponse:
    """Регистрирует новый документ базы знаний.

    Подключение нового источника не требует изменения кода: документ
    регистрируется здесь, после чего в него загружаются редакции.

    Args:
        request: HTTP-запрос.
        payload: Описание документа.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Созданная запись.

    Raises:
        ConflictError: Документ с таким кодом уже зарегистрирован.
    """
    existing = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.code == payload.code)
    )
    if existing is not None:
        raise ConflictError(f"Документ с кодом «{payload.code}» уже зарегистрирован")

    document = KnowledgeDocument(**payload.model_dump())
    db.add(document)
    await db.flush()

    await write_audit(
        db,
        user=user,
        action="kb.document_register",
        resource_type="knowledge_document",
        resource_id=payload.code,
        request=request,
    )

    return KnowledgeDocumentResponse.model_validate(document)


@router.post(
    "/knowledge/upload",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Загрузить новую редакцию документа",
)
async def upload_knowledge_file(
    request: Request,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.KB_UPLOAD))],
    file: Annotated[UploadFile, File(description="Файл документа")],
    document_code: Annotated[str, Form(description="Код документа базы знаний")],
    version_label: Annotated[str, Form()] = "",
    changelog: Annotated[str, Form(description="Описание изменений")] = "",
    activate: Annotated[bool, Form(description="Сделать редакцию действующей")] = True,
) -> TaskAcceptedResponse:
    """Загружает файл как новую редакцию документа и ставит индексацию в очередь.

    Прежние редакции сохраняются: их можно просмотреть и вернуть в действие.

    Args:
        request: HTTP-запрос.
        db: Сессия БД.
        user: Текущий пользователь.
        file: Файл документа.
        document_code: Код документа базы знаний.
        version_label: Метка редакции.
        changelog: Описание изменений.
        activate: Активировать редакцию после индексации.

    Returns:
        Идентификатор задачи индексации.

    Raises:
        NotFoundError: Документ с указанным кодом не зарегистрирован.
        ValidationError: Недопустимый формат или размер файла.
    """
    document = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.code == document_code)
    )
    if document is None:
        raise NotFoundError(
            f"Документ «{document_code}» не зарегистрирован. "
            "Сначала зарегистрируйте его в базе знаний."
        )

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_upload_extensions:
        raise ValidationError(f"Формат «{suffix}» не поддерживается")

    target_dir = Path(settings.INSTRUCTIONS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{document_code}_{uuid.uuid4().hex[:8]}{suffix}"

    written = 0
    async with aiofiles.open(target, "wb") as handle:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > settings.max_upload_size_bytes:
                await handle.close()
                target.unlink(missing_ok=True)
                raise ValidationError(
                    f"Размер файла превышает {settings.MAX_UPLOAD_SIZE_MB} МБ"
                )
            await handle.write(chunk)

    task = index_document.delay(
        document_code=document_code,
        file_path=str(target),
        uploaded_by=str(user.id),
        version_label=version_label,
        changelog=changelog,
        activate=activate,
    )

    await write_audit(
        db,
        user=user,
        action="kb.upload",
        resource_type="knowledge_document",
        resource_id=document_code,
        request=request,
        details={"size_bytes": written, "activate": activate},
    )

    return TaskAcceptedResponse(
        task_id=task.id,
        message=f"Документ принят, выполняется индексация ({written // 1024} КБ)",
    )


@router.get(
    "/knowledge/documents/{document_code}/versions",
    response_model=list[DocumentVersionResponse],
    summary="История редакций документа",
)
async def get_document_versions(
    document_code: str,
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.KB_READ))],
) -> list[DocumentVersionResponse]:
    """Возвращает все редакции документа.

    Args:
        document_code: Код документа.
        db: Сессия БД.
        _user: Текущий пользователь.

    Returns:
        Редакции от новой к старой.

    Raises:
        NotFoundError: Документ не найден.
    """
    document = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.code == document_code)
    )
    if document is None:
        raise NotFoundError(f"Документ «{document_code}» не найден")

    versions = await list_versions(db, document.id)
    return [DocumentVersionResponse.model_validate(item) for item in versions]


@router.post(
    "/knowledge/documents/{document_code}/versions/{version_number}/activate",
    response_model=DocumentVersionResponse,
    summary="Сделать редакцию действующей",
)
async def activate_document_version(
    request: Request,
    document_code: str,
    version_number: int,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.KB_VERSION_MANAGE))],
) -> DocumentVersionResponse:
    """Переключает действующую редакцию документа.

    Переиндексация не требуется: фрагменты всех редакций остаются в
    векторном хранилище, меняется только признак действующей версии.
    Кеш ответов сбрасывается автоматически.

    Args:
        request: HTTP-запрос.
        document_code: Код документа.
        version_number: Номер редакции.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Активированная редакция.

    Raises:
        NotFoundError: Документ не найден.
    """
    document = await db.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.code == document_code)
    )
    if document is None:
        raise NotFoundError(f"Документ «{document_code}» не найден")

    version = await rollback_to_version(db, document.id, version_number)

    await write_audit(
        db,
        user=user,
        action="kb.version_activate",
        resource_type="document_version",
        resource_id=str(version.id),
        request=request,
        details={"document_code": document_code, "version_number": version_number},
    )

    return DocumentVersionResponse.model_validate(version)


@router.delete(
    "/knowledge/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Без явного response_model=None FastAPI принимает аннотацию
    # возврата «-> None» за модель ответа и отказывается
    # регистрировать маршрут: тело при 204 недопустимо.
    response_model=None,
    summary="Удалить редакцию документа",
)
async def remove_version(
    request: Request,
    version_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.KB_DELETE))],
) -> None:
    """Удаляет редакцию документа вместе с её векторами.

    Действующую редакцию удалить нельзя: сначала нужно активировать другую.

    Args:
        request: HTTP-запрос.
        version_id: Идентификатор редакции.
        db: Сессия БД.
        user: Текущий пользователь.
    """
    await delete_version(db, version_id)
    await write_audit(
        db,
        user=user,
        action="kb.version_delete",
        resource_type="document_version",
        resource_id=str(version_id),
        request=request,
    )


@router.post(
    "/knowledge/reindex",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Переиндексировать базу знаний",
)
async def reindex(
    request: Request,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.KB_REINDEX))],
    rebuild_vocabulary: Annotated[bool, Query()] = True,
) -> TaskAcceptedResponse:
    """Запускает полную переиндексацию действующих редакций.

    Args:
        request: HTTP-запрос.
        db: Сессия БД.
        user: Текущий пользователь.
        rebuild_vocabulary: Пересобрать словарь полнотекстового поиска.

    Returns:
        Идентификатор задачи.
    """
    task = reindex_knowledge_base.delay(rebuild_vocabulary=rebuild_vocabulary)

    await write_audit(
        db,
        user=user,
        action="kb.reindex",
        request=request,
        details={"full": rebuild_vocabulary},
    )

    return TaskAcceptedResponse(task_id=task.id, message="Запущена переиндексация базы знаний")


# ═══════════════════════════ ОЦЕНКА КАЧЕСТВА ═══════════════════════════
@router.post(
    "/eval/run",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Запустить оценку качества ответов",
)
async def start_eval(
    request: Request,
    payload: EvalRunRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.EVAL_RUN))],
) -> TaskAcceptedResponse:
    """Запускает offline-оценку качества по эталонному набору вопросов.

    Args:
        request: HTTP-запрос.
        payload: Параметры прогона.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Идентификатор задачи и записи о прогоне.
    """
    record = EvalRun(
        name=payload.name or f"Прогон {settings.OLLAMA_MODEL_ACTIVE}",
        model_name=settings.OLLAMA_MODEL_ACTIVE,
        status="running",
    )
    db.add(record)
    await db.commit()

    task = run_quality_eval.delay(
        run_id=str(record.id),
        model_name=settings.OLLAMA_MODEL_ACTIVE,
        limit=payload.limit,
        skip_ragas=payload.skip_ragas,
    )
    record.task_id = task.id

    await write_audit(
        db,
        user=user,
        action="eval.run",
        resource_type="eval_run",
        resource_id=str(record.id),
        request=request,
    )

    return TaskAcceptedResponse(
        task_id=task.id,
        message="Запущена оценка качества ответов",
        resource_id=record.id,
    )


@router.get(
    "/eval/runs",
    response_model=list[EvalRunResponse],
    summary="История прогонов оценки качества",
)
async def list_eval_runs(
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.METRICS_READ))],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[EvalRunResponse]:
    """Возвращает результаты прогонов оценки качества.

    Сравнение прогонов разных моделей показывает эффект дообучения.

    Args:
        db: Сессия БД.
        _user: Текущий пользователь.
        limit: Ограничение количества.

    Returns:
        Прогоны, новые первыми.
    """
    result = await db.scalars(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit))
    return [EvalRunResponse.model_validate(item) for item in result]


@router.get(
    "/eval/runs/{run_id}",
    response_model=EvalRunResponse,
    summary="Результат прогона оценки",
)
async def get_eval_run(
    run_id: uuid.UUID,
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.METRICS_READ))],
) -> EvalRunResponse:
    """Возвращает метрики конкретного прогона.

    Args:
        run_id: Идентификатор прогона.
        db: Сессия БД.
        _user: Текущий пользователь.

    Returns:
        Метрики прогона.

    Raises:
        NotFoundError: Прогон не найден.
    """
    record = await db.get(EvalRun, run_id)
    if record is None:
        raise NotFoundError("Прогон оценки не найден")
    return EvalRunResponse.model_validate(record)


# ═══════════════════════════ ПОЛЬЗОВАТЕЛИ ═══════════════════════════
@router.get("/users", response_model=list[UserResponse], summary="Список пользователей")
async def list_users(
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.USER_READ))],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[UserResponse]:
    """Возвращает учётные записи.

    Args:
        db: Сессия БД.
        _user: Текущий пользователь.
        limit: Ограничение количества.

    Returns:
        Список пользователей.
    """
    result = await db.scalars(select(User).order_by(User.created_at.desc()).limit(limit))
    return [UserResponse.model_validate(item) for item in result]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать учётную запись",
)
async def create_user(
    request: Request,
    payload: UserCreateRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.USER_WRITE))],
) -> UserResponse:
    """Создаёт учётную запись сотрудника.

    Args:
        request: HTTP-запрос.
        payload: Данные новой учётной записи.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Созданная учётная запись.

    Raises:
        ConflictError: Пользователь с таким адресом уже существует.
    """
    email = payload.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError("Пользователь с таким адресом уже зарегистрирован")

    validate_password_strength(payload.password)

    created = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        department=payload.department,
        position=payload.position,
        role=payload.role,
        is_active=True,
    )
    db.add(created)
    await db.flush()

    await write_audit(
        db,
        user=user,
        action="user.create",
        resource_type="user",
        resource_id=str(created.id),
        request=request,
        details={"role": payload.role.value},
    )

    return UserResponse.model_validate(created)


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Изменить учётную запись",
)
async def update_user(
    request: Request,
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.USER_WRITE))],
) -> UserResponse:
    """Изменяет учётную запись: роль, подразделение, активность.

    Args:
        request: HTTP-запрос.
        user_id: Идентификатор изменяемой записи.
        payload: Изменяемые поля.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Обновлённая учётная запись.

    Raises:
        NotFoundError: Учётная запись не найдена.
        ValidationError: Попытка снять с себя права администратора.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise NotFoundError("Учётная запись не найдена")

    if target.id == user.id and payload.role is not None and payload.role != user.role:
        raise ValidationError("Нельзя изменить собственную роль")
    if target.id == user.id and payload.is_active is False:
        raise ValidationError("Нельзя отключить собственную учётную запись")

    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(target, field, value)

    if payload.is_active is True:
        target.failed_login_attempts = 0

    await db.flush()

    await write_audit(
        db,
        user=user,
        action="user.update",
        resource_type="user",
        resource_id=str(user_id),
        request=request,
        details={"fields": sorted(changes.keys())},
    )

    return UserResponse.model_validate(target)


# ═══════════════════════════ ЖУРНАЛ ДЕЙСТВИЙ ═══════════════════════════
@router.get("/audit", response_model=list[AuditLogResponse], summary="Журнал действий")
async def read_audit_log(
    db: SessionDep,
    _user: Annotated[User, Depends(require(Permission.AUDIT_READ))],
    action: Annotated[str, Query(description="Фильтр по коду действия")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditLogResponse]:
    """Возвращает записи журнала действий.

    В журнале фиксируется факт действия, но не содержание обращений:
    тексты вопросов и реквизиты документов туда не попадают.

    Args:
        db: Сессия БД.
        _user: Текущий пользователь.
        action: Фильтр по коду действия.
        limit: Ограничение количества.

    Returns:
        Записи журнала, новые первыми.
    """
    query = select(AuditLog).options(selectinload(AuditLog.user))
    if action:
        query = query.where(AuditLog.action == action)

    result = await db.scalars(query.order_by(AuditLog.created_at.desc()).limit(limit))

    items: list[AuditLogResponse] = []
    for record in result:
        response = AuditLogResponse.model_validate(record)
        response.user_email = record.user.email if record.user else ""
        items.append(response)
    return items


# ═══════════════════════════ СОСТОЯНИЕ КОНТУРА ═══════════════════════════
@router.get(
    "/system/health",
    response_model=HealthResponse,
    summary="Состояние компонентов контура",
)
async def system_health(
    _user: Annotated[User, Depends(require(Permission.METRICS_READ))],
) -> HealthResponse:
    """Возвращает состояние внешних зависимостей для экрана администратора.

    Дублирует состав проверок ``/health/ready``, но требует права на чтение
    показателей: корневая проба подключена вне префикса версии и наружу
    через Nginx не отдаётся, а состав моделей и показатели базы знаний
    незачем раскрывать без токена.

    Args:
        _user: Текущий пользователь; нужен для проверки прав.

    Returns:
        Общий статус и состояние каждого компонента.
    """
    components = await collect_components()
    return HealthResponse(
        status=overall_status(components),  # type: ignore[arg-type]
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
        components=components,
    )
