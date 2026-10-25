#!/usr/bin/env python3
"""Прогрев кеша ответов перед демонстрацией.

Генератор даёт разброс между запусками. На одном и том же вопросе он
обычно отвечает со ссылками на пункты, но примерно в одном прогоне из
нескольких критик отклоняет ответ, и сотрудник видит честный отказ
вместо разъяснения. В обычной работе это правильное поведение — лучше
отказ, чем выдумка, — но на защите с регламентом в три минуты
переспрашивать некогда.

Ответы кешируются в Redis на ``CACHE_TTL_SECONDS`` (по умолчанию час).
Скрипт задаёт каждый демонстрационный вопрос, проверяет ответ и
повторяет вопрос, если ответ оказался отказом. В кеше остаётся
последний успешный ответ, и на защите он отдаётся мгновенно и
одинаково.

Запускать за 10–15 минут до выступления::

    make warmup
    ./.venv/Scripts/python.exe scripts/warmup_demo.py --attempts 4

Логин и пароль администратора берутся из ``.env``
(``BOOTSTRAP_ADMIN_EMAIL`` и ``BOOTSTRAP_ADMIN_PASSWORD``), как и у
сквозной проверки; ключи ``--email`` и ``--password`` их перекрывают.

Зависимостей нет, только стандартная библиотека: скрипт должен
работать с хоста, где зависимости бэкенда не установлены.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

DEFAULT_BASE = "http://localhost/api/v1"
DEFAULT_EMAIL = "admin@cbr.local"


@dataclass(frozen=True)
class DemoCase:
    """Демонстрационный вопрос и ожидания к ответу."""

    title: str
    question: str
    expect_refusal: bool = False
    """Вопрос, на который правильный ответ — отказ."""
    expect_template: bool = False
    """К ответу должен быть приложен модульный документ."""


# Порядок совпадает с порядком демонстрации в docs/pitch.md.
CASES: tuple[DemoCase, ...] = (
    DemoCase(
        title="Сценарий 1 — порядок подготовки приказа и модуль",
        question=(
            "Мне нужно подготовить приказ о вводе информационной системы "
            "в эксплуатацию. Что для этого нужно сделать?"
        ),
        expect_template=True,
    ),
    DemoCase(
        title="Сценарий 3 — чего в инструкции нет",
        question=(
            "Можно ли направить внутреннюю служебную записку без сопроводительного "
            "письма? А если сопроводительное письмо необходимо, как его правильно "
            "составить?"
        ),
    ),
    DemoCase(
        title="Сценарий 4 — состав визирующих подразделений",
        question="Какие подразделения визируют приказ о вводе системы в эксплуатацию?",
    ),
    DemoCase(
        title="Сценарий 5 — проверка готового документа",
        question=(
            "Я подготовил приказ на основе модульного документа. Проверь его, "
            "пожалуйста, и подскажи, что делать дальше"
        ),
    ),
    DemoCase(
        title="Запасной — сроки согласования",
        question="В какой срок согласовывается приказ по основной деятельности?",
    ),
    DemoCase(
        title="Запасной — вопрос вне базы знаний",
        question="Какой курс доллара установлен на сегодня?",
        expect_refusal=True,
    ),
    DemoCase(
        title="Запасной — реестр замечаний",
        question=(
            "Мне нужно собрать замечания от нескольких подразделений к проекту "
            "документа. Как правильно оформить реестр замечаний?"
        ),
    ),
)

NO_ANSWER_MARK = "нет информации по этому вопросу"

# Статусы, при которых помощник сознательно не отвечает по существу:
# данных в базе знаний нет либо тема вне его компетенции. Отказ по теме
# вне компетенции выдаётся готовым текстом, без обращения к модели, и
# фразы про базу знаний в нём нет — проверять нужно статус, а не текст.
DECLINE_STATUSES = {"no_context", "info"}


@dataclass
class Report:
    """Итог прогрева."""

    good: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def read_env_file(path: Path) -> dict[str, str]:
    """Читает значения из .env без сторонних библиотек.

    Args:
        path: Путь к файлу окружения.

    Returns:
        Словарь переменных; пустой, если файла нет.
    """
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Комментарий в конце строки и кавычки вокруг значения убираются.
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def post(url: str, payload: dict[str, object], token: str = "") -> dict[str, object]:
    """Выполняет POST-запрос и разбирает ответ.

    Args:
        url: Полный адрес.
        payload: Тело запроса.
        token: Access-токен, если запрос требует авторизации.

    Returns:
        Разобранный ответ.

    Raises:
        RuntimeError: Если сервис ответил ошибкой.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=420) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise RuntimeError(f"{exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"сервис недоступен: {exc.reason}") from exc


def login(base: str, email: str, password: str) -> str:
    """Выполняет вход и возвращает access-токен.

    Args:
        base: Базовый адрес API.
        email: Адрес администратора.
        password: Пароль.

    Returns:
        Access-токен.
    """
    data = post(f"{base}/auth/login", {"email": email, "password": password})
    return str(data["access_token"])


def describe(answer: str, case: DemoCase, data: dict[str, object]) -> tuple[bool, str]:
    """Оценивает, годится ли ответ для демонстрации.

    Args:
        answer: Текст ответа.
        case: Демонстрационный вопрос.
        data: Полный ответ API.

    Returns:
        Пара «ответ годится» и пояснение.
    """
    status = str(data.get("answer_status") or "")
    refused = NO_ANSWER_MARK in answer or status in DECLINE_STATUSES
    citations = data.get("citations") or []
    templates = data.get("template_candidates") or []

    if case.expect_refusal:
        if refused:
            return True, f"отказ по существу, статус «{status}»"
        return False, "ожидался отказ, а получен содержательный ответ"

    if refused:
        return False, "генератор отказал: нужен повтор"
    if not citations:
        return False, "в ответе нет ссылок на пункты"
    if case.expect_template and not templates:
        return False, "к ответу не приложен модульный документ"

    note = f"ссылок: {len(citations)}"
    if templates:
        note += f", модуль: {templates[0].get('code', '')}"
    return True, note


def warm_case(base: str, token: str, case: DemoCase, attempts: int) -> tuple[str, str]:
    """Прогревает один вопрос, повторяя при неудачном ответе.

    Args:
        base: Базовый адрес API.
        token: Access-токен.
        case: Демонстрационный вопрос.
        attempts: Сколько попыток допустимо.

    Returns:
        Пара «итог» и пояснение. Итог — ``good``, ``weak`` или ``failed``.
    """
    last_note = ""
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            data = post(
                f"{base}/chat",
                {"message": case.question, "session_id": None, "template_code": ""},
                token=token,
            )
        except RuntimeError as exc:
            return "failed", str(exc)

        elapsed = time.monotonic() - started
        answer = str(data.get("answer") or "")
        cached = bool(data.get("from_cache"))
        ok, note = describe(answer, case, data)
        suffix = " (из кеша)" if cached else ""
        print(f"    попытка {attempt}: {elapsed:.0f} c{suffix} — {note}")

        if ok:
            return "good", note
        last_note = note
        if cached:
            # Повторять бессмысленно: кеш отдаст тот же ответ. Его нужно
            # сбросить, иначе на защите придёт он же.
            return "weak", f"{note}; в кеше лежит неудачный ответ, сбросьте кеш"

    return "weak", last_note


def main() -> int:
    """Прогревает кеш демонстрационными вопросами.

    Returns:
        Код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="Прогрев кеша перед демонстрацией")
    parser.add_argument("--base", default=DEFAULT_BASE, help="базовый адрес API")
    parser.add_argument("--email", default="", help="адрес администратора; по умолчанию из .env")
    parser.add_argument("--password", default="", help="пароль администратора; по умолчанию из .env")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="сколько раз повторять вопрос, если генератор отказал",
    )
    args = parser.parse_args()

    # Make не подгружает .env, и `make warmup` передавал пустой пароль:
    # вход падал ещё до первого вопроса. Значения берутся из файла.
    env = read_env_file(args.env_file)
    args.email = args.email or env.get("BOOTSTRAP_ADMIN_EMAIL", "") or DEFAULT_EMAIL
    args.password = args.password or env.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not args.password:
        print(f"{RED}✗{RESET} пароль не задан: укажите --password или BOOTSTRAP_ADMIN_PASSWORD в {args.env_file}")
        return 2

    print(f"Прогрев кеша: {args.base}")
    print("-" * 64)

    try:
        token = login(args.base, args.email, args.password)
    except RuntimeError as exc:
        print(f"{RED}✗{RESET} вход не выполнен — {exc}")
        return 2

    report = Report()
    for case in CASES:
        print(f"  {case.title}")
        outcome, note = warm_case(args.base, token, case, args.attempts)
        if outcome == "good":
            print(f"  {GREEN}✓{RESET} {note}\n")
            report.good.append(case.title)
        elif outcome == "weak":
            print(f"  {YELLOW}!{RESET} {note}\n")
            report.weak.append(f"{case.title} — {note}")
        else:
            print(f"  {RED}✗{RESET} {note}\n")
            report.failed.append(f"{case.title} — {note}")

    print("-" * 64)
    print(f"готовы к показу: {len(report.good)} из {len(CASES)}")

    for item in report.weak:
        print(f"{YELLOW}!{RESET} {item}")
    for item in report.failed:
        print(f"{RED}✗{RESET} {item}")

    if report.failed:
        return 1
    if report.weak:
        print()
        print(
            f"{YELLOW}!{RESET} Ответы в кеше живут час. Если до выступления "
            "больше часа, прогрейте ещё раз."
        )
        return 0

    print()
    print(f"{GREEN}Все демонстрационные вопросы прогреты{RESET}")
    print("  Кеш живёт час: если до выступления больше часа, прогрейте повторно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
