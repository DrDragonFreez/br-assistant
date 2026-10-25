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

# ─────────────── Реквизиты следующих этапов ───────────────
# Перечень п. 8.2 описывает готовый документ. Но порядок подготовки —
# создание и оформление ПД, согласование, визирование, подписание,
# регистрация [п. 10.1] — ставит подпись и регистрационные данные
# после того, как проект ушёл от исполнителя: согласованный проект
# направляется на подписание, и только после подписания — на
# регистрацию [п. 5.1, 5.2], где в РК вносятся дата и номер [п. 7.1].
#
# Поэтому их отсутствие в проекте — не несоответствие, а состояние:
# автор проставить их не может и не должен, на то и оставлено место в
# модуле. Молчать о них нельзя — сотрудник не узнает, чего документу
# не хватает до отправки, — но и засчитывать в нарушения нельзя:
# иначе помощник собирает черновик по модулю организаторов и сам же
# бракует его на два критичных реквизита.
#
# Значение — пункт, устанавливающий этап, и формулировка требования.
DEFERRED_REQUISITES: dict[str, tuple[str, str]] = {
    "date": ("7.1", "Дата вносится в РК при регистрации документа"),
    "number": ("7.1", "Регистрационный номер присваивается при регистрации"),
    "signature": ("10.1", "Подписание следует за согласованием и визированием"),
}

# Названия, которыми модель называет те же реквизиты.
#
# Замечание модели о таком реквизите дублирует уже выставленную отметку
# «заполняется на следующем этапе»: один и тот же пробел оказался бы
# одновременно и сведением, и критичным нарушением. Сопоставление по
# точному названию, а не по вхождению слова: «дата ввода в
# эксплуатацию» — содержательное поле приказа, а не реквизит, и
# замечание о ней должно доходить до сотрудника как есть.
DEFERRED_FIELD_SYNONYMS: dict[str, str] = {
    "дата": "дата документа",
    "дата документа": "дата документа",
    "номер": "регистрационный номер документа",
    "номер документа": "регистрационный номер документа",
    "регистрационный номер": "регистрационный номер документа",
    "регистрационный номер документа": "регистрационный номер документа",
    "подпись": "подпись",
    "подпись документа": "подпись",
    "подпись и расшифровка": "подпись",
}

# Начала служебных слов в названии поля, которые не говорят, о чём
# требование.
#
# Сверка замечания с пунктом идёт по смысловым словам поля: «гриф
# утверждения» должен найти в пункте «гриф» или «утвер». Служебные
# слова встречаются почти в любом пункте инструкции и пропустили бы
# замечание, у которого с цитатой нет ничего общего.
FIELD_STOP_PREFIXES: frozenset[str] = frozenset(
    {
        "отмет",
        "рекви",
        "докум",
        "налич",
        "оформ",
        "содер",
        "текст",
        "разде",
        "пункт",
        "сведе",
        "указа",
        "требо",
        "инфор",
        "обяза",
        "отсут",
        "поле",
        "поля",
    }
)

FIELD_WORD_RE = re.compile(r"[а-яё]{4,}")


def field_stems(field_name: str) -> set[str]:
    """Выделяет основы смысловых слов из названия поля.

    Основа — первые пять букв слова (четыре у коротких слов): этого
    хватает, чтобы «исполнителе» нашло «исполнитель», а «регистрации» —
    «регистрируется», без морфологического словаря.

    Args:
        field_name: Название поля из замечания модели.

    Returns:
        Множество основ; пустое, если смысловых слов в названии нет.
    """
    stems: set[str] = set()
    for word in FIELD_WORD_RE.findall(field_name.lower()):
        if word[:5] in FIELD_STOP_PREFIXES:
            continue
        stems.add(word[:5] if len(word) >= 6 else word[:4])
    return stems


def claim_grounded_in_clause(
    field_name: str, clause: str, chunks: list[Any], problem: str = ""
) -> bool:
    """Проверяет, что процитированный пункт действительно говорит о поле.

    Ссылка на существующий пункт ещё не делает замечание верным. Модель
    берёт из пункта условие и приписывает ему чужое требование: п. 10.12
    требует пояснительную записку к приказам объёмом более трёх
    страниц, а в отчёт попадало «отсутствует гриф утверждения —
    обязателен для приказов более трёх страниц (п. 10.12)». Пункт
    существует, проверка ссылки такое пропускает, но требование выдумано.

    Правило детерминированное: хотя бы одна основа смыслового слова
    поля должна встретиться в тексте процитированного пункта. Модель
    иногда пишет в поле номер пункта («пункт 9.5») — тогда сверяется
    формулировка проблемы. Замечания без пункта или без смысловых слов
    ни в поле, ни в проблеме не отсеиваются — сверять их не с чем.

    Args:
        field_name: Название поля из замечания модели.
        clause: Номер пункта или «Приложение N» из замечания.
        chunks: Фрагменты, переданные модели в контекст.
        problem: Формулировка проблемы из замечания модели.

    Returns:
        ``True``, если замечание опирается на текст пункта или сверить
        его не с чем.
    """
    stems = field_stems(field_name) or field_stems(problem)
    if not clause or not stems:
        return True

    texts: list[str] = []
    for chunk in chunks:
        numbers = set(getattr(chunk, "clause_numbers", None) or [])
        if chunk.clause_number:
            numbers.add(chunk.clause_number)
        if chunk.annex_number:
            numbers.add(f"Приложение {chunk.annex_number}")
        if any(number == clause or number.startswith(f"{clause}.") for number in numbers):
            texts.append(chunk.content.lower())

    # Пункта нет среди фрагментов — это решает проверка ссылки, а не
    # сверка содержания.
    if not texts:
        return True
    joined = " ".join(texts)
    return any(stem in joined for stem in stems)

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

        deferred = DEFERRED_REQUISITES.get(key)
        if deferred is not None:
            stage_clause, stage_requirement = deferred
            violations.append(
                Violation(
                    severity="info",
                    field_name=label,
                    problem=(
                        f"Реквизит «{label}» не заполнен. Для проекта документа "
                        "это нормально: он появляется на следующем этапе."
                    ),
                    requirement=(
                        f"{stage_requirement} [п. {stage_clause}]. "
                        f"Сам реквизит установлен перечнем [п. {clause}]."
                    ),
                    clause=stage_clause,
                    recommendation=(
                        f"Оставьте место под реквизит «{label}» и не заполняйте "
                        f"его самостоятельно [п. {stage_clause}]."
                    ),
                )
            )
            continue

        severity = "critical" if key == "addressee" else "major"
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


# Незаполненные поля модуля.
#
# Шаблон оставляет поля docxtpl вида «{{ start_date }}», исходный модуль
# организаторов — прочерки под значение. В собранном документе нет ни
# того, ни другого. Без этого правила пустой модуль проходил проверку
# как соответствующий: дата и подпись в проекте законно отмечаются
# сведением о следующем этапе, а других формальных признаков пустоты
# проверка не знала.
TEMPLATE_FIELD_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")
BLANK_LINE_RE = re.compile(r"_{3,}")

# Служебная зона даты и номера — «___» ________ 20___ г. № ____ —
# сохраняется по методическим рекомендациям [п. 3.3] и заполняется при
# регистрации, поэтому её прочерки пропуском поля не считаются.
DATE_ZONE_RE = re.compile(
    r"«\s*_{2,}\s*»\s*_{3,}\s*20_*\s*г\.?(?:\s*№\s*_{2,})?|№\s*_{2,}"
)

UNFILLED_CLAUSE = "3.2"
UNFILLED_REQUIREMENT = (
    "Ответственный исполнитель заполняет поля модуля, внося конкретные "
    "наименования, даты, номера, должности, фамилии, сроки и основания, "
    "и после заполнения удаляет линии и прочерки (Методические рекомендации "
    "по работе с модульными документами, п. 3.2, 3.3)"
)


def check_unfilled_fields(text: str) -> list[Violation]:
    """Ищет незаполненные поля модуля в проекте документа.

    Args:
        text: Текст документа.

    Returns:
        Одно критичное замечание с числом пропусков либо пустой список.
    """
    template_fields = TEMPLATE_FIELD_RE.findall(text)
    blanks = BLANK_LINE_RE.findall(DATE_ZONE_RE.sub(" ", text))
    total = len(template_fields) + len(blanks)
    if not total:
        return []

    examples = list(dict.fromkeys(item.strip() for item in template_fields))[:3]
    shown = f": {', '.join(examples)}" if examples else ""
    return [
        Violation(
            severity="critical",
            field_name="поля модуля",
            problem=(
                f"В проекте остались незаполненные поля модуля ({total}){shown}."
            ),
            requirement=UNFILLED_REQUIREMENT + ".",
            clause=UNFILLED_CLAUSE,
            recommendation=(
                "Заполните все поля конкретными значениями и удалите прочерки, "
                "сохранив служебную зону для даты и регистрационного номера."
            ),
        )
    ]


# Служебные элементы модуля, которые исполнитель удаляет после
# заполнения [Методические рекомендации, п. 3.3]. Все они есть в каждом
# из семи модулей организаторов и отсутствуют в собранных черновиках.
# Гриф «УТВЕРЖДАЮ» относится к самому модулю: у приказа и распоряжения
# собственного грифа утверждения нет, поэтому для них он — остаток бланка.
TEMPLATE_LEFTOVERS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("пометка «МОДУЛЬНЫЙ ДОКУМЕНТ»", re.compile(r"МОДУЛЬН\w*\s+ДОКУМЕНТ"), False),
    ("гриф утверждения модуля «УТВЕРЖДАЮ»", re.compile(r"\bУТВЕРЖДАЮ\b"), True),
    (
        "блок «Примечания» с пояснениями к модулю",
        re.compile(
            r"(?m)^\s*Примечания(?:\s+по\s+заполнению(?:\s+модульного\s+\w+)?)?\s*:?\s*$"
        ),
        False,
    ),
    ("номера примечаний вида [1]", re.compile(r"\[\d{1,2}\]"), False),
    (
        "невыбранные варианты в квадратных скобках",
        re.compile(r"\[[^\]\n]{1,60}\s/\s[^\]\n]{1,60}\]"),
        False,
    ),
    (
        "окончания на выбор в скобках вида «(ых)»",
        re.compile(r"[а-яё]\((?:ых|ого|ов|ая|ые|ий|ей)\)"),
        False,
    ),
)


def check_template_leftovers(text: str, doc_kind: str) -> list[Violation]:
    """Ищет служебные элементы модуля, оставшиеся в проекте.

    Args:
        text: Текст документа.
        doc_kind: Определённый вид документа.

    Returns:
        Одно замечание с перечнем найденных элементов либо пустой список.
    """
    # Поля docxtpl проверяет правило незаполненных полей; индексы внутри
    # них («{{ rows[0] }}») номерами примечаний не являются.
    cleaned = TEMPLATE_FIELD_RE.sub(" ", text)
    found = [
        label
        for label, pattern, order_only in TEMPLATE_LEFTOVERS
        if (not order_only or doc_kind == "order") and pattern.search(cleaned)
    ]
    if not found:
        return []

    return [
        Violation(
            severity="major",
            field_name="элементы шаблона",
            problem=f"В проекте остались служебные элементы модуля: {'; '.join(found)}.",
            requirement=(
                "После заполнения ответственный исполнитель удаляет линии, "
                "неиспользованные варианты, квадратные скобки, номера примечаний "
                "и пояснения, гриф утверждения вверху первой страницы и пометку "
                "«МОДУЛЬНЫЙ ДОКУМЕНТ» (Методические рекомендации по работе с "
                "модульными документами, п. 3.3)."
            ),
            clause="3.3",
            recommendation=(
                "Удалите перечисленные элементы, сохранив служебные зоны для даты, "
                "регистрационного номера и электронной подписи."
            ),
        )
    ]


# Порядок действий после проверки — для приказа и распоряжения.
#
# Сценарий организаторов №3: после проверки сотрудник спрашивает «что
# делать дальше». Порядок установлен инструкцией явно, поэтому он
# собирается детерминированно, а не моделью: номера пунктов и сроки
# сверены с текстом [п. 10.1–10.6, 7.1]. Для писем и записок порядок в
# инструкции так не расписан, и достраивать его нельзя.
ORDER_NEXT_STEPS: tuple[tuple[str, str], ...] = (
    (
        "10.2",
        "Согласуйте проект внутри своего подразделения и одновременно "
        "направьте его во все заинтересованные подразделения, включая "
        "подразделения обязательного визирования по Матрице, с отметкой "
        "«Первичное рассмотрение».",
    ),
    (
        "10.3",
        "Учитывайте срок рассмотрения в каждом подразделении: приказ по "
        "основной деятельности — до 10 рабочих дней, распоряжение — до 3 "
        "рабочих дней.",
    ),
    (
        "10.5",
        "Замечания сведите в таблицу: автор, содержание, позиция "
        "исполнителя, категория «принято», «частично принято» или "
        "«отклонено» и обоснование. При повторном рассмотрении таблица "
        "обязательна.",
    ),
    (
        "10.6",
        "Проведите визирование в установленной последовательности: "
        "исполнитель, разработчики, руководитель подразделения, "
        "заинтересованные подразделения, подразделения обязательного "
        "визирования.",
    ),
    (
        "10.1",
        "После визирования — лингвистическая экспертиза, визирование "
        "координирующим должностным лицом и подписание.",
    ),
    (
        "7.1",
        "Регистрация — не позднее рабочего дня, следующего за днём "
        "подписания: в РК вносятся дата и номер.",
    ),
)


def next_steps_for(doc_kind: str, verdict: str) -> list[dict[str, Any]]:
    """Составляет порядок действий после проверки проекта.

    Args:
        doc_kind: Определённый вид документа.
        verdict: Вердикт проверки.

    Returns:
        Нумерованные шаги со ссылками на пункты инструкции; пустой
        список для видов, порядок которых инструкция не расписывает.
    """
    if doc_kind != "order":
        return []

    steps: list[tuple[str, str]] = []
    if verdict not in {"compliant", "pending", "failed"}:
        steps.append(("", "Устраните замечания из порядка доработки и проверьте проект повторно."))
    steps.extend(ORDER_NEXT_STEPS)
    return [
        {"step": index + 1, "action": action, "clause": clause}
        for index, (clause, action) in enumerate(steps)
    ]


# Короче этого порога телеграфная формулировка модели («отсутствует»,
# «не заполнено») без названия поля непонятна.
TERSE_PROBLEM_LENGTH = 24


def _as_sentence(text: str) -> str:
    """Приводит фразу к виду предложения.

    Args:
        text: Исходная фраза.

    Returns:
        Фраза с заглавной буквы и с точкой на конце.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""
    cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned[-1] in ".!?" else f"{cleaned}."


def describe_violation(field_name: str, problem: str) -> str:
    """Формулирует замечание так, чтобы его можно было прочитать отдельно.

    Модель отвечает телеграфно: поле «виза», проблема «отсутствует».
    В перечне замечаний такая строка не несёт смысла без заголовка
    столбца, поэтому название поля переносится в саму формулировку.

    Args:
        field_name: Элемент документа, к которому относится замечание.
        problem: Формулировка модели.

    Returns:
        Замечание целой фразой.
    """
    problem = problem.strip()
    field_name = field_name.strip()
    if not field_name or len(problem) > TERSE_PROBLEM_LENGTH:
        return _as_sentence(problem)
    return _as_sentence(f"{field_name}: {problem}")


def compute_verdict(violations: list[Violation]) -> tuple[str, float]:
    """Вычисляет итоговый вердикт и оценку соответствия.

    Args:
        violations: Выявленные несоответствия.

    Returns:
        Кортеж ``(вердикт, оценка 0–1)``.
    """
    # Отметки уровня «info» — сведения о следующих этапах жизненного
    # цикла, а не несоответствия: ни на вердикт, ни на оценку они не
    # влияют. Иначе правильно оформленный проект терял бы по четверти
    # балла за то, что его ещё не подписали.
    counted = [item for item in violations if item.severity != "info"]
    if not counted:
        return "compliant", 1.0

    weights = {"critical": 0.25, "major": 0.1, "minor": 0.03}
    penalty = sum(weights.get(item.severity, 0.05) for item in counted)
    score = max(0.0, 1.0 - penalty)

    if any(item.severity == "critical" for item in counted):
        verdict = "major_issues"
    elif sum(1 for item in counted if item.severity == "major") >= 3:
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
        violations = [
            *check_formal_requisites(document_text),
            *check_unfilled_fields(document_text),
            *check_template_leftovers(document_text, detected_kind),
        ]
        checked_rules = (
            len(REQUISITE_PATTERNS) + 2 + len(TEMPLATE_LEFTOVERS) + len(STYLE_CHECKS)
        )

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
        model_verdict = ""

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

            # Реквизиты, уже отмеченные как появляющиеся позже.
            deferred_labels = {
                item.field_name for item in violations if item.severity == "info"
            }

            discarded_claims = 0
            if isinstance(raw_violations, list):
                for item in raw_violations[:12]:
                    if not isinstance(item, dict) or not item.get("problem"):
                        continue
                    raw_field = str(item.get("field", ""))
                    normalized_field = raw_field.strip().strip("«»\"' .:").lower()
                    if DEFERRED_FIELD_SYNONYMS.get(normalized_field) in deferred_labels:
                        logger.info("validator_claim_deferred", field=normalized_field)
                        discarded_claims += 1
                        continue
                    clause = str(item.get("clause", "")).strip()
                    if clause and clause not in allowed_clauses:
                        if not any(c.startswith(f"{clause}.") for c in allowed_clauses):
                            logger.info("validator_claim_dropped", clause=clause)
                            discarded_claims += 1
                            continue
                    if not claim_grounded_in_clause(
                        raw_field, clause, chunks, str(item["problem"])
                    ):
                        logger.info("validator_claim_ungrounded", clause=clause)
                        discarded_claims += 1
                        continue
                    severity = str(item.get("severity", "minor"))
                    llm_violations.append(
                        Violation(
                            severity=(
                                severity
                                if severity in {"critical", "major", "minor"}
                                else "minor"
                            ),
                            field_name=raw_field or "содержание",
                            problem=describe_violation(raw_field, str(item["problem"])),
                            requirement=_as_sentence(
                                str(item.get("requirement", ""))
                            ),
                            clause=clause,
                            recommendation=_as_sentence(
                                str(item.get("recommendation", ""))
                            ),
                        )
                    )
            checked_rules += len(chunks)

            # Итог модель пишет по своему перечню замечаний. Если часть
            # его отброшена, итог пересказывает уже не тот отчёт, что
            # видит сотрудник: «отсутствуют обязательные реквизиты» при
            # вердикте «соответствует» — и повторяет отброшенную выдумку.
            # Тогда итог собирается ниже по оставшимся замечаниям.
            if discarded_claims:
                summary = ""
            model_verdict = str(payload.get("verdict", ""))

        all_violations = [*violations, *llm_violations]
        verdict, score = compute_verdict(all_violations)

        # Модель пишет итог, не видя формальных замечаний: незаполненные
        # поля модуля дают «существенные замечания», а итог модели
        # говорит «соответствует основным требованиям». Итог модели
        # остаётся, только если её вердикт совпал с вычисленным.
        if summary and model_verdict != verdict:
            summary = ""

        # Порядок доработки — перечень действий сотрудника. Реквизиты
        # следующих этапов в него не входят: делать с ними ничего не
        # нужно, а пункт «ничего не делайте» в списке дел только
        # отвлекает от настоящих замечаний.
        to_fix = [item for item in all_violations if item.severity != "info"]
        recommendations = [
            {
                "priority": index + 1,
                "severity": item.severity,
                "action": item.recommendation or f"Устраните замечание: {item.problem}",
                "clause": item.clause,
            }
            for index, item in enumerate(
                sorted(
                    to_fix,
                    key=lambda v: {"critical": 0, "major": 1, "minor": 2}.get(v.severity, 3),
                )
            )
        ]

        if not summary:
            deferred = len(all_violations) - len(to_fix)
            if verdict == "compliant":
                summary = "Документ соответствует проверенным требованиям инструкции."
                if deferred:
                    summary += (
                        f" Реквизиты, которые заполняются при подписании и "
                        f"регистрации, отмечены отдельно: {deferred}."
                    )
            else:
                critical = sum(1 for i in to_fix if i.severity == "critical")
                summary = (
                    f"Выявлено несоответствий: {len(to_fix)}, "
                    f"из них критичных: {critical}. "
                    "Документ требует доработки перед направлением на согласование."
                )

        documents_validated_total.labels(verdict=verdict).inc()
        document_violations_found.observe(len(to_fix))

        logger.info(
            "document_validated",
            verdict=verdict,
            violations=len(to_fix),
            deferred=len(all_violations) - len(to_fix),
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
