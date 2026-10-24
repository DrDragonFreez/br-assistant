"""Проверка проектов документов на соответствие требованиям инструкции.

Дополнительная функция со слайда 5: сотрудник загружает черновик, система
выявляет несоответствия и выдаёт рекомендации по доработке.

Проверка двухслойная, по той же логике, что и критик:

1. **Формальные правила** — наличие обязательных реквизитов, их порядок,
   объём текста. Работают детерминированно и быстро, не зависят от модели.
2. **Смысловая проверка моделью** — сверка содержания документа с найденными
   в инструкции требованиями.

Каждое замечание обязательно сопровождается ссылкой на пункт инструкции:
рекомендация без основания бесполезна сотруднику и не защитит его при
возврате документа на доработку.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.core.metrics import document_violations_found, documents_validated_total
from app.rag.generator import get_generator
from app.rag.prompts import build_validator_prompt
from app.rag.retriever import get_retriever

logger = get_logger(__name__)

# ─────────────────── Формальные правила ───────────────────
# Проверяемые реквизиты: выражение для поиска, название и пункт Инструкции.
#
# Номер пункта обязателен. Замечание «не обнаружен обязательный реквизит»
# без ссылки сотруднику нечем проверить, а выдумывать номер нельзя —
# поэтому он взят из текста: перечень реквизитов установлен п. 8.2,
# общее правило их применения — п. 8.1. Названия реквизитов приведены
# к формулировкам самого перечня.
REQUISITE_PATTERNS: dict[str, tuple[str, str, str]] = {
    "date": (
        r"\b\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)"
        r"\w*\s+\d{4}|\b\d{2}\.\d{2}\.\d{4}\b",
        "дата документа",
        "8.2",
    ),
    "number": (r"№\s*[\d\-/]+|\bрег\w*\s*(номер|№)", "регистрационный номер документа", "8.2"),
    "addressee": (
        r"\b(?:кому|адресат|начальник|руководител|директор|департамент|управлени)\w*",
        "адресат",
        "8.2",
    ),
    "signature": (
        r"\b(подпись|_{3,}|И\.?О\.?\s*Фамили|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+)\b",
        "подпись",
        "8.2",
    ),
    "title": (r"\b(о|об)\s+[а-яё]{4,}", "заголовок документа", "8.2"),
}

# Требование, на которое ссылаются замечания о реквизитах.
REQUISITE_REQUIREMENT = (
    "Реквизиты применяются сообразно виду документа независимо от носителя "
    "[п. 8.1]; перечень реквизитов установлен [п. 8.2]."
)

STYLE_CHECKS: tuple[tuple[str, str, str], ...] = (
    (r"\s{3,}", "лишние пробелы", "minor"),
    (r"[А-ЯЁ]{20,}", "избыточное выделение прописными буквами", "minor"),
    (r"!{2,}|\?{2,}", "недопустимая пунктуация для официального документа", "minor"),
    (
        r"\b(короче|типа|вроде бы|как бы|в общем)\b",
        "разговорные обороты в официальном документе",
        "major",
    ),
)

MIN_BODY_LENGTH = 150


@dataclass(slots=True)
class Violation:
    """Выявленное несоответствие требованиям."""

    severity: str
    field_name: str
    problem: str
    requirement: str = ""
    clause: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Сериализует замечание для хранения и передачи на фронтенд."""
        return {
            "severity": self.severity,
            "field": self.field_name,
            "problem": self.problem,
            "requirement": self.requirement,
            "clause": self.clause,
            "recommendation": self.recommendation,
        }


@dataclass(slots=True)
class ValidationResult:
    """Итог проверки документа."""

    verdict: str
    score: float
    violations: list[Violation] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    checked_rules: int = 0
    summary: str = ""
    detected_doc_kind: str = ""

    @property
    def critical_count(self) -> int:
        """Количество критичных нарушений."""
        return sum(1 for item in self.violations if item.severity == "critical")

    def to_dict(self) -> dict[str, Any]:
        """Сериализует результат проверки."""
        return {
            "verdict": self.verdict,
            "score": round(self.score, 3),
            "violations": [item.to_dict() for item in self.violations],
            "recommendations": self.recommendations,
            "citations": self.citations,
            "checked_rules": self.checked_rules,
            "summary": self.summary,
            "detected_doc_kind": self.detected_doc_kind,
            "critical_count": self.critical_count,
        }


def detect_document_kind(text: str) -> str:
    """Определяет тип загруженного документа по содержанию.

    Args:
        text: Текст документа.

    Returns:
        Код типа документа или пустая строка.
    """
    head = text[:1500].lower()
    patterns: dict[str, str] = {
        "order": r"\bприказыва[ею]|\bприказ\b|\bраспоряжени",
        "memo": r"\bслужебная\s+записка|\bдокладная\s+записка",
        "protocol": r"\bпротокол\b|\bприсутствовали\b|\bповестка\b",
        "letter": r"\bуважаем|\bпросим\s+вас|\bнаправля[ею]м",
    }
    for kind, pattern in patterns.items():
        if re.search(pattern, head, re.IGNORECASE):
            return kind
    return ""


def check_formal_requisites(text: str) -> list[Violation]:
    """Проверяет наличие обязательных реквизитов.

    Args:
        text: Текст документа.

    Returns:
        Список выявленных несоответствий.
    """
    violations: list[Violation] = []

    for key, (pattern, label, clause) in REQUISITE_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            continue
        severity = "critical" if key in {"date", "signature", "addressee"} else "major"
        violations.append(
            Violation(
                severity=severity,
                field_name=label,
                problem=f"В документе не обнаружен обязательный реквизит: {label}.",
                requirement=REQUISITE_REQUIREMENT,
                clause=clause,
                recommendation=f"Добавьте реквизит «{label}» [п. {clause}].",
            )
        )

    body_length = len(re.sub(r"\s+", " ", text).strip())
    if body_length < MIN_BODY_LENGTH:
        violations.append(
            Violation(
                severity="major",
                field_name="содержательная часть",
                problem=f"Текст документа слишком короткий ({body_length} знаков).",
                recommendation="Изложите суть обращения или распоряжения развёрнуто.",
            )
        )

    for pattern, problem, severity in STYLE_CHECKS:
        if re.search(pattern, text, re.IGNORECASE):
            violations.append(
                Violation(
                    severity=severity,
                    field_name="оформление",
                    problem=problem.capitalize() + ".",
                    recommendation="Приведите текст к официально-деловому стилю.",
                )
            )

    return violations


def compute_verdict(violations: list[Violation]) -> tuple[str, float]:
    """Вычисляет итоговый вердикт и оценку соответствия.

    Args:
        violations: Выявленные несоответствия.

    Returns:
        Кортеж ``(вердикт, оценка 0–1)``.
    """
    if not violations:
        return "compliant", 1.0

    weights = {"critical": 0.25, "major": 0.1, "minor": 0.03}
    penalty = sum(weights.get(item.severity, 0.05) for item in violations)
    score = max(0.0, 1.0 - penalty)

    if any(item.severity == "critical" for item in violations):
        verdict = "major_issues"
    elif sum(1 for item in violations if item.severity == "major") >= 3:
        verdict = "major_issues"
    else:
        verdict = "minor_issues"

    return verdict, score


class DocumentValidator:
    """Проверка проектов документов на соответствие инструкции."""

    async def validate(
        self,
        *,
        document_text: str,
        allowed_levels: list[str],
        doc_kind: str = "",
    ) -> ValidationResult:
        """Проверяет документ и формирует рекомендации по доработке.

        Args:
            document_text: Текст проверяемого документа.
            allowed_levels: Грифы, доступные роли пользователя.
            doc_kind: Тип документа; определяется автоматически, если не задан.

        Returns:
            Результат проверки со ссылками на пункты инструкции.
        """
        detected_kind = doc_kind or detect_document_kind(document_text)
        violations = check_formal_requisites(document_text)
        checked_rules = len(REQUISITE_PATTERNS) + 1 + len(STYLE_CHECKS)

        kind_labels = {
            "order": "приказ",
            "memo": "служебная записка",
            "protocol": "протокол совещания",
            "letter": "деловое письмо",
        }
        search_query = (
            f"требования к оформлению документа {kind_labels.get(detected_kind, '')} "
            "обязательные реквизиты состав согласование"
        ).strip()

        try:
            chunks = await get_retriever().search(
                search_query, allowed_levels=allowed_levels, top_k=12
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("validator_retrieval_failed", error=str(exc))
            chunks = []

        citations = [chunk.to_payload() for chunk in chunks[:6]]
        llm_violations: list[Violation] = []
        summary = ""

        if chunks:
            system, user = build_validator_prompt(document_text, detected_kind, chunks)
            try:
                payload = await get_generator().generate_json(system, user, stage="validator")
            except Exception as exc:  # noqa: BLE001
                logger.warning("validator_llm_failed", error=str(exc))
                payload = {}

            summary = str(payload.get("summary", ""))
            raw_violations = payload.get("violations", [])
            allowed_clauses = {
                chunk.clause_number for chunk in chunks if chunk.clause_number
            } | {
                f"Приложение {chunk.annex_number}" for chunk in chunks if chunk.annex_number
            }

            if isinstance(raw_violations, list):
                for item in raw_violations[:12]:
                    if not isinstance(item, dict) or not item.get("problem"):
                        continue
                    clause = str(item.get("clause", "")).strip()
                    if clause and clause not in allowed_clauses:
                        if not any(c.startswith(f"{clause}.") for c in allowed_clauses):
                            logger.info("validator_claim_dropped", clause=clause)
                            continue
                    severity = str(item.get("severity", "minor"))
                    llm_violations.append(
                        Violation(
                            severity=(
                                severity
                                if severity in {"critical", "major", "minor"}
                                else "minor"
                            ),
                            field_name=str(item.get("field", "содержание")),
                            problem=str(item["problem"]),
                            requirement=str(item.get("requirement", "")),
                            clause=clause,
                            recommendation=str(item.get("recommendation", "")),
                        )
                    )
            checked_rules += len(chunks)

        all_violations = [*violations, *llm_violations]
        verdict, score = compute_verdict(all_violations)

        recommendations = [
            {
                "priority": index + 1,
                "severity": item.severity,
                "action": item.recommendation or f"Устраните замечание: {item.problem}",
                "clause": item.clause,
            }
            for index, item in enumerate(
                sorted(
                    all_violations,
                    key=lambda v: {"critical": 0, "major": 1, "minor": 2}.get(v.severity, 3),
                )
            )
        ]

        if not summary:
            if verdict == "compliant":
                summary = "Документ соответствует проверенным требованиям инструкции."
            else:
                critical = sum(1 for i in all_violations if i.severity == "critical")
                summary = (
                    f"Выявлено несоответствий: {len(all_violations)}, "
                    f"из них критичных: {critical}. "
                    "Документ требует доработки перед направлением на согласование."
                )

        documents_validated_total.labels(verdict=verdict).inc()
        document_violations_found.observe(len(all_violations))

        logger.info(
            "document_validated",
            verdict=verdict,
            violations=len(all_violations),
            doc_kind=detected_kind,
        )

        return ValidationResult(
            verdict=verdict,
            score=score,
            violations=all_violations,
            recommendations=recommendations,
            citations=citations,
            checked_rules=checked_rules,
            summary=summary,
            detected_doc_kind=detected_kind,
        )


_validator: DocumentValidator | None = None


def get_validator() -> DocumentValidator:
    """Возвращает singleton-валидатор документов.

    Returns:
        Общий экземпляр :class:`DocumentValidator`.
    """
    global _validator
    if _validator is None:
        _validator = DocumentValidator()
    return _validator
