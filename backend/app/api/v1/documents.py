"""Работа с документами: шаблоны, генерация черновиков, проверка проектов.

Загружаемые файлы проверяются по трём параметрам: расширение, фактический
размер и успешность разбора. Проверять только заголовок ``Content-Length``
недостаточно — его можно подделать, поэтому размер считается по мере чтения.

Файлы сохраняются во временный каталог с генерируемым именем: исходное имя
от пользователя в путь не подставляется.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import aiofiles
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.v1.deps import AccessLevels, SessionDep, limiter, require, write_audit
from app.api.v1.schemas import (
    DocumentGenerateRequest,
    GeneratedDocumentResponse,
    TaskAcceptedResponse,
    TemplateResponse,
    ValidationReportResponse,
)
from app.core.config import settings
from app.core.exceptions import (
    FileTooLargeError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedFileTypeError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.rbac import Permission, Role
from app.db.models import ChatSession, GeneratedDocument, User, ValidationReport
from app.rag.loader import load_document
from app.services.template_registry import get_template_registry
from app.workers.tasks import generate_document, validate_document

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["Документы"])

UPLOAD_CHUNK_SIZE = 1024 * 1024


async def _save_upload(upload: UploadFile) -> Path:
    """Сохраняет загруженный файл во временный каталог.

    Args:
        upload: Загруженный файл.

    Returns:
        Путь к сохранённому файлу.

    Raises:
        UnsupportedFileTypeError: Недопустимое расширение.
        FileTooLargeError: Размер превышает лимит.
        ValidationError: Загружен пустой файл.
    """
    original_name = upload.filename or "upload"
    suffix = Path(original_name).suffix.lower()

    if suffix not in settings.ALLOWED_UPLOAD_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Формат «{suffix or 'без расширения'}» не поддерживается. "
            f"Доступные: {', '.join(settings.ALLOWED_UPLOAD_EXTENSIONS)}"
        )

    upload_dir = Path(settings.ARTIFACTS_DIR) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid.uuid4().hex}{suffix}"

    written = 0
    async with aiofiles.open(target, "wb") as handle:
        while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
            written += len(chunk)
            if written > settings.max_upload_size_bytes:
                await handle.close()
                target.unlink(missing_ok=True)
                raise FileTooLargeError(
                    f"Размер файла превышает {settings.MAX_UPLOAD_SIZE_MB} МБ"
                )
            await handle.write(chunk)

    if written == 0:
        target.unlink(missing_ok=True)
        raise ValidationError("Загружен пустой файл")

    logger.info("file_uploaded", extension=suffix, size_bytes=written)
    return target


# ═══════════════════════════ ШАБЛОНЫ ═══════════════════════════
@router.get(
    "/templates",
    response_model=list[TemplateResponse],
    summary="Доступные шаблоны документов",
)
async def list_templates(
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.TEMPLATE_READ))],
) -> list[TemplateResponse]:
    """Возвращает шаблоны, доступные роли пользователя.

    Args:
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Список шаблонов модульных документов.
    """
    templates = await get_template_registry().list_templates(db, user_role=user.role.value)
    return [TemplateResponse.model_validate(item) for item in templates]


@router.get(
    "/templates/{code}",
    response_model=TemplateResponse,
    summary="Описание шаблона",
)
async def get_template(
    code: str,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.TEMPLATE_READ))],
) -> TemplateResponse:
    """Возвращает схему полей и маршрут согласования шаблона.

    Args:
        code: Код шаблона.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Описание шаблона.
    """
    template = await get_template_registry().get_by_code(db, code, user_role=user.role.value)
    return TemplateResponse.model_validate(template)


# ═══════════════════════════ ГЕНЕРАЦИЯ ЧЕРНОВИКОВ ═══════════════════════════
@router.post(
    "/generate",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Сформировать черновик документа",
)
@limiter.limit("20/minute")
async def create_document(
    request: Request,
    payload: DocumentGenerateRequest,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_GENERATE))],
) -> TaskAcceptedResponse:
    """Ставит в очередь формирование черновика документа.

    Генерация вынесена в фоновую задачу: сборка DOCX с листом согласования
    занимает до нескольких секунд, и держать на ней HTTP-соединение незачем.

    Args:
        request: HTTP-запрос.
        payload: Код шаблона и значения полей.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Идентификатор задачи и созданной записи.

    Raises:
        ValidationError: Не заполнены обязательные поля.
    """
    registry = get_template_registry()
    template = await registry.get_by_code(db, payload.template_code, user_role=user.role.value)

    missing = registry.missing_fields(template, payload.values)
    if missing:
        labels = [
            (registry.field_by_name(template, name) or {}).get("label", name)
            for name in missing
        ]
        raise ValidationError(
            "Не заполнены обязательные поля: " + ", ".join(labels),
            code="missing_required_fields",
            context={"missing": missing},
        )

    if payload.session_id is not None:
        chat_session = await db.get(ChatSession, payload.session_id)
        if chat_session is None:
            raise NotFoundError("Диалог не найден")
        if chat_session.user_id != user.id:
            raise PermissionDeniedError("Диалог принадлежит другому пользователю")

    record = GeneratedDocument(
        user_id=user.id,
        template_id=template.id,
        session_id=payload.session_id,
        status="pending",
        filled_fields=payload.values,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(record)
    await db.commit()

    task = generate_document.delay(
        document_id=str(record.id),
        template_code=template.code,
        values=payload.values,
        user_id=str(user.id),
    )
    record.task_id = task.id

    await write_audit(
        db,
        user=user,
        action="document.generate",
        resource_type="generated_document",
        resource_id=str(record.id),
        request=request,
        details={"template_code": template.code},
    )

    return TaskAcceptedResponse(
        task_id=task.id,
        message=f"Формируется черновик документа «{template.name}»",
        resource_id=record.id,
    )


@router.get(
    "/generated",
    response_model=list[GeneratedDocumentResponse],
    summary="Мои черновики документов",
)
async def list_generated(
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_GENERATE))],
) -> list[GeneratedDocumentResponse]:
    """Возвращает сформированные черновики текущего пользователя.

    Args:
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Список черновиков, новые первыми.
    """
    result = await db.scalars(
        select(GeneratedDocument)
        .where(
            GeneratedDocument.user_id == user.id,
            GeneratedDocument.status != "expired",
        )
        .order_by(GeneratedDocument.created_at.desc())
        .limit(50)
    )

    items: list[GeneratedDocumentResponse] = []
    for record in result:
        response = GeneratedDocumentResponse.model_validate(record)
        response.template_name = record.template.name if record.template else ""
        if record.status == "ready":
            response.download_url = (
                f"{settings.API_V1_PREFIX}/documents/generated/{record.id}/download"
            )
        items.append(response)
    return items


@router.get(
    "/generated/{document_id}",
    response_model=GeneratedDocumentResponse,
    summary="Состояние черновика документа",
)
async def get_generated(
    document_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_GENERATE))],
) -> GeneratedDocumentResponse:
    """Возвращает состояние формирования черновика.

    Args:
        document_id: Идентификатор черновика.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Сведения о черновике.

    Raises:
        NotFoundError: Черновик не найден.
        PermissionDeniedError: Черновик принадлежит другому пользователю.
    """
    record = await db.get(GeneratedDocument, document_id)
    if record is None:
        raise NotFoundError("Черновик документа не найден")
    if record.user_id != user.id and user.role != Role.ADMIN:
        raise PermissionDeniedError("Документ принадлежит другому пользователю")

    response = GeneratedDocumentResponse.model_validate(record)
    response.template_name = record.template.name if record.template else ""
    if record.status == "ready":
        response.download_url = (
            f"{settings.API_V1_PREFIX}/documents/generated/{record.id}/download"
        )
    return response


@router.get(
    "/generated/{document_id}/download",
    summary="Скачать черновик документа",
    response_class=FileResponse,
)
async def download_generated(
    request: Request,
    document_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_GENERATE))],
) -> FileResponse:
    """Отдаёт файл черновика.

    Args:
        request: HTTP-запрос.
        document_id: Идентификатор черновика.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Файл в формате DOCX.

    Raises:
        NotFoundError: Черновик или файл отсутствует.
        PermissionDeniedError: Доступ к чужому документу.
    """
    record = await db.get(GeneratedDocument, document_id)
    if record is None:
        raise NotFoundError("Черновик документа не найден")
    if record.user_id != user.id and user.role != Role.ADMIN:
        raise PermissionDeniedError("Документ принадлежит другому пользователю")
    if record.status != "ready" or not record.file_path:
        raise NotFoundError("Файл ещё не сформирован")

    path = Path(record.file_path)
    if not path.exists():
        raise NotFoundError("Файл документа недоступен")

    await write_audit(
        db,
        user=user,
        action="document.download",
        resource_type="generated_document",
        resource_id=str(document_id),
        request=request,
    )

    return FileResponse(
        path=str(path),
        filename=record.filename or path.name,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
    )


# ═══════════════════════════ ПРОВЕРКА ПРОЕКТОВ ═══════════════════════════
@router.post(
    "/validate",
    response_model=TaskAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Проверить проект документа",
)
@limiter.limit("10/minute")
async def validate_draft(
    request: Request,
    db: SessionDep,
    access_levels: AccessLevels,
    user: Annotated[User, Depends(require(Permission.DOC_VALIDATE))],
    file: Annotated[UploadFile, File(description="Проект документа (.docx, .pdf, .txt)")],
    doc_kind: Annotated[str, Form(description="Тип документа, если известен")] = "",
) -> TaskAcceptedResponse:
    """Принимает проект документа и ставит проверку в очередь.

    Args:
        request: HTTP-запрос.
        db: Сессия БД.
        access_levels: Грифы, доступные роли пользователя.
        user: Текущий пользователь.
        file: Загружаемый проект документа.
        doc_kind: Тип документа.

    Returns:
        Идентификатор задачи и созданного отчёта.
    """
    saved_path = await _save_upload(file)

    try:
        loaded = await load_document(saved_path)
    finally:
        saved_path.unlink(missing_ok=True)

    report = ValidationReport(
        user_id=user.id,
        source_filename=file.filename or "",
        detected_doc_kind=doc_kind,
        verdict="pending",
    )
    db.add(report)
    await db.commit()

    task = validate_document.delay(
        report_id=str(report.id),
        document_text=loaded.full_text,
        allowed_levels=access_levels,
        doc_kind=doc_kind,
    )
    report.task_id = task.id

    await write_audit(
        db,
        user=user,
        action="document.validate",
        resource_type="validation_report",
        resource_id=str(report.id),
        request=request,
        details={"pages": loaded.pages_count},
    )

    return TaskAcceptedResponse(
        task_id=task.id,
        message="Проект документа принят на проверку",
        resource_id=report.id,
    )


@router.get(
    "/validate/{report_id}",
    response_model=ValidationReportResponse,
    summary="Отчёт проверки документа",
)
async def get_validation_report(
    report_id: uuid.UUID,
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_VALIDATE))],
) -> ValidationReportResponse:
    """Возвращает результат проверки проекта документа.

    Args:
        report_id: Идентификатор отчёта.
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Отчёт с перечнем несоответствий и рекомендациями.

    Raises:
        NotFoundError: Отчёт не найден.
        PermissionDeniedError: Отчёт принадлежит другому пользователю.
    """
    report = await db.get(ValidationReport, report_id)
    if report is None:
        raise NotFoundError("Отчёт проверки не найден")
    if report.user_id != user.id and user.role not in {Role.ADMIN, Role.AUDITOR}:
        raise PermissionDeniedError("Отчёт принадлежит другому пользователю")

    return ValidationReportResponse.model_validate(report)


@router.get(
    "/validate",
    response_model=list[ValidationReportResponse],
    summary="Мои отчёты проверки",
)
async def list_validation_reports(
    db: SessionDep,
    user: Annotated[User, Depends(require(Permission.DOC_VALIDATE))],
) -> list[ValidationReportResponse]:
    """Возвращает отчёты проверки текущего пользователя.

    Args:
        db: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Отчёты, новые первыми.
    """
    result = await db.scalars(
        select(ValidationReport)
        .where(ValidationReport.user_id == user.id)
        .order_by(ValidationReport.created_at.desc())
        .limit(30)
    )
    return [ValidationReportResponse.model_validate(item) for item in result]
