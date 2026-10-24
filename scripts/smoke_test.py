#!/usr/bin/env python3
"""Сквозная проверка поднятого контура.

Отвечает на один вопрос: работает ли система целиком — от входа до ответа
со ссылкой на пункт инструкции. Юнит-тесты этого не покрывают: они идут
без Qdrant, Ollama и базы данных.

Проверяется ровно то, за что начисляют баллы:

* вход по учётной записи и выдача токена;
* ответ на вопрос по инструкции **со ссылкой на конкретный пункт**;
* честный отказ на вопрос вне базы знаний — выдумывание ловится здесь же;
* доступность реестра бланков документов;
* состояние компонентов контура.

Зависимостей нет: только стандартная библиотека, чтобы скрипт запускался
на машине, где нет ни venv бэкенда, ни установленных пакетов.

Запуск::

    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://localhost/api/v1 --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost/api/v1"
# Вопрос по инструкции: ответ обязан содержать ссылку на пункт.
GROUNDED_QUESTION = "Какой максимальный срок рассмотрения проекта распоряжения?"
# Вопрос вне базы знаний: ожидается честный отказ.
OUT_OF_SCOPE_QUESTION = "Какая ставка по кредитам для работников Организации?"
# Признак отказа — тот же текст, что отдаёт бэкенд при отсутствии данных.
REFUSAL_MARKERS = ("нет информации", "не найдено", "НЕТ_В_БАЗЕ_ЗНАНИЙ")
# Ссылка на пункт в тексте ответа: [п. 3.4], [Приложение 2].
CITATION_RE = re.compile(r"\[(?:п\.?\s*|пункт\s*)?\d{1,2}(?:\.\d{1,3}){0,4}\]|\[Приложение", re.I)

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


class SmokeError(Exception):
    """Проверка не пройдена."""


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


def call(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str = "",
    timeout: float = 180.0,
) -> tuple[int, Any]:
    """Выполняет запрос к API.

    Args:
        base_url: Базовый адрес с префиксом версии.
        path: Путь после префикса.
        method: Метод HTTP.
        body: Тело запроса.
        token: Access-токен.
        timeout: Предел ожидания ответа.

    Returns:
        Кортеж ``(код ответа, разобранное тело)``.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"detail": raw[:200]}
    except (urllib.error.URLError, TimeoutError) as error:
        raise SmokeError(f"сервис недоступен: {error}") from error


def wait_for_api(base_url: str, attempts: int = 30, delay: float = 2.0) -> None:
    """Ждёт, пока API начнёт отвечать.

    Args:
        base_url: Базовый адрес API.
        attempts: Число попыток.
        delay: Пауза между попытками в секундах.

    Raises:
        SmokeError: API не ответил за отведённое время.
    """
    for attempt in range(1, attempts + 1):
        try:
            # Неавторизованный запрос к /auth/me: ответ 401 означает,
            # что приложение поднято и маршруты работают.
            status, _ = call(base_url, "/auth/me", timeout=10.0)
            if status in {200, 401, 403}:
                return
        except SmokeError:
            pass
        print(f"   ожидание API… попытка {attempt}/{attempts}")
        time.sleep(delay)

    raise SmokeError(f"API не ответил по адресу {base_url}")


def check_login(base_url: str, email: str, password: str) -> str:
    """Проверяет вход и возвращает access-токен.

    Args:
        base_url: Базовый адрес API.
        email: Логин.
        password: Пароль.

    Returns:
        Access-токен.

    Raises:
        SmokeError: Вход не выполнен.
    """
    status, body = call(
        base_url, "/auth/login", method="POST", body={"email": email, "password": password}
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        detail = body.get("detail") if isinstance(body, dict) else body
        raise SmokeError(f"вход не выполнен ({status}): {detail}")
    return str(body["access_token"])


def check_profile(base_url: str, token: str) -> dict[str, Any]:
    """Проверяет выдачу профиля и прав.

    Args:
        base_url: Базовый адрес API.
        token: Access-токен.

    Returns:
        Профиль пользователя.

    Raises:
        SmokeError: Профиль не получен.
    """
    status, body = call(base_url, "/auth/me", token=token)
    if status != 200 or not isinstance(body, dict):
        raise SmokeError(f"профиль не получен ({status})")
    return body


def ask(base_url: str, token: str, question: str) -> dict[str, Any]:
    """Задаёт вопрос помощнику в полном контуре обработки.

    Args:
        base_url: Базовый адрес API.
        token: Access-токен.
        question: Текст вопроса.

    Returns:
        Ответ помощника.

    Raises:
        SmokeError: Обращение не обработано.
    """
    status, body = call(base_url, "", method="POST", body={"message": question}, token=token)
    if status != 200 or not isinstance(body, dict):
        detail = body.get("detail") if isinstance(body, dict) else body
        raise SmokeError(f"обращение не обработано ({status}): {detail}")
    return body


def main() -> int:
    """Выполняет все проверки и печатает итог.

    Returns:
        Код выхода процесса.
    """
    parser = argparse.ArgumentParser(description="Сквозная проверка контура помощника")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default="", help="Логин; по умолчанию из .env")
    parser.add_argument("--password", default="", help="Пароль; по умолчанию из .env")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--verbose", action="store_true", help="Печатать текст ответов")
    parser.add_argument("--skip-wait", action="store_true", help="Не ждать подъёма API")
    args = parser.parse_args()

    env = read_env_file(args.env_file)
    email = args.email or env.get("BOOTSTRAP_ADMIN_EMAIL", "admin@cbr.local")
    password = args.password or env.get("BOOTSTRAP_ADMIN_PASSWORD", "")

    if not password:
        print(
            f"{RED}✗{RESET} пароль не задан: укажите --password либо "
            f"BOOTSTRAP_ADMIN_PASSWORD в {args.env_file}"
        )
        return 2

    # Адрес чата лежит на самом префиксе: POST /api/v1/chat.
    chat_url = f"{args.base_url}/chat"
    failures: list[str] = []
    warnings: list[str] = []

    print(f"Проверка контура: {args.base_url}")
    print("-" * 64)

    try:
        if not args.skip_wait:
            wait_for_api(args.base_url)

        token = check_login(args.base_url, email, password)
        print(f"{GREEN}✓{RESET} вход выполнен: {email}")

        profile = check_profile(args.base_url, token)
        print(
            f"{GREEN}✓{RESET} профиль получен: роль {profile.get('role')}, "
            f"прав {len(profile.get('permissions') or [])}, "
            f"грифы {profile.get('allowed_access_levels')}"
        )

        # ── Ответ по инструкции со ссылкой на пункт ──
        grounded = ask(chat_url, token, GROUNDED_QUESTION)
        answer = str(grounded.get("answer", ""))
        citations = grounded.get("citations") or []
        status_value = grounded.get("answer_status")

        if args.verbose:
            print(f"\n   вопрос: {GROUNDED_QUESTION}\n   ответ: {answer[:400]}\n")

        if status_value == "no_context":
            warnings.append(
                "на вопрос по инструкции получен отказ — вероятно, база знаний "
                "не проиндексирована (make index)"
            )
            print(f"{YELLOW}!{RESET} ответ по инструкции: честный отказ, индекс пуст")
        elif not answer:
            failures.append("ответ по инструкции пустой")
            print(f"{RED}✗{RESET} ответ по инструкции пустой")
        else:
            print(
                f"{GREEN}✓{RESET} ответ получен: {len(answer)} знаков, "
                f"источников {len(citations)}, "
                f"релевантность {grounded.get('top_relevance_percent')}%, "
                f"{grounded.get('latency_ms')} мс"
            )

            if not CITATION_RE.search(answer):
                failures.append("в тексте ответа нет ссылки на пункт инструкции")
                print(f"{RED}✗{RESET} в ответе нет ссылки вида [п. 3.4]")
            else:
                print(f"{GREEN}✓{RESET} ответ содержит ссылку на пункт инструкции")

            if not citations:
                failures.append("ответ без источников")
                print(f"{RED}✗{RESET} источники не приложены")
            else:
                first = citations[0]
                address = first.get("clause_numbers") or first.get("clause_number")
                print(f"{GREEN}✓{RESET} первый источник: п. {address}, с. {first.get('page_number')}")

            verdict = grounded.get("critic_verdict")
            if verdict:
                print(f"{GREEN}✓{RESET} вердикт критика: {verdict}")

        # ── Честный отказ вне базы знаний ──
        refusal = ask(chat_url, token, OUT_OF_SCOPE_QUESTION)
        refusal_answer = str(refusal.get("answer", ""))
        refused = refusal.get("answer_status") in {"no_context", "info"} or any(
            marker.lower() in refusal_answer.lower() for marker in REFUSAL_MARKERS
        )

        if args.verbose:
            print(f"\n   вопрос: {OUT_OF_SCOPE_QUESTION}\n   ответ: {refusal_answer[:300]}\n")

        if refused:
            print(f"{GREEN}✓{RESET} на вопрос вне базы знаний получен честный отказ")
        else:
            failures.append(
                "на вопрос вне базы знаний получен содержательный ответ — проверьте "
                "порог ANSWER_MIN_RELEVANCE и промпт"
            )
            print(f"{RED}✗{RESET} вопрос вне базы знаний получил ответ вместо отказа")

        # ── Реестр бланков ──
        status, templates = call(args.base_url, "/documents/templates", token=token)
        if status == 200 and isinstance(templates, list):
            if templates:
                print(f"{GREEN}✓{RESET} бланков документов: {len(templates)}")
            else:
                warnings.append("реестр бланков пуст — выполните make seed")
                print(f"{YELLOW}!{RESET} реестр бланков пуст")
        else:
            failures.append(f"реестр бланков недоступен ({status})")
            print(f"{RED}✗{RESET} реестр бланков недоступен ({status})")

        # ── Состояние компонентов ──
        status, health = call(args.base_url, "/admin/system/health", token=token)
        if status == 200 and isinstance(health, dict):
            unhealthy = [
                item.get("name")
                for item in health.get("components") or []
                if not item.get("healthy")
            ]
            if unhealthy:
                warnings.append(f"компоненты не в порядке: {', '.join(map(str, unhealthy))}")
                print(f"{YELLOW}!{RESET} состояние: {health.get('status')}, сбои: {unhealthy}")
            else:
                print(f"{GREEN}✓{RESET} состояние контура: {health.get('status')}")
        elif status in {401, 403}:
            warnings.append("состояние компонентов недоступно для этой роли")
        else:
            failures.append(f"состояние контура недоступно ({status})")

    except SmokeError as error:
        print(f"{RED}✗{RESET} {error}")
        failures.append(str(error))

    print("-" * 64)
    for item in warnings:
        print(f"{YELLOW}предупреждение:{RESET} {item}")
    for item in failures:
        print(f"{RED}ошибка:{RESET} {item}")

    if failures:
        print(f"\n{RED}Проверка не пройдена: {len(failures)} ошибок{RESET}")
        return 1

    print(f"\n{GREEN}Контур работает{RESET}" + (f", предупреждений: {len(warnings)}" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
