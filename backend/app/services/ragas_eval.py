"""Offline-оценка качества ответов.

Считаются две группы метрик:

* **RAGAS** — faithfulness, answer_relevancy, context_precision, context_recall.
  Требуют установленного пакета ``ragas`` (см. requirements-eval.txt).
* **Собственные метрики** — точность цитирования и доля честных отказов.
  Считаются всегда, не зависят от внешних пакетов и напрямую отражают
  критерий №1: ответ должен ссылаться на правильные пункты и не выдумывать
  сведения.

Собственные метрики важнее для защиты: RAGAS оценивает ответ в целом, а
citation_accuracy проверяет ровно то, что требует задание — попадание в нужный
пункт инструкции.

Запуск: ``python -m app.services.ragas_eval --eval-set data/eval_set/golden.jsonl``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.graph import run_assistant
from app.agents.state import AnswerStatus
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.rbac import Role, allowed_access_levels

logger = get_logger(__name__)


@dataclass(slots=True)
class EvalCase:
    """Эталонный вопрос для оценки.

    Attributes:
        question: Вопрос сотрудника.
        ground_truth: Эталонный ответ.
        expected_clauses: Пункты инструкции, которые должны быть процитированы.
        expect_no_answer: Ожидается ли честный отказ (вопрос вне базы знаний).
        category: Категория вопроса для разбивки результатов.
    """

    question: str
    ground_truth: str = ""
    expected_clauses: list[str] = field(default_factory=list)
    expect_no_answer: bool = False
    category: str = "general"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalCase:
        """Создаёт эталонный случай из записи JSONL.

        Args:
            data: Запись из файла эталонного набора.

        Returns:
            Заполненный :class:`EvalCase`.
        """
        return cls(
            question=str(data["question"]),
            ground_truth=str(data.get("ground_truth", "")),
            expected_clauses=[str(item) for item in data.get("expected_clauses", [])],
            expect_no_answer=bool(data.get("expect_no_answer", False)),
            category=str(data.get("category", "general")),
        )


@dataclass(slots=True)
class CaseResult:
    """Результат обработки одного эталонного вопроса."""

    case: EvalCase
    answer: str
    answer_status: str
    cited_clauses: list[str]
    contexts: list[str]
    top_score: float
    latency_ms: int
    critic_verdict: str

    @property
    def citation_hit(self) -> bool:
        """True, если хотя бы один ожидаемый пункт процитирован."""
        if not self.case.expected_clauses:
            return True
        return bool(set(self.case.expected_clauses) & set(self.cited_clauses))

    @property
    def citation_precision(self) -> float:
        """Доля ожидаемых пунктов, попавших в ответ."""
        if not self.case.expected_clauses:
            return 1.0
        matched = len(set(self.case.expected_clauses) & set(self.cited_clauses))
        return matched / len(self.case.expected_clauses)

    @property
    def no_answer_correct(self) -> bool:
        """True, если поведение при отсутствии данных корректно.

        Проверяется в обе стороны: отказ там, где ответа быть не должно,
        и наличие ответа там, где он есть в базе знаний.
        """
        declined = self.answer_status == AnswerStatus.NO_CONTEXT.value
        return declined == self.case.expect_no_answer


def load_eval_set(path: str | Path) -> list[EvalCase]:
    """Загружает эталонный набор вопросов из JSONL.

    Args:
        path: Путь к файлу.

    Returns:
        Список эталонных случаев.

    Raises:
        FileNotFoundError: Файл отсутствует.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Эталонный набор не найден: {file_path}")

    cases: list[EvalCase] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            cases.append(EvalCase.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("eval_case_skipped", error=str(exc))

    logger.info("eval_set_loaded", cases=len(cases), path=str(file_path))
    return cases


async def run_case(case: EvalCase, *, user_role: str = Role.METHODOLOGIST.value) -> CaseResult:
    """Прогоняет один эталонный вопрос через граф.

    Args:
        case: Эталонный случай.
        user_role: Роль, от имени которой задаётся вопрос.

    Returns:
        Результат обработки.
    """
    state = await run_assistant(
        question=case.question,
        session_id=f"eval-{int(time.time() * 1000)}",
        user_id="eval-runner",
        user_role=user_role,
        allowed_levels=allowed_access_levels(user_role),
        history=[],
        request_id="eval",
    )

    citations = state.get("citations", [])

    cited_clauses: list[str] = []
    for item in citations:
        numbers = item.get("clause_numbers") or (
            [item["clause_number"]] if item.get("clause_number") else []
        )
        cited_clauses.extend(str(number) for number in numbers if number)
        if item.get("annex_number"):
            cited_clauses.append(f"Приложение {item['annex_number']}")
    contexts = [str(item.get("quote", "")) for item in citations]

    return CaseResult(
        case=case,
        answer=state.get("answer", ""),
        answer_status=state.get("answer_status", ""),
        cited_clauses=cited_clauses,
        contexts=contexts,
        top_score=float(state.get("top_score", 0.0)),
        latency_ms=int(state.get("latency_ms", 0)),
        critic_verdict=state.get("critic_verdict", ""),
    )


def compute_custom_metrics(results: list[CaseResult]) -> dict[str, float]:
    """Считает собственные метрики качества.

    Args:
        results: Результаты по всем вопросам.

    Returns:
        Словарь метрик.
    """
    if not results:
        return {}

    answered = [r for r in results if not r.case.expect_no_answer]
    total = len(results)

    return {
        "citation_hit_rate": sum(1 for r in answered if r.citation_hit) / max(len(answered), 1),
        "citation_precision": sum(r.citation_precision for r in answered)
        / max(len(answered), 1),
        "no_answer_accuracy": sum(1 for r in results if r.no_answer_correct) / total,
        "answered_rate": sum(
            1 for r in results if r.answer_status == AnswerStatus.ANSWERED.value
        )
        / total,
        "no_answer_rate": sum(
            1 for r in results if r.answer_status == AnswerStatus.NO_CONTEXT.value
        )
        / total,
        "critic_approval_rate": sum(1 for r in results if r.critic_verdict == "approved")
        / total,
        "avg_latency_ms": sum(r.latency_ms for r in results) / total,
        "avg_top_score": sum(r.top_score for r in answered) / max(len(answered), 1),
    }


async def compute_ragas_metrics(results: list[CaseResult]) -> dict[str, float]:
    """Считает метрики RAGAS.

    Оценка выполняется локальной моделью через Ollama: отправка внутренних
    регламентов во внешний API недопустима.

    Args:
        results: Результаты по вопросам.

    Returns:
        Словарь метрик; пустой, если ragas не установлен или оценка не удалась.
    """
    usable = [
        r
        for r in results
        if not r.case.expect_no_answer and r.contexts and r.case.ground_truth
    ]
    if not usable:
        logger.warning("ragas_skipped", reason="no_usable_cases")
        return {}

    try:
        from datasets import Dataset
        from langchain_ollama import ChatOllama, OllamaEmbeddings
        from ragas import evaluate
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            context_recall,
            faithfulness,
        )
    except ImportError as exc:
        logger.warning("ragas_not_installed", error=str(exc))
        return {}

    dataset = Dataset.from_dict(
        {
            "question": [r.case.question for r in usable],
            "answer": [r.answer for r in usable],
            "contexts": [r.contexts for r in usable],
            "ground_truth": [r.case.ground_truth for r in usable],
        }
    )

    try:
        judge = ChatOllama(
            base_url=settings.OLLAMA_BASE_URL,
            model=settings.OLLAMA_LLM_MODEL,
            temperature=0.0,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
        embeddings = OllamaEmbeddings(
            base_url=settings.OLLAMA_BASE_URL, model=settings.OLLAMA_EMBED_MODEL
        )
        scores = await asyncio.to_thread(
            evaluate,
            dataset,
            metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
            llm=judge,
            embeddings=embeddings,
            raise_exceptions=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("ragas_evaluation_failed", error=str(exc))
        return {}

    result = {
        key: round(float(value), 4)
        for key, value in dict(scores).items()
        if isinstance(value, (int, float))
    }
    logger.info("ragas_completed", **result)
    return result


async def run_evaluation(
    *,
    eval_set_path: str | Path,
    model_name: str = "",
    limit: int | None = None,
    skip_ragas: bool = False,
) -> dict[str, Any]:
    """Выполняет полную оценку качества.

    Args:
        eval_set_path: Путь к эталонному набору.
        model_name: Имя оцениваемой модели (для отчёта).
        limit: Ограничение числа вопросов.
        skip_ragas: Не считать метрики RAGAS (быстрый прогон).

    Returns:
        Отчёт с метриками и разбивкой по категориям.
    """
    started = time.perf_counter()
    cases = load_eval_set(eval_set_path)
    if limit:
        cases = cases[:limit]

    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        try:
            results.append(await run_case(case))
        except Exception as exc:  # noqa: BLE001 — один сбой не должен срывать прогон
            logger.error("eval_case_failed", index=index, error=str(exc))
        if index % 10 == 0:
            logger.info("eval_progress", done=index, total=len(cases))

    custom = compute_custom_metrics(results)
    ragas_scores = {} if skip_ragas else await compute_ragas_metrics(results)

    by_category: dict[str, dict[str, Any]] = {}
    categories = {r.case.category for r in results}
    for category in categories:
        subset = [r for r in results if r.case.category == category]
        by_category[category] = {
            "count": len(subset),
            **{k: round(v, 4) for k, v in compute_custom_metrics(subset).items()},
        }

    report = {
        "model": model_name or settings.OLLAMA_MODEL_ACTIVE,
        "eval_set": str(eval_set_path),
        "questions_total": len(cases),
        "questions_processed": len(results),
        "duration_seconds": round(time.perf_counter() - started, 1),
        "metrics": {**{k: round(v, 4) for k, v in custom.items()}, **ragas_scores},
        "by_category": by_category,
        "failed_cases": [
            {
                "question": r.case.question[:120],
                "expected_clauses": r.case.expected_clauses,
                "cited_clauses": r.cited_clauses,
                "status": r.answer_status,
            }
            for r in results
            if not r.citation_hit or not r.no_answer_correct
        ][:20],
    }

    logger.info("evaluation_completed", **report["metrics"])
    return report


def format_report_table(report: dict[str, Any]) -> str:
    """Форматирует отчёт в виде таблицы для презентации.

    Args:
        report: Отчёт, полученный от :func:`run_evaluation`.

    Returns:
        Готовая к вставке markdown-таблица.
    """
    labels = {
        "faithfulness": "Faithfulness (обоснованность)",
        "answer_relevancy": "Answer relevancy (релевантность ответа)",
        "context_precision": "Context precision (точность контекста)",
        "context_recall": "Context recall (полнота контекста)",
        "citation_hit_rate": "Попадание в нужный пункт",
        "citation_precision": "Точность цитирования",
        "no_answer_accuracy": "Корректность отказов",
        "critic_approval_rate": "Одобрено критиком",
        "avg_latency_ms": "Средняя задержка, мс",
    }

    lines = [
        f"### Оценка качества — {report['model']}",
        "",
        f"Вопросов: {report['questions_processed']} из {report['questions_total']} · "
        f"длительность: {report['duration_seconds']} с",
        "",
        "| Метрика | Значение |",
        "|---|---|",
    ]
    for key, label in labels.items():
        value = report["metrics"].get(key)
        if value is None:
            continue
        formatted = f"{value:.0f}" if key == "avg_latency_ms" else f"{value:.3f}"
        lines.append(f"| {label} | {formatted} |")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> None:
    """Асинхронная точка входа CLI.

    Args:
        args: Разобранные аргументы командной строки.
    """
    report = await run_evaluation(
        eval_set_path=args.eval_set,
        model_name=args.model,
        limit=args.limit,
        skip_ragas=args.skip_ragas,
    )

    output = (
        Path(args.output) if args.output else Path(settings.ARTIFACTS_DIR) / "eval_report.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(format_report_table(report))
    print()
    print(f"Полный отчёт сохранён: {output}")


def main() -> None:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(description="Offline-оценка качества ответов помощника")
    parser.add_argument(
        "--eval-set",
        default=str(Path(settings.DATA_DIR) / "eval_set" / "golden.jsonl"),
        help="Путь к эталонному набору вопросов (JSONL)",
    )
    parser.add_argument("--model", default="", help="Имя оцениваемой модели для отчёта")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число вопросов")
    parser.add_argument("--output", default="", help="Куда сохранить JSON-отчёт")
    parser.add_argument(
        "--skip-ragas",
        action="store_true",
        help="Пропустить метрики RAGAS (быстрый прогон только по собственным метрикам)",
    )
    args = parser.parse_args()

    configure_logging()
    settings.ensure_dirs()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
