"""Дообучение Qwen2.5-7B-Instruct методом QLoRA.

Полное дообучение 7B не умещается в 16 ГБ видеопамяти, доступных в Colab,
поэтому базовая модель загружается в 4 битах (NF4), а обучаются только
низкоранговые адаптеры на проекциях внимания и MLP. Обучаемых параметров
при этом около 0,5% от общего числа, а качество на узкой задаче — формат
ссылок и честный отказ — сопоставимо с полным дообучением.

Что осознанно выбрано:

* **r=16, alpha=32.** Задача не требует новых знаний, только устойчивого
  формата ответа; больший ранг на наборе из нескольких тысяч примеров
  быстрее приводит к переобучению, чем к пользе.
* **Маскирование промпта.** Потери считаются только по ответу модели:
  учить предсказывать текст фрагментов инструкции незачем, а на длинных
  контекстах это главный источник бесполезного сигнала.
* **packing отключён.** Примеры с отказом коротки, и склейка нескольких
  примеров в одну последовательность размыла бы границу между ними.

Запуск::

    python ml/train_lora.py --dataset data/synthetic_qa/train.jsonl \\
        --output ml/outputs/br-lora --epochs 3
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_DATASET = Path("data/synthetic_qa/train.jsonl")
DEFAULT_OUTPUT = Path("ml/outputs/br-lora")

# Проекции, к которым подключаются адаптеры: внимание плюс MLP. Только
# внимания для смены стиля ответа недостаточно, весь блок — избыточно.
TARGET_MODULES = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки.

    Returns:
        Разобранные аргументы.
    """
    parser = argparse.ArgumentParser(description="QLoRA-дообучение помощника по регламентам")
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch-size", type=int, default=1, help="Размер микропакета")
    parser.add_argument("--grad-accum", type=int, default=8, help="Шагов накопления градиента")
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--max-seq-length", type=int, default=4096)
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--eval-share", type=float, default=0.05, help="Доля на валидацию")
    parser.add_argument("--seed", type=int, default=20261023)
    return parser.parse_args()


def load_examples(path: Path) -> list[dict[str, object]]:
    """Читает набор примеров из JSONL.

    Args:
        path: Файл, собранный ``synth_dataset.py``.

    Returns:
        Список записей с полем ``messages``.

    Raises:
        SystemExit: Файл отсутствует или не содержит пригодных записей.
    """
    if not path.exists():
        raise SystemExit(f"Набор не найден: {path}. Сначала запустите ml/synth_dataset.py")

    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record.get("messages"), list) and len(record["messages"]) >= 2:
            records.append(record)

    if not records:
        raise SystemExit(f"В {path} нет пригодных примеров")
    return records


def main() -> None:
    """Запускает обучение адаптера."""
    args = parse_args()

    # Тяжёлые зависимости импортируются внутри функции: так --help работает
    # на машине без torch, а сообщение об отсутствующем пакете приходит
    # после разбора аргументов, а не вместо него.
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        set_seed,
    )
    from trl import SFTConfig, SFTTrainer

    set_seed(args.seed)

    records = load_examples(args.dataset)
    print(f">> примеров в наборе: {len(records)}")

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    # Для обучения выравнивание справа: иначе маска ответа съезжает.
    tokenizer.padding_side = "right"

    def to_text(record: dict[str, object]) -> dict[str, str]:
        """Превращает диалог в текст по шаблону чата модели."""
        messages = record["messages"]
        return {
            "text": tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
        }

    dataset = Dataset.from_list([to_text(item) for item in records])
    if args.eval_share > 0:
        split = dataset.train_test_split(test_size=args.eval_share, seed=args.seed)
        train_dataset, eval_dataset = split["train"], split["test"]
    else:
        train_dataset, eval_dataset = dataset, None

    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16
        if torch.cuda.is_bf16_supported()
        else torch.float16,
    )

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=quantization,
        device_map="auto",
        trust_remote_code=True,
        attn_implementation="sdpa",
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    peft_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=list(TARGET_MODULES),
    )
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    config = SFTConfig(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=2,
        eval_strategy="epoch" if eval_dataset is not None else "no",
        bf16=torch.cuda.is_bf16_supported(),
        fp16=not torch.cuda.is_bf16_supported(),
        optim="paged_adamw_8bit",
        max_grad_norm=0.3,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_seq_length=args.max_seq_length,
        dataset_text_field="text",
        packing=False,
        report_to=[],
        seed=args.seed,
    )

    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        processing_class=tokenizer,
    )

    trainer.train()

    args.output.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))

    print(f">> адаптер сохранён в {args.output}")
    print(">> дальше: python ml/export_gguf.py --adapter", args.output)


if __name__ == "__main__":
    main()
