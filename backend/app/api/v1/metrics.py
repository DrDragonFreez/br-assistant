"""Экспорт метрик Prometheus.

Эндпоинт доступен только внутри контура. Он подключается в корень приложения
(``/metrics``), а не с префиксом версии: наружу Nginx отдаёт лишь ``/api/``,
поэтому путь без этого префикса недостижим из интернета, а Prometheus
обращается напрямую к ``backend:8000/metrics`` по внутренней сети. Метрики
раскрывают характер нагрузки и состав базы знаний, выставлять их наружу
не следует.

При запуске с несколькими рабочими процессами метрики агрегируются через
общий каталог, задаваемый переменной ``PROMETHEUS_MULTIPROC_DIR``.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from app.core.config import settings
from app.core.metrics import build_multiprocess_registry, multiproc_dir

router = APIRouter(tags=["Метрики"])


@router.get(
    "/metrics",
    summary="Метрики в формате Prometheus",
    include_in_schema=False,
    response_class=Response,
)
async def metrics() -> Response:
    """Отдаёт текущие значения метрик.

    Returns:
        Ответ в формате экспозиции Prometheus.
    """
    if not settings.METRICS_ENABLED:
        return Response(status_code=404)

    if multiproc_dir() is not None:
        registry = build_multiprocess_registry()
    else:
        registry = REGISTRY

    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
