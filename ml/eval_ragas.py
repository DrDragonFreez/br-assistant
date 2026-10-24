"""Сравнение базовой и дообученной модели на эталонном наборе.

Отдельный скрипт, а не копия оценки из бэкенда: сама оценка живёт в
``app.services.ragas_eval`` и работает внутри контура, а здесь решается
другая задача — прогнать один и тот же набор вопросов на двух моделях
и показать разницу. Именно эта таблица отвечает на вопрос защиты
«зачем вы дообучали модель, если есть поиск».

Скрипт запускается с хоста (не из контейнера) и переключает модель через
переменную окружения ``OLLAMA_MODEL_ACTIVE`` перед каждым прогоном.
Контур при этом должен быть поднят: нужны Qdrant с проиндексированной
базой знаний и Ollama с обеими моделями.

Запуск::

    python ml/eval_ragas.py --models qwen2.5:7b-instruct br-assistant \\
        --eval-set data/eval_set/golden.jsonl --output docs/ragas_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_PATH))

DEFAULT_MODELS = ("qwen2.5:7b-instruct", "br-assistant")
DEFAULT_EVAL_SET = Path("data/eval_set/golden.jsonl")
DEFAULT_OUTPUT = Path("ml/outputs/model_comparison.json")

# Метрики, которые сравниваются между моделями, и их подписи для таблицы.
# Имена совпадают с ключами report["metrics"] из app.services.ragas_eval.
COMPARED = (
    ("citation_hit_rate", "Попадание в нужный пункт"),
    ("citation_precision", "Точность цитирования"),
    ("no_answer_accuracy", "Корректность отказов"),
    ("answered_rate", "Доля отвеченных"),
    ("critic_approval_rate", "Одобрено критиком"),
    ("avg_latency_ms", "Средняя задержка, мс"),
    ("faithfulness", "Faithfulness (RAGAS)"),
    ("answer_relevancy", "Answer relevancy (RAGAS)"),
)


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки.

    Returns:
        Разобранные аргументы.
    """
    parser = argparse.ArgumentParser(description="Сравнение моделей на эталонном наборе")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число вопросов")
    parser.add_argument(
        "--skip-ragas",
        action="store_true",
        help="Только собственные метрики: быстрее в разы, не требует модели-судьи",
    )
    parser.add_argument(
        "--ollama",
        default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Адрес Ollama с хоста",
    )
    parser.add_argument(
        "--qdrant",
        default=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        help="Адрес Qdrant с хоста",
    )
    return parser.parse_args()


def prepare_environment(args: argparse.Namespace, model: str) -> None:
    """Готовит окружение для прогона на конкретной модели.

    Настройки бэкенда читаются при импорте, поэтому переменные
    выставляются до него, а кеш настроек сбрасывается между прогонами.

    Args:
        args: Аргументы командной строки.
        model: Имя модели в Ollama.
    """
    os.environ["OLLAMA_MODEL_ACTIVE"] = model
    os.environ["OLLAMA_BASE_URL"] = args.ollama
    os.environ["QDRANT_URL"] = args.qdrant
    # Запасная модель совпадает с проверяемой: иначе при сбое прогон
    # незаметно уехал бы на другую модель и сравнение потеряло смысл.
    os.environ["OLLAMA_LLM_MODEL"] = model
    os.environ["LLM_FALLBACK_ENABLED"] = "false"
    os.environ.setdefault("APP_ENV", "local")
    os.environ.setdefault("LOG_FORMAT", "console")
    os.environ.setdefault("SECRET_KEY", "local-evaluation-key-not-for-production")


async def run_for_model(args: argparse.Namespace, model: str) -> dict[str, Any]:
    """Прогоняет эталонный набор на одной модели.

    Args:
        args: Аргументы командной строки.
        model: Имя модели в Ollama.

    Returns:
        Словарь метрик прогона.
    """
    prepare_environment(args, model)

    # Импорт внутри функции: между моделями нужно пересоздать настройки
    # и клиентов, а они кешируются на уровне модулей.
    for name in [item for item in sys.modules if item.startswith("app.")]:
        del sys.modules[name]

    from app.core.config import get_settings
    from app.rag.generator import close_generator
    from app.services.ragas_eval import run_evaluation

    get_settings.cache_clear()

    print(f">> прогон на модели {model}")
    try:
        report = await run_evaluation(
            eval_set_path=args.eval_set,
            model_name=model,
            limit=args.limit,
            skip_ragas=args.skip_ragas,
        )
    finally:
        # Клиент модели закрывается явно: между прогонами создаётся новый,
        # и незакрытое соединение осталось бы висеть до конца процесса.
        await close_generator()

    return report


def print_table(results: dict[str, dict[str, Any]]) -> None:
    """Печатает сравнительную таблицу метрик.

    Args:
        results: Метрики по каждой модели.
    """
    models = list(results)
    width = max((len(label) for _, label in COMPARED), default=20)

    print()
    print("СРАВНЕНИЕ МОДЕЛЕЙ")
    print("-" * (width + 18 * len(models)))
    header = "Метрика".ljust(width) + "".join(model[:16].rjust(18) for model in models)
    print(header)
    print("-" * (width + 18 * len(models)))

    for key, label in COMPARED:
        row = label.ljust(width)
        for model in models:
            value = (results[model].get("metrics") or {}).get(key)
            if value is None:
                cell = "—"
            elif key == "avg_latency_ms":
                cell = f"{value:.0f}"
            else:
                cell = f"{value:.3f}" if isinstance(value, float) else str(value)
            row += cell.rjust(18)
        print(row)
    print("-" * (width + 18 * len(models)))


async def main() -> None:
    """Точка входа сравнения моделей."""
    args = parse_args()

    if not args.eval_set.exists():
        raise SystemExit(f"Эталонный набор не найден: {args.eval_set}")

    results: dict[str, dict[str, Any]] = {}
    for model in args.models:
        try:
            results[model] = await run_for_model(args, model)
        except Exception as error:  # noqa: BLE001 — один сбой не должен рушить сравнение
            print(f"!! модель {model}: прогон не удался — {error}")
            results[model] = {"error": str(error)}

    print_table(results)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f">> отчёт сохранён в {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
