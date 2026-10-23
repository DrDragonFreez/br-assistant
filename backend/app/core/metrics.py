"""Метрики Prometheus.

Здесь описаны доменные метрики RAG-контура: доля отказов «нет в базе знаний»,
количество цитат в ответе, срабатывания критика, латентность этапов графа.
Именно они выводятся на дашборд Grafana и показываются жюри. Метрики уровня
HTTP (коды ответов, время запроса) снимаются с логов Nginx: дублировать их
в приложении незачем, а счётчики по каждому пути раздувают выдачу.

API работает в нескольких процессах (``uvicorn --workers``), а индексация —
в процессах Celery. Чтобы Prometheus видел их суммарно, а не значения
случайного процесса, задаётся каталог ``PROMETHEUS_MULTIPROC_DIR``: каждый
процесс пишет в него свой файл, а эндпоинт ``/metrics`` собирает их вместе
(см. :func:`build_multiprocess_registry`). Каталог готовит
``docker-entrypoint.sh`` при старте контейнера.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess

NAMESPACE: Final[str] = "br"

# ─────────────────────────── ЗАПРОСЫ К RAG ───────────────────────────
rag_queries_total = Counter(
    f"{NAMESPACE}_rag_queries_total",
    "Количество обработанных запросов к базе знаний",
    labelnames=("intent", "role"),
)

rag_no_answer_total = Counter(
    f"{NAMESPACE}_rag_no_answer_total",
    "Количество честных отказов «в базе знаний нет информации»",
    labelnames=("reason",),
)

rag_clarification_total = Counter(
    f"{NAMESPACE}_rag_clarification_total",
    "Количество уточняющих вопросов, заданных пользователю",
    labelnames=("missing_slot",),
)

rag_citations_per_answer = Histogram(
    f"{NAMESPACE}_rag_citations_per_answer",
    "Число ссылок на пункты инструкции в одном ответе",
    buckets=(0, 1, 2, 3, 4, 5, 7, 10),
)

rag_top_score = Histogram(
    f"{NAMESPACE}_rag_top_score",
    "Релевантность лучшего найденного фрагмента после реранжирования",
    buckets=(0.0, 0.2, 0.35, 0.45, 0.6, 0.75, 0.85, 0.95, 1.0),
)

# ─────────────────────────── ЭТАПЫ ГРАФА ───────────────────────────
graph_node_latency_seconds = Histogram(
    f"{NAMESPACE}_graph_node_latency_seconds",
    "Длительность выполнения узла LangGraph",
    labelnames=("node",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 45, 90),
)

graph_node_errors_total = Counter(
    f"{NAMESPACE}_graph_node_errors_total",
    "Ошибки внутри узлов графа",
    labelnames=("node", "error_type"),
)

retrieval_latency_seconds = Histogram(
    f"{NAMESPACE}_retrieval_latency_seconds",
    "Длительность поиска в Qdrant",
    labelnames=("mode",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

rerank_latency_seconds = Histogram(
    f"{NAMESPACE}_rerank_latency_seconds",
    "Длительность переранжирования",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# ─────────────────────────── LLM ───────────────────────────
llm_latency_seconds = Histogram(
    f"{NAMESPACE}_llm_latency_seconds",
    "Длительность генерации ответа языковой моделью",
    labelnames=("model", "stage"),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 120),
)

llm_tokens_total = Counter(
    f"{NAMESPACE}_llm_tokens_total",
    "Количество обработанных токенов",
    labelnames=("model", "kind"),
)

llm_errors_total = Counter(
    f"{NAMESPACE}_llm_errors_total",
    "Ошибки обращения к языковой модели",
    labelnames=("model", "error_type"),
)

llm_fallback_total = Counter(
    f"{NAMESPACE}_llm_fallback_total",
    "Переключения с дообученной модели на базовую",
    labelnames=("from_model", "to_model"),
)

# ─────────────────────────── КРИТИК (анти-галлюцинации) ───────────────────────────
critic_verdicts_total = Counter(
    f"{NAMESPACE}_critic_verdicts_total",
    "Вердикты критика по сгенерированному ответу",
    labelnames=("verdict",),
)

critic_regenerations_total = Counter(
    f"{NAMESPACE}_critic_regenerations_total",
    "Повторные генерации, инициированные критиком",
)

# ─────────────────────────── ДОКУМЕНТЫ ───────────────────────────
documents_generated_total = Counter(
    f"{NAMESPACE}_documents_generated_total",
    "Сформированные черновики документов",
    labelnames=("template_code",),
)

documents_validated_total = Counter(
    f"{NAMESPACE}_documents_validated_total",
    "Проверенные черновики документов",
    labelnames=("verdict",),
)

document_violations_found = Histogram(
    f"{NAMESPACE}_document_violations_found",
    "Количество несоответствий требованиям в проверенном черновике",
    buckets=(0, 1, 2, 3, 5, 8, 13, 21),
)

# ─────────────────────────── БАЗА ЗНАНИЙ ───────────────────────────
kb_chunks_indexed = Gauge(
    f"{NAMESPACE}_kb_chunks_indexed",
    "Количество проиндексированных фрагментов в базе знаний",
    labelnames=("collection",),
    multiprocess_mode="max",
)

kb_documents_total = Gauge(
    f"{NAMESPACE}_kb_documents_total",
    "Количество документов в базе знаний",
    labelnames=("status",),
    multiprocess_mode="max",
)

kb_active_version = Gauge(
    f"{NAMESPACE}_kb_active_version",
    "Номер активной версии документа базы знаний",
    labelnames=("document_code",),
    multiprocess_mode="max",
)

indexing_duration_seconds = Histogram(
    f"{NAMESPACE}_indexing_duration_seconds",
    "Длительность индексации документа",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)


@contextmanager
def observe_latency(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Контекстный менеджер для замера длительности блока кода.

    Args:
        histogram: Гистограмма, в которую пишется результат.
        **labels: Значения меток гистограммы.

    Yields:
        Ничего; длительность фиксируется при выходе из блока.

    Example:
        >>> with observe_latency(rerank_latency_seconds):
        ...     ...  # вызов реранкера
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        if labels:
            histogram.labels(**labels).observe(elapsed)
        else:
            histogram.observe(elapsed)


def multiproc_dir() -> Path | None:
    """Возвращает каталог метрик многопроцессного режима, если он задан.

    Returns:
        Путь из ``PROMETHEUS_MULTIPROC_DIR`` либо None, если переменная
        не выставлена и используется обычный внутрипроцессный реестр.
    """
    raw = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    return Path(raw) if raw else None


def build_multiprocess_registry() -> CollectorRegistry:
    """Собирает реестр метрик для многопроцессного режима uvicorn.

    Используется, когда сервис запущен с ``--workers > 1`` и задана переменная
    окружения ``PROMETHEUS_MULTIPROC_DIR``.

    Returns:
        Реестр, агрегирующий метрики всех воркеров.
    """
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def drop_process_metrics(pid: int | None = None) -> None:
    """Удаляет файлы метрик завершившегося процесса.

    Вызывается при остановке процесса Celery-воркера: дочерние процессы
    сменяются по ``worker_max_tasks_per_child``, и без очистки значения
    их gauge-ов продолжали бы попадать в агрегированную выдачу.

    Args:
        pid: Идентификатор процесса; по умолчанию текущий.
    """
    if multiproc_dir() is None:
        return
    try:
        multiprocess.mark_process_dead(pid if pid is not None else os.getpid())
    except Exception:  # noqa: BLE001
        return
