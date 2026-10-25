"""Узел маршрутизации обращений.

Классификация выполняется в два слоя. Сначала работают быстрые правила: они
покрывают большинство типовых формулировок, не тратят время на вызов модели
и дают предсказуемый результат. Если правила не дали уверенного ответа,
подключается языковая модель в режиме структурированного вывода.

Такой порядок важен для 48-часового прототипа: правила отвечают за
воспроизводимость демонстрации, модель — за гибкость на произвольных запросах.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.agents.state import AssistantState, Intent
from app.core.logging import get_logger
from app.core.metrics import graph_node_errors_total, graph_node_latency_seconds
from app.rag.generator import get_generator
from app.rag.prompts import build_router_prompt
from app.rag.retriever import ANNEX_MENTION_RE, CLAUSE_MENTION_RE

logger = get_logger(__name__)

NODE_NAME = "router"

# ─────────────────────────── Быстрые правила ───────────────────────────
TEMPLATE_PATTERNS = (
    # Окончания нужны: «в бланке», «по форме», «в образце» — слово стоит
    # в падеже, и точное совпадение по границе слова его не ловило.
    # «Форма» перечисляется по гласным, иначе под шаблон попадают
    # «формирование» и «формулировка».
    r"\b(шаблон\w*|бланк\w*|форм[аыуе]\w*|образец|образц\w*)",
    # Слово «модуль» отдельно просьбой о бланке не является: в Инструкции
    # так называется утверждённая структура документа, поэтому «что такое
    # модуль документа» — вопрос о требованиях, а не заказ черновика.
    # Нужен глагол изготовления рядом; без этого ветка mentions_module
    # ниже была недостижима ровно для тех вопросов, ради которых её
    # и написали, и помощник переспрашивал тип документа.
    r"\b(подготов|соста|сформир|созда|сдела|напиш|оформ|заполни)\w*\s+"
    r"(мне\s+)?(проект\s+)?(документ\w*\s+)?(по\s+)?модул",
    r"\b(подготов|соста|сформир|созда|сдела|напиш|оформ)\w*\s+(мне\s+)?"
    r"(проект\s+)?(служебн|приказ|письм|протокол|записк|распоряж)",
    r"\bчернови[кч]\w*\s+(документа|приказа|письма|записки|распоряжения)",
    r"\bзаполни\w*\s+(шаблон|бланк|форму|модуль)",
    r"\b(нужн\w*|требуется|надо)\s+(приказ|распоряжени|служебн\w*\s+записк)",
)
CHECK_PATTERNS = (
    r"\bпровер\w+\s+(мой|этот|документ|проект|чернови|приказ|письм|записк)",
    r"\b(соответству\w+|правильно\s+ли)\s+.*(требовани|инструкц|регламент)",
    r"\bчто\s+не\s+так\s+с\s+(документ|проект|приказ|письм)",
    r"\bнайди\s+ошибки",
)

# Побуждение проверить уже существующий текст. В живой речи дополнение
# почти всегда местоимение — «проверь его», «посмотри, всё ли верно», —
# поэтому требовать рядом название вида документа нельзя. Само по себе
# правило слишком широкое («проверь, какой срок согласования» — вопрос),
# поэтому применяется только вместе с признаком готовой работы или
# названным видом документа.
CHECK_REQUEST_PATTERNS = (
    r"\b(провер|перепровер)(ь|ьте|ить|им)\b",
    r"\bпосмотр(и|ите|еть)\b",
    r"\bоцени(те)?\b",
    r"\bвычита(й|ть)\b",
    r"\bвс[её]\s+ли\s+(верно|правильно|на\s+месте)\b",
    r"\bнет\s+ли\s+(ошибок|замечаний)\b",
)

# Признак того, что документ уже составлен. Прошедшее время отличает
# «я подготовил приказ, проверь» от «подготовь приказ»: в первом случае
# документ существует и его нужно проверить, во втором — изготовить.
COMPLETED_WORK_PATTERNS = (
    r"\b(подготовил|подготовила|составил|составила|оформил|оформила|сделал|"
    r"сделала|написал|написала|сформировал|сформировала|заполнил|заполнила|"
    r"доработал|доработала|подготовлен|составлен|оформлен|заполнен)\w*",
    r"\b(мой|свой|наш)\s+(документ|приказ|проект|письм\w*|записк\w*|"
    r"распоряжени\w*|протокол)",
    r"\bготов\w*\s+(документ|приказ|проект|письм\w*|записк\w*)",
)

# Прямая просьба объяснить порядок действий. От INFO_FRAMING отличается
# тем, что перебивает побуждение изготовить документ: в «Мне нужно
# подготовить приказ. Что для этого нужно сделать?» есть и то и другое,
# но сотрудник просит объяснить порядок, а не выдать черновик немедленно.
# Бланк ему тоже нужен — он предлагается после объяснения, вместе с
# перечнем визирующих подразделений.
#
# Проверяется по всему тексту, а не от начала строки: вопрос обычно
# стоит второй фразой, после описания задачи.
HOW_TO_PROCEED_PATTERNS = (
    r"\bчто\s+(для\s+этого\s+)?(нужно|надо|требуется|необходимо)\s+"
    r"(сделать|знать|учесть|предусмотреть|подготовить|собрать)",
    r"\bчто\s+(мне\s+)?делать(\s+дальше)?\b",
    r"\bс\s+чего\s+(мне\s+)?(нужно\s+)?начать\b",
    r"\bкак\s+(это\s+)?(правильно\s+)?(сделать|оформить|организовать|провести|"
    r"действовать|поступить)\b",
    r"\bкак(ов|ова|ой)\s+(порядок|последовательность|процедура)\b",
    r"\bкакие\s+(шаги|действия|этапы)\b",
    r"\bподскажи\w*,?\s+(что|как|какой|какие)\b",
    r"\bобъясни\w*,?\s+(что|как|какой|порядок)\b",
)

# Сотрудник готовит документ названного вида. По этому признаку к
# объяснению порядка добавляется подходящий модуль и перечень
# обязательного визирования — переход от ответа к действию.
DOCUMENT_PREPARATION_PATTERNS = (
    # Нужен глагол изготовления рядом с видом документа: «подготовить
    # приказ», «составить служебную записку», «готовлю распоряжение».
    r"\b(подготов|соста|сформир|созда|оформ|напиш|готовл)\w*\s+[^.?!]{0,60}?"
    r"(приказ|распоряжени|письм|записк|протокол|положени|регламент|модул|документ)",
    # Та же связка через модальность: «мне нужно подготовить…»,
    # «надо оформить…». Без глагола правило срабатывало на «требуется
    # сопроводительное письмо» — а это вопрос об условиях, при которых
    # письмо нужно, а не о подготовке письма.
    r"\b(нужно|надо|требуется|необходимо)\s+"
    r"(подготов|соста|сформир|созда|оформ|напиш)\w*",
)
APPROVAL_PATTERNS = (
    r"\b(кто|с\s+кем|какие\s+подразделени\w+)\s+(согласу|визиру|подписыва)",
    r"\b(порядок|маршрут|схем[ау]|цепочк[ау])\s+согласовани",
    r"\bсрок\w*\s+согласовани",
    r"\bматриц[ауы]\s+согласовани",
)
SMALLTALK_PATTERNS = (
    r"^\s*(привет|здравствуй|добрый\s+(день|вечер|утро)|хай|ку)\b",
    r"^\s*(спасибо|благодарю|понятно|ясно|ок|окей)[\s!.]*$",
    r"\b(что\s+ты\s+(уме|можешь)|твои\s+возможности|как\s+ты\s+работаешь|помощь|help)\b",
)
OUT_OF_SCOPE_PATTERNS = (
    r"\b(погод[ауы]|курс\s+(валют|доллара|рубля)|ставк[ауи]\s+цб|анекдот|рецепт)\b",
    r"\b(напиши\s+(код|программу|скрипт)|реши\s+(задачу|пример)|переведи\s+на\s+английский)\b",
)

# Виды документов из материалов кейса. Порядок важен: проверка идёт сверху
# вниз, и более узкие признаки должны стоять раньше общих.
#
# Приказ и распоряжение разделены намеренно: по инструкции у них разные
# сроки рассмотрения (10 против 3 рабочих дней [п. 10.3]) и разные строки
# в Матрице обязательного визирования, поэтому объединять их в один вид
# значило бы отвечать неверно на половину вопросов о согласовании.
DOC_KIND_PATTERNS: dict[str, str] = {
    "memo": r"\bслужебн\w*\s+записк|\bдокладн\w*\s+записк|служебк",
    "direction": r"\bраспоряжени",
    "order": r"\bприказ",
    "letter": r"\b(деловое\s+|информационное\s+)?письм|исходящ\w+\s+документ",
    "protocol": r"\bпротокол|совещани",
    "regulation": r"\bположени|\bрегламент|\bпорядок\b|\bправила\b|локальн\w*\s+(норматив|акт)",
}

# Упоминание модульного документа: по нему помощник сразу ведёт к бланку
# и к требованиям главы 9 инструкции, а не к общему порядку согласования.
MODULE_PATTERNS = (
    # Окончание задаётся как \w*, а не перечислением: «модуля», «модулю»,
    # «модулем», «модулях» — набор форм в вопросах сотрудников шире, чем
    # можно выписать руками, а дательный падеж («требования к модулю
    # приказа») из-за точного перечисления уже выпадал.
    r"\bмодул\w*\s+(документ|приказ|распоряж)",
    r"\bмодульн\w+\s+(документ|приказ|распоряжени)",
    r"\bпо\s+модул\w+\b",
    r"\bреестр\w*\s+модул",
)

# Признак того, что сотрудник просит объяснить требование, а не
# изготовить документ. Слова «бланк», «форма», «модуль» встречаются в обоих
# случаях, поэтому различать их по словарю нельзя — различается
# формулировка. «Какие зоны должны быть предусмотрены в электронном бланке»
# — вопрос о пункте инструкции; «подготовь бланк» — заказ черновика.
INFO_FRAMING_PATTERNS = (
    r"^\s*(что|чем|как\w*|где|когда|кто|почему|зачем|сколько|каков\w*)\b",
    # «В какой срок…», «На основании чего…», «По какой форме…»,
    # «При каких условиях…», «Согласно какому пункту…»
    r"^\s*(в|во|на|из|при|для|с|со|до|после|об|о|по|у|за|без|через|согласно)\s+\w+",
    r"\b(можно|нужно|требуется|обязательно|допускается|применяется|предусмотрен\w*)\s+ли\b",
)

# Побуждение изготовить или выдать документ.
PRODUCTION_PATTERNS = (
    # Окончание «-ся» / «-сь» отсекается: «составляется», «оформляется»,
    # «создаётся» описывают порядок работы, а не просят его выполнить.
    # Без этого «по какой форме составляется протокол» считалось заказом
    # бланка вместо вопроса о пункте инструкции.
    r"\b(подготов|соста|сформир|созда|сдела|напиш|оформ|заполни|выдай|дай|покажи|"
    r"найди|пришли|сгенерир)\w*(?<!ся)(?<!сь)\b",
    r"\b(нужен|нужна|нужно|требуется|надо)\s+(шаблон|бланк|форм|образец|модул|"
    r"приказ|распоряж|письм|записк|протокол)",
)

# Предметная лексика Инструкции. Если она присутствует, обращение относится
# к базе знаний, даже когда модель решила иначе.
DOMAIN_VOCABULARY = (
    r"\b(документ|документооборот|документацион|инструкц|приказ|распоряжени|"
    r"протокол|письм|записк|реквизит|регистрац|согласован|визирован|подписан|"
    r"бланк|модул|номенклатур|дело|делопроизводств|архив|хранени|исполнени|"
    r"контрол|резолюц|адресат|подразделени|проект|редакц|срок|гриф|"
    r"экземпляр|копи|подлинник|журнал|реестр)\w*",
    r"\bиис\s*до\b",
)

VAGUE_PATTERNS = (
    r"^\s*\w{1,3}\s*$",
    r"^\s*(документ|приказ|письмо|записка|согласование|регламент|инструкция)\s*[?.]?\s*$",
)


# Согласие заполнить модуль вместе — ответ на вопрос узла offer
# «Заполните модуль самостоятельно… либо подготовлю проект вместе с вами.
# Как удобнее?». Без распознавания ответ «давай вместе» уходил в модель
# маршрутизатора, та относила его к болтовне, и сотрудник получал
# справку «Что я умею» вместо первого вопроса по модулю.
JOINT_FILLING_PATTERNS = (
    r"\bвместе\b",
    r"\bс\s+(тобой|вами)\b",
    r"\b(заполни|заполните|подготовь|подготовьте|собери|соберите|составь|составьте)\b",
    r"\b(давай|давайте)\s+(заполн|подготов|собер|состав)",
    r"\bпомоги(те)?\s+(заполнить|подготовить|составить|собрать)",
    r"\bвтор(ой|ое|ым)\s*(вариант)?\b",
)
# Выбор «сам» и отказ: сотрудник заполнит бланк в панели справа.
SELF_FILLING_PATTERNS = (
    r"\b(заполню|подготовлю|составлю|сделаю|соберу)\b",
    r"\bсамостоятельно\b",
    r"\bне\s+(надо|нужно)\b",
    r"^\s*нет\b",
)
# Ответ на вопрос «как удобнее» короткий; длинная реплика — уже новое
# обращение, и решать за сотрудника, что он согласился, нельзя.
JOINT_FILLING_MAX_LENGTH = 80


def accepts_joint_filling(text: str) -> bool:
    """Определяет, согласился ли сотрудник заполнить модуль вместе.

    Args:
        text: Реплика сотрудника после предложения модуля.

    Returns:
        True, если реплика — короткое согласие на совместное заполнение.
    """
    lowered = text.strip().lower()
    if not lowered or len(lowered) > JOINT_FILLING_MAX_LENGTH:
        return False
    if _matches_any(lowered, SELF_FILLING_PATTERNS):
        return False
    return _matches_any(lowered, JOINT_FILLING_PATTERNS)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    """Проверяет совпадение текста хотя бы с одним шаблоном.

    Args:
        text: Проверяемый текст в нижнем регистре.
        patterns: Регулярные выражения.

    Returns:
        True при первом совпадении.
    """
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def detect_document_kind(text: str) -> str:
    """Определяет тип документа, упомянутый в обращении.

    Args:
        text: Текст обращения.

    Returns:
        Код типа документа или пустая строка.
    """
    for kind, pattern in DOC_KIND_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return kind
    return ""


def wants_document_template(text: str, document_kind: str) -> bool:
    """Проверяет, готовит ли сотрудник документ названного вида.

    По этому признаку к разъяснению добавляется подходящий модуль и
    перечень обязательного визирования.

    Args:
        text: Текст обращения.
        document_kind: Определённый вид документа.

    Returns:
        True, если обращение относится к подготовке документа.
    """
    if not document_kind:
        return False
    return _matches_any(text.lower(), DOCUMENT_PREPARATION_PATTERNS)


def classify_by_rules(question: str) -> dict[str, Any] | None:
    """Классифицирует обращение по правилам.

    Args:
        question: Текст обращения.

    Returns:
        Результат классификации либо None, если правила не сработали
        и нужна языковая модель.
    """
    text = question.strip().lower()
    if not text:
        return {
            "intent": Intent.SMALLTALK.value,
            "needs_clarification": False,
            "missing_info": [],
            "document_kind": "",
            "mentioned_clauses": [],
            "source": "rules",
        }

    document_kind = detect_document_kind(text)
    clauses = CLAUSE_MENTION_RE.findall(question)
    annexes = ANNEX_MENTION_RE.findall(question)

    mentions_module = _matches_any(text, MODULE_PATTERNS)
    asks_for_information = _matches_any(text, INFO_FRAMING_PATTERNS) and not _matches_any(
        text, PRODUCTION_PATTERNS
    )
    asks_how_to_proceed = _matches_any(text, HOW_TO_PROCEED_PATTERNS)
    asks_to_check = _matches_any(text, CHECK_REQUEST_PATTERNS)
    mentions_completed_work = _matches_any(text, COMPLETED_WORK_PATTERNS)

    if _matches_any(text, OUT_OF_SCOPE_PATTERNS):
        intent = Intent.OUT_OF_SCOPE
    elif _matches_any(text, SMALLTALK_PATTERNS) and len(text) < 60:
        intent = Intent.SMALLTALK
    elif _matches_any(text, CHECK_PATTERNS) or (
        asks_to_check and (mentions_completed_work or document_kind)
    ):
        # Дополнение после «проверь» в живой речи — местоимение, поэтому
        # вид документа рядом с глаголом не требуется. Достаточно, что
        # документ уже составлен либо назван его вид.
        intent = Intent.DOCUMENT_CHECK
    elif asks_for_information and _matches_any(text, TEMPLATE_PATTERNS):
        # Вопрос о требованиях к бланку или модулю: отвечать нужно пунктом
        # инструкции, а не выдачей черновика. Иначе помощник на вопрос
        # «какие зоны должны быть в электронном бланке» переспрашивал тип
        # документа вместо ссылки на пункт.
        intent = Intent.QUESTION
    elif _matches_any(text, TEMPLATE_PATTERNS):
        # Просьба объяснить порядок действий важнее побуждения изготовить
        # документ: сначала разъяснение со ссылками на пункты, бланк —
        # следующим шагом. Иначе на «что для этого нужно сделать?»
        # помощник сразу спрашивал значения полей бланка.
        intent = Intent.QUESTION if asks_how_to_proceed else Intent.TEMPLATE_REQUEST
    elif _matches_any(text, APPROVAL_PATTERNS):
        intent = Intent.APPROVAL_ROUTE
    elif mentions_module:
        # «Что такое модуль документа», «где реестр модулей» — вопрос о
        # порядке работы с модульными документами, а не просьба о бланке.
        intent = Intent.QUESTION
    elif clauses or annexes:
        intent = Intent.QUESTION
    else:
        return None

    missing: list[str] = []
    needs_clarification = False

    if intent is Intent.TEMPLATE_REQUEST and not document_kind:
        needs_clarification = True
        missing.append("тип документа")

    if _matches_any(text, VAGUE_PATTERNS):
        needs_clarification = True
        missing.append("суть вопроса")

    return {
        "intent": intent.value,
        "needs_clarification": needs_clarification,
        "missing_info": missing,
        "document_kind": document_kind,
        "mentioned_clauses": [*clauses, *(f"Приложение {a}" for a in annexes)],
        "source": "rules",
    }


async def classify_by_llm(
    question: str, history: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Классифицирует обращение языковой моделью.

    Args:
        question: Текст обращения.
        history: История диалога.

    Returns:
        Результат классификации; при сбое — безопасное значение
        ``question``, чтобы обращение всё равно дошло до поиска.
    """
    system, user = build_router_prompt(question, history)
    try:
        payload = await get_generator().generate_json(system, user, stage="router")
    except Exception as exc:  # noqa: BLE001 — маршрутизация не должна ронять запрос
        logger.warning("router_llm_failed", error=str(exc))
        return {
            "intent": Intent.QUESTION.value,
            "needs_clarification": False,
            "missing_info": [],
            "document_kind": detect_document_kind(question),
            "mentioned_clauses": [],
            "source": "fallback",
        }

    raw_intent = str(payload.get("intent", "")).strip().lower()
    valid_intents = {item.value for item in Intent}
    intent = raw_intent if raw_intent in valid_intents else Intent.QUESTION.value

    # Намерения smalltalk и out_of_scope замыкают граф на статичный ответ,
    # минуя поиск. Ошибка модели здесь обходится дороже всех остальных:
    # сослаться на пункт такой ответ не может по построению, и вопрос,
    # ответ на который в базе знаний есть, остаётся без ответа. Так
    # терялись «кто отвечает за документационное обеспечение в
    # подразделении» и «на основании чего предоставляется доступ».
    #
    # Поэтому при наличии предметной лексики Инструкции обращение всё
    # равно идёт в поиск: если данных действительно нет, порог
    # релевантности даст честный отказ — исход не хуже статичного ответа,
    # а при наличии данных сотрудник получит пункт.
    if intent in {Intent.SMALLTALK.value, Intent.OUT_OF_SCOPE.value} and _matches_any(
        question.lower(), DOMAIN_VOCABULARY
    ):
        logger.info("static_branch_overridden", model_intent=intent)
        intent = Intent.QUESTION.value

    # То же различие «спрашивают о требованиях» против «просят изготовить»
    # применяется и к решению модели. Без этого вопрос «какие зоны должны
    # быть предусмотрены в электронном бланке документа» уходил в подбор
    # бланка и оставался без ссылки на пункт.
    lowered = question.lower()
    if (
        intent == Intent.TEMPLATE_REQUEST.value
        and _matches_any(lowered, INFO_FRAMING_PATTERNS)
        and not _matches_any(lowered, PRODUCTION_PATTERNS)
    ):
        logger.info("template_branch_overridden", model_intent=intent)
        intent = Intent.QUESTION.value

    missing_raw = payload.get("missing_info", [])
    missing = [str(item) for item in missing_raw] if isinstance(missing_raw, list) else []

    clauses_raw = payload.get("mentioned_clauses", [])
    clauses = [str(item) for item in clauses_raw] if isinstance(clauses_raw, list) else []

    # Уточняющий вопрос задаётся там, где без ответа сотрудника работу
    # действительно не продолжить: выбрать бланк, не зная типа документа,
    # невозможно. На вопросе о требованиях модель на 7 млрд параметров
    # сообщает о неполноте слишком охотно — на «что делать, если ИИС ДО
    # недоступна» она переспрашивала тип документа, которого в вопросе
    # нет и быть не должно. Такой встречный вопрос хуже ответа со
    # ссылкой на пункт: сотрудник не получает ничего. Поэтому вопросы
    # идут в поиск, а неполноту ловят детерминированные признаки
    # размытости и порог релевантности с честным отказом.
    model_wants_clarification = bool(payload.get("needs_clarification", False))
    blocking_intents = {Intent.TEMPLATE_REQUEST.value, Intent.DOCUMENT_CHECK.value}
    needs_clarification = model_wants_clarification and (
        intent in blocking_intents or _matches_any(question.lower(), VAGUE_PATTERNS)
    )
    if model_wants_clarification and not needs_clarification:
        logger.info("clarification_suppressed", intent=intent, missing=len(missing))

    return {
        "intent": intent,
        "needs_clarification": needs_clarification,
        "missing_info": missing if needs_clarification else [],
        "document_kind": str(payload.get("document_kind", "")) or detect_document_kind(question),
        "mentioned_clauses": clauses,
        "source": "llm",
    }


async def route_node(state: AssistantState) -> dict[str, Any]:
    """Узел графа: определяет намерение обращения.

    Args:
        state: Текущее состояние графа.

    Returns:
        Обновление состояния с намерением и признаком необходимости уточнения.
    """
    started = time.perf_counter()
    question = state.get("question", "")

    try:
        classification = classify_by_rules(question)
        if classification is None:
            classification = await classify_by_llm(question, state.get("history"))

        if state.get("attached_document_text"):
            classification["intent"] = Intent.DOCUMENT_CHECK.value
            classification["needs_clarification"] = False

        if state.get("template_code") and state.get("filled_fields") is not None:
            classification["intent"] = Intent.TEMPLATE_REQUEST.value
            classification["needs_clarification"] = False

        logger.info(
            "intent_classified",
            intent=classification["intent"],
            source=classification.get("source", ""),
            needs_clarification=classification["needs_clarification"],
        )

        return {
            "intent": classification["intent"],
            "needs_clarification": classification["needs_clarification"],
            "missing_info": classification["missing_info"],
            "document_kind": classification["document_kind"],
            "mentioned_clauses": classification.get("mentioned_clauses", []),
            "node_trace": [f"{NODE_NAME}:{classification['intent']}"],
        }
    except Exception as exc:  # noqa: BLE001
        graph_node_errors_total.labels(node=NODE_NAME, error_type=type(exc).__name__).inc()
        logger.exception("router_node_failed")
        return {
            "intent": Intent.QUESTION.value,
            "needs_clarification": False,
            "node_trace": [f"{NODE_NAME}:error"],
        }
    finally:
        graph_node_latency_seconds.labels(node=NODE_NAME).observe(time.perf_counter() - started)
