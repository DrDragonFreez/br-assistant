"""Генерация обучающего набора из текста инструкции.

Зачем дообучать модель, когда есть RAG: базовая Qwen2.5-7B отвечает по
переданным фрагментам, но делает это не так, как нужно в документообороте —
забывает ставить ссылку на пункт после каждого утверждения, пересказывает
требование своими словами и, главное, пытается ответить даже тогда, когда
во фрагментах ответа нет. Дообучение закрепляет три навыка: формат ссылки
``[п. 3.4]``, дословность формулировок и отказ ``НЕТ_В_БАЗЕ_ЗНАНИЙ``.

Набор строится из самой инструкции, без ручной разметки:

1. документ нарезается тем же чанкером, что работает в бэкенде, — обучающие
   примеры повторяют то, что модель увидит в рабочем режиме;
2. для каждого фрагмента модель-учитель (крупнее рабочей) придумывает
   вопрос сотрудника и ответ строго по тексту фрагмента;
3. треть примеров делается отрицательной: к вопросу подкладываются
   фрагменты из других глав, и правильный ответ — отказ. Без таких примеров
   модель учится отвечать всегда, а это прямой путь к выдумыванию.

Запуск::

    python ml/synth_dataset.py --source data/instructions \\
        --output data/synthetic_qa/train.jsonl --per-chunk 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# Скрипт запускается вне контейнера, поэтому пакет backend подключается
# по относительному пути: дублировать чанкер и промпты нельзя — набор
# должен готовиться точно тем же кодом, что работает в рантайме.
BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_PATH))

from app.rag.chunker import ClauseAwareChunker  # noqa: E402
from app.rag.loader import load_document  # noqa: E402
from app.rag.prompts import NO_ANSWER_MARKER, SYSTEM_PROMPT  # noqa: E402

DEFAULT_SOURCE = Path("data/instructions")
DEFAULT_OUTPUT = Path("data/synthetic_qa/train.jsonl")
DEFAULT_MODEL = "qwen2.5:14b-instruct"
DEFAULT_OLLAMA = "http://localhost:11434"

# Доля примеров, где правильный ответ — отказ.
NEGATIVE_SHARE = 0.3
# Минимальная длина фрагмента: по обрывку осмысленный вопрос не задать.
MIN_CHUNK_CHARS = 200

TEACHER_SYSTEM = """Ты составляешь обучающие примеры для помощника по документообороту.
По фрагменту нормативного документа придумай вопрос, который реально задал бы сотрудник,
и ответ на него.

Требования к ответу:
- только то, что есть во фрагменте; ничего не добавляй от себя;
- формулировки сохраняй близкими к тексту документа;
- после каждого утверждения ставь ссылку на пункт в квадратных скобках, например [п. 3.4];
- 2-4 предложения.

Требования к вопросу:
- как спрашивает человек, без канцелярита и без слова «фрагмент»;
- без номера пункта в тексте вопроса: сотрудник его обычно не знает.

Верни строго JSON: {"question": "...", "answer": "..."}"""

TEACHER_USER = """ПУНКТ: {clause}
ТЕКСТ ФРАГМЕНТА:
{content}

Составь {count} разных пар «вопрос — ответ» по этому фрагменту.
Верни JSON-массив объектов вида {{"question": "...", "answer": "..."}}."""

JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


@dataclass(slots=True)
class Example:
    """Пример обучающего набора в формате чата."""

    system: str
    user: str
    assistant: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        """Сериализует пример в формат, который читает train_lora.py."""
        return {
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": self.user},
                {"role": "assistant", "content": self.assistant},
            ],
            "kind": self.kind,
        }


def format_context(chunks: list[Any]) -> str:
    """Собирает блок контекста так же, как это делает рантайм.

    Args:
        chunks: Фрагменты инструкции.

    Returns:
        Текст контекста для пользовательского промпта.
    """
    parts: list[str] = []
    for position, chunk in enumerate(chunks, start=1):
        address = ", ".join(chunk.clause_numbers) or chunk.clause_number
        header = f"--- Фрагмент {position}"
        if address:
            header += f" | п. {address}"
        if chunk.page_number:
            header += f" | с. {chunk.page_number}"
        parts.append(f"{header} ---\n{chunk.content.strip()}")
    return "\n\n".join(parts)


def build_user_prompt(question: str, chunks: list[Any]) -> str:
    """Формирует пользовательский промпт обучающего примера.

    Args:
        question: Вопрос сотрудника.
        chunks: Фрагменты, переданные модели.

    Returns:
        Текст промпта.
    """
    return f"ФРАГМЕНТЫ ИНСТРУКЦИИ:\n{format_context(chunks)}\n\nВОПРОС СОТРУДНИКА: {question}"


async def ask_teacher(
    client: httpx.AsyncClient, model: str, clause: str, content: str, count: int
) -> list[dict[str, str]]:
    """Просит модель-учителя составить пары «вопрос — ответ».

    Args:
        client: HTTP-клиент Ollama.
        model: Имя модели-учителя.
        clause: Номер пункта.
        content: Текст фрагмента.
        count: Сколько пар запросить.

    Returns:
        Список пар; пустой список при неразобранном ответе.
    """
    response = await client.post(
        "/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": TEACHER_SYSTEM},
                {
                    "role": "user",
                    "content": TEACHER_USER.format(clause=clause, content=content, count=count),
                },
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.7, "num_ctx": 8192},
        },
    )
    response.raise_for_status()
    raw = str(response.json().get("message", {}).get("content", ""))

    # Модель может вернуть массив, один объект или объект с полем items.
    for pattern in (JSON_ARRAY_RE, JSON_OBJECT_RE):
        match = pattern.search(raw)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
            items = parsed["items"]
        elif isinstance(parsed, dict):
            items = [parsed]
        else:
            continue

        pairs = [
            {"question": str(item["question"]).strip(), "answer": str(item["answer"]).strip()}
            for item in items
            if isinstance(item, dict) and item.get("question") and item.get("answer")
        ]
        if pairs:
            return pairs

    return []


async def load_chunks(source: Path) -> list[Any]:
    """Загружает и нарезает все документы каталога.

    Args:
        source: Файл или каталог с документами.

    Returns:
        Список фрагментов, пригодных для составления вопросов.
    """
    files = [source] if source.is_file() else sorted(
        item
        for item in source.rglob("*")
        if item.is_file() and item.suffix.lower() in {".pdf", ".docx", ".txt", ".md", ".html"}
    )
    if not files:
        raise SystemExit(f"В {source} нет документов для обработки")

    chunker = ClauseAwareChunker()
    chunks: list[Any] = []

    for path in files:
        loaded = await load_document(path)
        pages = [(page.page_number, page.text) for page in loaded.pages]
        produced = chunker.split(pages)
        chunks.extend(
            chunk
            for chunk in produced
            if len(chunk.content) >= MIN_CHUNK_CHARS and chunk.clause_numbers
        )
        print(f"  {path.name}: фрагментов с пунктами — {len(produced)}")

    return chunks


def make_negative(question: str, chunks: list[Any], pool: list[Any], rng: random.Random) -> Example:
    """Собирает отрицательный пример: вопрос без ответа в контексте.

    Args:
        question: Вопрос сотрудника.
        chunks: Фрагменты, которые нужно исключить из контекста.
        pool: Все доступные фрагменты.
        rng: Генератор случайных чисел.

    Returns:
        Пример с ожидаемым отказом.
    """
    forbidden = {chunk.clause_number for chunk in chunks}
    candidates = [chunk for chunk in pool if chunk.clause_number not in forbidden]
    distractors = rng.sample(candidates, k=min(3, len(candidates)))

    return Example(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(question, distractors),
        assistant=NO_ANSWER_MARKER,
        kind="negative",
    )


async def main() -> None:
    """Точка входа генератора набора."""
    parser = argparse.ArgumentParser(description="Генерация обучающего набора из инструкции")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Файл или каталог")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Куда писать JSONL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Модель-учитель в Ollama")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA, help="Базовый адрес Ollama")
    parser.add_argument("--per-chunk", type=int, default=2, help="Пар на фрагмент")
    parser.add_argument("--limit", type=int, default=0, help="Ограничить число фрагментов")
    parser.add_argument("--seed", type=int, default=20261023, help="Зерно случайности")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    print(f">> разбор документов из {args.source}")
    chunks = await load_chunks(args.source)
    if args.limit:
        chunks = chunks[: args.limit]
    print(f">> пригодных фрагментов: {len(chunks)}")

    examples: list[Example] = []
    negative_target = int(len(chunks) * NEGATIVE_SHARE)

    async with httpx.AsyncClient(base_url=args.ollama, timeout=180.0) as client:
        for index, chunk in enumerate(chunks, start=1):
            address = ", ".join(chunk.clause_numbers) or chunk.clause_number
            try:
                pairs = await ask_teacher(
                    client, args.model, address, chunk.content, args.per_chunk
                )
            except httpx.HTTPError as error:
                print(f"  [{index}/{len(chunks)}] п. {address}: ошибка модели — {error}")
                continue

            if not pairs:
                print(f"  [{index}/{len(chunks)}] п. {address}: ответ не разобран")
                continue

            for pair in pairs:
                examples.append(
                    Example(
                        system=SYSTEM_PROMPT,
                        user=build_user_prompt(pair["question"], [chunk]),
                        assistant=pair["answer"],
                        kind="positive",
                    )
                )

            # Отрицательные примеры берутся из тех же вопросов: так модель
            # учится отличать «ответа нет в контексте» от «вопрос непонятен».
            if len(examples) and negative_target > 0 and index % 3 == 0:
                examples.append(make_negative(pairs[0]["question"], [chunk], chunks, rng))
                negative_target -= 1

            print(f"  [{index}/{len(chunks)}] п. {address}: примеров — {len(pairs)}")

    rng.shuffle(examples)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for example in examples:
            handle.write(json.dumps(example.to_dict(), ensure_ascii=False) + "\n")

    positives = sum(1 for item in examples if item.kind == "positive")
    negatives = len(examples) - positives
    print(
        f">> записано {len(examples)} примеров в {args.output} "
        f"(с ответом — {positives}, с отказом — {negatives})"
    )


if __name__ == "__main__":
    asyncio.run(main())
