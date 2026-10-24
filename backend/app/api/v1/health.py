"""Проверки состояния сервиса.

Два уровня, как требует оркестратор:

* ``/health`` — жив ли процесс. Отвечает всегда, не ходит во внешние
  сервисы: это liveness-проба для Docker и Kubernetes.
* ``/health/ready`` — готов ли сервис принимать трафик. Опрашивает
  PostgreSQL, Redis, Qdrant и Ollama. Используется как readiness-проба
  и как страница диагностики при демонстрации.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.v1.schemas import ComponentHealth, HealthResponse
from app.core.config import settings
from app.db import postgres, qdrant_client
from app.db import redis as redis_db
from app.rag.embedder import get_embedder
from app.rag.generator import get_generator
from app.rag.reranker import get_reranker

router = APIRouter(tags=["Состояние"])


@router.get("/health", response_model=HealthResponse, summary="Проверка жизнеспособности")
async def health() -> HealthResponse:
    """Сообщает, что процесс работает.

    Внешние сервисы не опрашиваются: проба должна отвечать мгновенно
    и не падать из-за недоступности зависимостей.

    Returns:
        Состояние сервиса.
    """
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
        components=[],
    )


CRITICAL_COMPONENTS: frozenset[str] = frozenset({"postgres", "qdrant", "ollama", "embeddings"})


async def collect_components() -> list[ComponentHealth]:
    """Опрашивает все внешние зависимости сервиса.

    Вынесено из обработчика: тем же составом проверок пользуется экран
    администратора (``GET /admin/system/health``), и расходиться они
    не должны.

    Returns:
        Состояние каждого компонента с подробностями.
    """
    components: list[ComponentHealth] = []

    postgres_ok = await postgres.check_connection()
    components.append(ComponentHealth(name="postgres", healthy=postgres_ok))

    redis_ok = await redis_db.check_connection()
    components.append(ComponentHealth(name="redis", healthy=redis_ok))

    qdrant_ok = await qdrant_client.check_connection()
    qdrant_details = await qdrant_client.collection_stats() if qdrant_ok else {}
    components.append(
        ComponentHealth(name="qdrant", healthy=qdrant_ok, details=qdrant_details)
    )

    llm_health = await get_generator().health()
    components.append(
        ComponentHealth(
            name="ollama",
            healthy=bool(llm_health.get("available")),
            details={
                "primary_model": llm_health.get("primary_model", ""),
                "primary_ready": llm_health.get("primary_ready", False),
                "fallback_ready": llm_health.get("fallback_ready", False),
                "using_fallback": llm_health.get("using_fallback", False),
            },
        )
    )

    embedder_ok = await get_embedder().health()
    components.append(
        ComponentHealth(
            name="embeddings",
            healthy=embedder_ok,
            details={"model": settings.OLLAMA_EMBED_MODEL},
        )
    )

    # Реранкер, выключенный настройкой, готовности не нарушает.
    #
    # RERANKER_ENABLED=false — поддерживаемый режим работы: граф переходит
    # на MMR-переранжирование и отвечает со ссылками на пункты как обычно.
    # Пока этот режим давал «degraded», контур в штатной конфигурации
    # всегда числился неисправным: такую пробу перестают читать, а
    # оркестратор по ней не выпустит новую версию в работу. Качество
    # поиска без реранкера ниже, и это видно в отчёте об оценке, но к
    # готовности сервиса отношения не имеет.
    if not settings.RERANKER_ENABLED:
        reranker_ok = True
        reranker_details = {
            "enabled": False,
            "mode": "отключён настройкой, переранжирование выполняет MMR",
        }
    else:
        reranker_ok = await get_reranker().health()
        reranker_details = {
            "enabled": True,
            "fallback": "mmr" if not reranker_ok else "",
        }

    components.append(
        ComponentHealth(
            name="reranker",
            healthy=reranker_ok,
            details=reranker_details,
        )
    )

    return components


def overall_status(components: list[ComponentHealth]) -> str:
    """Выводит общий статус контура из состояния компонентов.

    Args:
        components: Результат :func:`collect_components`.

    Returns:
        ``ok``, ``degraded`` либо ``unavailable``.
    """
    critical_failed = any(
        item.name in CRITICAL_COMPONENTS and not item.healthy for item in components
    )
    if critical_failed:
        return "unavailable"

    optional_failed = any(
        item.name not in CRITICAL_COMPONENTS and not item.healthy for item in components
    )
    return "degraded" if optional_failed else "ok"


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    summary="Проверка готовности к работе",
)
async def readiness(response: Response) -> HealthResponse:
    """Проверяет доступность всех зависимостей.

    Args:
        response: Ответ, в который проставляется HTTP-статус.

    Returns:
        Подробное состояние каждого компонента.
    """
    components = await collect_components()
    overall = overall_status(components)

    if overall == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,  # type: ignore[arg-type]
        version=settings.APP_VERSION,
        environment=settings.APP_ENV,
        components=components,
    )
