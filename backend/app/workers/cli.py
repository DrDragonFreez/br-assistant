"""Консольные команды обслуживания.

Запуск: ``python -m app.workers.cli <команда>`` (используется в Makefile).

Команды выполняются синхронно в текущем процессе, а не через очередь: на
хакатоне важно видеть вывод индексации сразу, а не искать его в логах воркера.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.models import DocumentVersion, KnowledgeDocument
from app.db.postgres import close_postgres, lifespan_postgres, session_scope
from app.db.redis import close_redis
from app.db.qdrant_client import (
    close_qdrant,
    collection_stats,
    ensure_collection,
    lifespan_qdrant,
)
from app.rag.loader import discover_documents
from app.rag.versioning import list_versions, rollback_to_version
from app.services.document_indexer import get_indexer

logger = get_logger(__name__)

FILENAME_TO_CODE: dict[str, str] = {
    "инструкц": "INSTR-DOC",
    "instruction": "INSTR-DOC",
    "документооборот": "INSTR-DOC",
    "матриц": "MATRIX-APPROVAL",
    "matrix": "MATRIX-APPROVAL",
    "согласовани": "MATRIX-APPROVAL",
    "модульн": "GUIDE-MODULES",
    "рекомендац": "GUIDE-MODULES",
    "faq": "FAQ-PORTAL",
    "вопрос": "FAQ-PORTAL",
}


def guess_document_code(filename: str) -> str:
    """Определяет код документа по имени файла.

    Args:
        filename: Имя файла.

    Returns:
        Код документа; по умолчанию — код основной инструкции.
    """
    lowered = filename.lower()
    for marker, code in FILENAME_TO_CODE.items():
        if marker in lowered:
            return code
    return "INSTR-DOC"


async def cmd_reindex(args: argparse.Namespace) -> int:
    """Индексирует документы из каталога.

    Args:
        args: Аргументы командной строки.

    Returns:
        Код возврата процесса.
    """
    directory = Path(args.path or settings.INSTRUCTIONS_DIR)
    files = await discover_documents(directory)

    if not files:
        print(f"⚠ В каталоге {directory} не найдено поддерживаемых файлов.")
        print("  Положите инструкцию, матрицу согласования и рекомендации в data/instructions/")
        return 1

    print(f"Найдено файлов: {len(files)}")
    await ensure_collection(recreate=args.recreate)

    succeeded = 0
    failed: list[str] = []

    for path in files:
        code = args.document_code or guess_document_code(path.name)
        print(f"  → {path.name} ({code}) ... ", end="", flush=True)
        try:
            async with session_scope() as session:
                summary = await get_indexer().index_file(
                    session,
                    document_code=code,
                    file_path=path,
                    version_label=args.version_label,
                    changelog=args.changelog,
                    activate=True,
                )
            if summary.get("skipped"):
                print("пропущен (дубликат)")
            else:
                print(
                    f"готово: {summary['chunks']} фрагментов, "
                    f"{summary['with_clause']} с номером пункта"
                )
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ОШИБКА: {exc}")
            failed.append(f"{path.name}: {exc}")

    stats = await collection_stats()
    print()
    print(f"Проиндексировано документов: {succeeded} из {len(files)}")
    print(f"Всего фрагментов в коллекции: {stats.get('points_count', 0)}")

    if failed:
        print("\nНе удалось обработать:")
        for item in failed:
            print(f"  - {item}")
        return 1
    return 0


async def cmd_status(_args: argparse.Namespace) -> int:
    """Показывает состояние базы знаний.

    Args:
        _args: Аргументы командной строки (не используются).

    Returns:
        Код возврата процесса.
    """
    stats = await collection_stats()

    print("Векторное хранилище:")
    print(f"  коллекция: {stats.get('collection')}")
    print(f"  статус:    {stats.get('status')}")
    print(f"  точек:     {stats.get('points_count', 0)}")
    print()

    async with session_scope() as session:
        documents = list(await session.scalars(select(KnowledgeDocument)))

        if not documents:
            print("База знаний пуста: документы не зарегистрированы.")
            print("Выполните: make seed")
            return 0

        print("Документы базы знаний:")
        for document in documents:
            versions = await list_versions(session, document.id)
            active = next((v for v in versions if v.is_active), None)
            print(f"  {document.code} — {document.title}")
            print(f"    гриф: {document.access_level.value}, редакций: {len(versions)}")
            if active:
                print(
                    f"    действующая: № {active.version_number} "
                    f"({active.version_label}), фрагментов: {active.chunks_count}, "
                    f"статус: {active.status}"
                )
            else:
                print("    действующая редакция не активирована")
            for version in versions:
                if version.status == "failed":
                    print(
                        f"    ⚠ редакция № {version.version_number}: "
                        f"{version.indexing_error[:120]}"
                    )
    return 0


async def cmd_versions(args: argparse.Namespace) -> int:
    """Показывает историю редакций документа.

    Args:
        args: Аргументы командной строки.

    Returns:
        Код возврата процесса.
    """
    async with session_scope() as session:
        document = await session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.code == args.document_code)
        )
        if document is None:
            print(f"Документ «{args.document_code}» не найден.")
            return 1

        versions = await list_versions(session, document.id)
        print(f"{document.title} ({document.code})")
        print()
        for version in versions:
            marker = "►" if version.is_active else " "
            print(
                f"{marker} № {version.version_number:<3} {version.version_label:<20} "
                f"{version.status:<10} фрагментов: {version.chunks_count:<5} "
                f"{version.created_at:%Y-%m-%d %H:%M}"
            )
            if version.changelog:
                print(f"     {version.changelog}")
    return 0


async def cmd_activate(args: argparse.Namespace) -> int:
    """Активирует указанную редакцию документа.

    Args:
        args: Аргументы командной строки.

    Returns:
        Код возврата процесса.
    """
    async with session_scope() as session:
        document = await session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.code == args.document_code)
        )
        if document is None:
            print(f"Документ «{args.document_code}» не найден.")
            return 1

        try:
            version = await rollback_to_version(session, document.id, args.version)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось активировать редакцию: {exc}")
            return 1

    print(
        f"✓ Действующей редакцией назначена № {version.version_number} "
        f"({version.version_label})"
    )
    print("  Кеш ответов сброшен.")
    return 0


async def cmd_recreate(args: argparse.Namespace) -> int:
    """Пересоздаёт коллекцию Qdrant.

    Args:
        args: Аргументы командной строки.

    Returns:
        Код возврата процесса.
    """
    if not args.yes:
        answer = input("Коллекция будет удалена вместе со всеми векторами. Продолжить? [y/N] ")
        if answer.strip().lower() not in {"y", "yes", "д", "да"}:
            print("Отменено.")
            return 0

    await ensure_collection(recreate=True)

    async with session_scope() as session:
        await session.execute(
            update(DocumentVersion).values(status="pending", is_active=False)
        )

    print("✓ Коллекция пересоздана. Выполните переиндексацию: make index")
    return 0


async def cmd_export_stats(args: argparse.Namespace) -> int:
    """Выгружает статистику базы знаний в JSON.

    Args:
        args: Аргументы командной строки.

    Returns:
        Код возврата процесса.
    """
    stats = await collection_stats()

    async with session_scope() as session:
        documents = list(await session.scalars(select(KnowledgeDocument)))
        payload: dict[str, Any] = {"collection": stats, "documents": []}
        for document in documents:
            versions = await list_versions(session, document.id)
            payload["documents"].append(
                {
                    "code": document.code,
                    "title": document.title,
                    "access_level": document.access_level.value,
                    "versions": [
                        {
                            "number": v.version_number,
                            "label": v.version_label,
                            "status": v.status,
                            "is_active": v.is_active,
                            "chunks": v.chunks_count,
                            "pages": v.pages_count,
                        }
                        for v in versions
                    ],
                }
            )

    output = Path(args.output or Path(settings.ARTIFACTS_DIR) / "kb_stats.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ Статистика сохранена: {output}")
    return 0


COMMANDS = {
    "reindex": cmd_reindex,
    "status": cmd_status,
    "versions": cmd_versions,
    "activate": cmd_activate,
    "recreate": cmd_recreate,
    "export-stats": cmd_export_stats,
}


def build_parser() -> argparse.ArgumentParser:
    """Собирает парсер аргументов командной строки.

    Returns:
        Настроенный парсер.
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.cli",
        description="Обслуживание базы знаний помощника по регламентам",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reindex = subparsers.add_parser("reindex", help="Проиндексировать документы из каталога")
    reindex.add_argument(
        "--path", default="", help="Каталог с файлами (по умолчанию data/instructions)"
    )
    reindex.add_argument("--document-code", default="", help="Принудительный код документа")
    reindex.add_argument("--version-label", default="", help="Метка редакции")
    reindex.add_argument("--changelog", default="", help="Описание изменений")
    reindex.add_argument(
        "--recreate", action="store_true", help="Пересоздать коллекцию перед индексацией"
    )

    subparsers.add_parser("status", help="Показать состояние базы знаний")

    versions = subparsers.add_parser("versions", help="История редакций документа")
    versions.add_argument("document_code", help="Код документа, например INSTR-DOC")

    activate = subparsers.add_parser("activate", help="Активировать редакцию документа")
    activate.add_argument("document_code", help="Код документа")
    activate.add_argument("version", type=int, help="Номер редакции")

    recreate = subparsers.add_parser("recreate", help="Пересоздать коллекцию Qdrant")
    recreate.add_argument("--yes", action="store_true", help="Не запрашивать подтверждение")

    export = subparsers.add_parser("export-stats", help="Выгрузить статистику в JSON")
    export.add_argument("--output", default="", help="Путь к файлу отчёта")

    return parser


async def _main_async(args: argparse.Namespace) -> int:
    """Асинхронная точка входа.

    Args:
        args: Разобранные аргументы.

    Returns:
        Код возврата процесса.
    """
    await lifespan_postgres()
    await lifespan_qdrant()
    try:
        handler = COMMANDS[args.command]
        return await handler(args)
    finally:
        # Соединение с Redis закрывается явно: команды обслуживания сбрасывают
        # кэш ответов, а оставленный пул освобождался бы сборщиком мусора уже
        # после закрытия event loop — с трассировкой «Event loop is closed»
        # в конце вполне успешного прогона.
        await close_redis()
        await close_qdrant()
        await close_postgres()


def main() -> None:
    """Точка входа CLI."""
    configure_logging()
    settings.ensure_dirs()
    args = build_parser().parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
