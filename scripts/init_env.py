#!/usr/bin/env python3
"""Готовит файл окружения из шаблона со сгенерированными секретами.

Заполнять `.env` руками неудобно и опасно: один и тот же пароль входит
в несколько переменных сразу. Пароль PostgreSQL — в `POSTGRES_PASSWORD`,
`DATABASE_URL` и `DATABASE_URL_SYNC`; пароль Redis — в `REDIS_PASSWORD`,
`REDIS_URL`, `CELERY_BROKER_URL` и `CELERY_RESULT_BACKEND`. Если заменить
заглушку не везде, сервис поднимется и упадёт на первом обращении к базе,
а причина будет видна только в логах.

Скрипт генерирует секреты криптостойким источником, подставляет их во все
связанные переменные и проверяет, что заглушек не осталось.

Существующий файл не перезаписывается: пароль PostgreSQL попадает в том
данных при первом запуске, и новый пароль к уже созданному тому не
подойдёт. Перезапись — только с `--force` и только вместе с удалением
тома (`make nuke`).

Зависимостей нет: только стандартная библиотека, чтобы скрипт запускался
до сборки образов.

Запуск::

    python scripts/init_env.py
    python scripts/init_env.py --output .env.local
    python scripts/init_env.py --force          # перезаписать существующий
"""

from __future__ import annotations

import argparse
import re
import secrets
import string
import sys
from pathlib import Path

TEMPLATE = Path(".env.example")
OUTPUT = Path(".env")

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

# Пароли идут внутрь строк подключения, поэтому символы, требующие
# процентного кодирования в URL, из алфавита исключены: «@» разорвал бы
# строку на хост и учётные данные, «/» — на базу данных.
URL_SAFE_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 24) -> str:
    """Генерирует пароль, пригодный для подстановки в строку подключения.

    Args:
        length: Длина пароля.

    Returns:
        Пароль из букв и цифр.
    """
    return "".join(secrets.choice(URL_SAFE_ALPHABET) for _ in range(length))


def build_secrets() -> dict[str, str]:
    """Готовит значения для всех переменных, требующих секрета.

    Пароль администратора собирается отдельно: к нему применяется
    требование сложности из ``app/core/security.py`` — буквы разных
    регистров, цифра и специальный символ.

    Returns:
        Словарь «переменная — значение».
    """
    postgres_password = generate_password()
    redis_password = generate_password()

    admin_password = (
        generate_password(16)
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("#$%&*+-=?@")
    )

    return {
        "SECRET_KEY": secrets.token_hex(32),
        "POSTGRES_PASSWORD": postgres_password,
        "REDIS_PASSWORD": redis_password,
        "QDRANT_API_KEY": secrets.token_hex(16),
        "GRAFANA_ADMIN_PASSWORD": generate_password(16),
        "BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        "DATABASE_URL": (
            f"postgresql+asyncpg://br_user:{postgres_password}@postgres:5432/br_assistant"
        ),
        "DATABASE_URL_SYNC": (
            f"postgresql+psycopg2://br_user:{postgres_password}@postgres:5432/br_assistant"
        ),
        "REDIS_URL": f"redis://:{redis_password}@redis:6379/0",
        "CELERY_BROKER_URL": f"redis://:{redis_password}@redis:6379/1",
        "CELERY_RESULT_BACKEND": f"redis://:{redis_password}@redis:6379/2",
    }


def fill_template(template_text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Подставляет значения в шаблон окружения.

    Комментарии и порядок строк сохраняются: шаблон документирует каждое
    поле, и переписывать его структурой из словаря значило бы потерять
    пояснения.

    Args:
        template_text: Содержимое ``.env.example``.
        values: Значения для подстановки.

    Returns:
        Готовый текст и список переменных, оставшихся с заглушкой.
    """
    lines: list[str] = []
    leftovers: list[str] = []

    for line in template_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in values:
            lines.append(f"{key}={values[key]}")
            continue

        lines.append(line)
        if "CHANGE_ME" in line:
            leftovers.append(key)

    return "\n".join(lines) + "\n", leftovers


def main() -> int:
    """Создаёт файл окружения и печатает выданные учётные данные.

    Returns:
        Код возврата процесса.
    """
    parser = argparse.ArgumentParser(description="Подготовка .env со сгенерированными секретами")
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписать существующий файл. Пароль базы данных при этом "
        "перестанет подходить к уже созданному тому — нужен make nuke",
    )
    args = parser.parse_args()

    if not args.template.exists():
        print(f"{RED}✗{RESET} шаблон не найден: {args.template}")
        return 2

    if args.output.exists() and not args.force:
        print(f"{YELLOW}!{RESET} файл {args.output} уже существует — он не тронут.")
        print("  Пароль PostgreSQL записан в том данных при первом запуске,")
        print("  и новый пароль к нему не подойдёт. Если нужно начать с чистого")
        print(f"  контура: make nuke, затем python {Path(__file__).name} --force")
        return 0

    text, leftovers = fill_template(
        args.template.read_text(encoding="utf-8"), build_secrets()
    )
    args.output.write_text(text, encoding="utf-8")

    remaining = text.count("CHANGE_ME")
    if remaining:
        print(f"{RED}✗{RESET} в {args.output} осталось заглушек: {remaining} ({', '.join(leftovers)})")
        print("  Шаблон изменился — добавь переменную в build_secrets().")
        return 1

    admin_email = re.search(r"^BOOTSTRAP_ADMIN_EMAIL=(.*)$", text, re.MULTILINE)
    admin_password = re.search(r"^BOOTSTRAP_ADMIN_PASSWORD=(.*)$", text, re.MULTILINE)
    grafana_user = re.search(r"^GRAFANA_ADMIN_USER=(.*)$", text, re.MULTILINE)
    grafana_password = re.search(r"^GRAFANA_ADMIN_PASSWORD=(.*)$", text, re.MULTILINE)

    print(f"{GREEN}✓{RESET} {args.output} создан, секреты сгенерированы")
    print()
    print("  Администратор помощника:")
    print(f"    {admin_email.group(1) if admin_email else '—'}")
    print(f"    {admin_password.group(1) if admin_password else '—'}")
    print("  Grafana:")
    print(f"    {grafana_user.group(1) if grafana_user else '—'}")
    print(f"    {grafana_password.group(1) if grafana_password else '—'}")
    print()
    print(f"{YELLOW}!{RESET} Выпиши пароли сейчас: в файле они есть, но файл вне репозитория.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
