"""Слияние LoRA-адаптера с базовой моделью и выгрузка в GGUF.

Ollama не умеет подключать адаптеры PEFT: ей нужен один файл модели
в формате GGUF. Поэтому путь от обучения до рантайма такой:

1. базовая модель загружается в fp16 (не в 4 битах — слияние с
   квантованными весами теряет точность);
2. адаптер применяется и сливается в веса;
3. результат конвертируется скриптом llama.cpp в GGUF f16;
4. GGUF квантуется до q4_k_m — 7B-модель занимает около 4,4 ГБ и
   укладывается в видеопамять вместе с эмбеддером и реранкером.

Слияние требует примерно 16 ГБ оперативной памяти: на машине с 8 ГБ
процесс завершится по памяти, и это ожидаемо — шаг выполняется там же,
где шло обучение.

Запуск::

    python ml/export_gguf.py --adapter ml/outputs/br-lora \\
        --llama-cpp ~/llama.cpp --quantization q4_k_m
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_ADAPTER = Path("ml/outputs/br-lora")
DEFAULT_MERGED = Path("ml/outputs/br-merged")
DEFAULT_TARGET = Path("ml/modelfiles")
DEFAULT_QUANTIZATION = "q4_k_m"

# Скрипт конвертации в разных версиях llama.cpp лежит под разными именами.
CONVERT_SCRIPTS = ("convert_hf_to_gguf.py", "convert-hf-to-gguf.py", "convert.py")
# Утилита квантования: сборка CMake кладёт её в build/bin.
QUANTIZE_BINARIES = (
    "llama-quantize",
    "llama-quantize.exe",
    "build/bin/llama-quantize",
    "build/bin/llama-quantize.exe",
    "quantize",
)


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки.

    Returns:
        Разобранные аргументы.
    """
    parser = argparse.ArgumentParser(description="Слияние адаптера и выгрузка GGUF")
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--merged", type=Path, default=DEFAULT_MERGED)
    parser.add_argument(
        "--llama-cpp",
        type=Path,
        default=None,
        help="Каталог с клоном llama.cpp; без него выполняется только слияние",
    )
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="Куда положить GGUF")
    parser.add_argument("--quantization", default=DEFAULT_QUANTIZATION)
    parser.add_argument(
        "--keep-f16",
        action="store_true",
        help="Не удалять промежуточный f16-файл (около 15 ГБ)",
    )
    return parser.parse_args()


def merge_adapter(base_model: str, adapter: Path, merged: Path) -> None:
    """Сливает адаптер с базовой моделью и сохраняет результат.

    Args:
        base_model: Имя или путь базовой модели.
        adapter: Каталог с обученным адаптером.
        merged: Каталог для слитой модели.

    Raises:
        SystemExit: Адаптер не найден.
    """
    if not adapter.exists():
        raise SystemExit(f"Адаптер не найден: {adapter}. Сначала запустите ml/train_lora.py")

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f">> загрузка базовой модели {base_model} в fp16")
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=torch.float16,
        device_map="cpu",
        trust_remote_code=True,
    )

    print(f">> применение адаптера {adapter}")
    model = PeftModel.from_pretrained(model, str(adapter))
    model = model.merge_and_unload()

    merged.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(merged), safe_serialization=True)

    # Токенизатор берётся из каталога адаптера: train_lora.py сохраняет его
    # рядом, и именно с ним модель обучалась.
    tokenizer_source = adapter if (adapter / "tokenizer_config.json").exists() else base_model
    AutoTokenizer.from_pretrained(str(tokenizer_source), trust_remote_code=True).save_pretrained(
        str(merged)
    )
    print(f">> слитая модель сохранена в {merged}")


def find_file(root: Path, names: tuple[str, ...]) -> Path | None:
    """Ищет первый существующий файл из списка имён.

    Args:
        root: Каталог поиска.
        names: Возможные относительные пути.

    Returns:
        Найденный путь либо None.
    """
    for name in names:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def convert_to_gguf(llama_cpp: Path, merged: Path, output: Path) -> None:
    """Конвертирует модель HuggingFace в GGUF f16.

    Args:
        llama_cpp: Каталог с клоном llama.cpp.
        merged: Каталог слитой модели.
        output: Файл GGUF на выходе.

    Raises:
        SystemExit: Скрипт конвертации не найден или завершился с ошибкой.
    """
    script = find_file(llama_cpp, CONVERT_SCRIPTS)
    if script is None:
        raise SystemExit(
            f"В {llama_cpp} не найден скрипт конвертации. "
            "Клонируйте llama.cpp: git clone https://github.com/ggerganov/llama.cpp"
        )

    command = [
        sys.executable,
        str(script),
        str(merged),
        "--outfile",
        str(output),
        "--outtype",
        "f16",
    ]
    print(">> конвертация в GGUF:", " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Конвертация завершилась с кодом {result.returncode}")


def quantize(llama_cpp: Path, source: Path, output: Path, quantization: str) -> None:
    """Квантует GGUF до указанного формата.

    Args:
        llama_cpp: Каталог с собранным llama.cpp.
        source: Файл GGUF f16.
        output: Файл результата.
        quantization: Формат квантования, например ``q4_k_m``.

    Raises:
        SystemExit: Утилита не найдена или завершилась с ошибкой.
    """
    binary = find_file(llama_cpp, QUANTIZE_BINARIES)
    if binary is None:
        raise SystemExit(
            f"В {llama_cpp} не найдена утилита llama-quantize. Соберите llama.cpp: "
            "cmake -B build && cmake --build build --config Release"
        )

    command = [str(binary), str(source), str(output), quantization.upper()]
    print(">> квантование:", " ".join(command))
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Квантование завершилось с кодом {result.returncode}")


def main() -> None:
    """Выполняет полный путь от адаптера до файла для Ollama."""
    args = parse_args()

    merge_adapter(args.base_model, args.adapter, args.merged)

    if args.llama_cpp is None:
        print(
            ">> llama.cpp не указан: слияние выполнено, конвертация пропущена.\n"
            "   Повторите с --llama-cpp <путь к клону llama.cpp>"
        )
        return

    args.target.mkdir(parents=True, exist_ok=True)
    f16_path = args.merged.parent / "br-assistant-f16.gguf"
    final_path = args.target / f"br-assistant-{args.quantization}.gguf"

    convert_to_gguf(args.llama_cpp, args.merged, f16_path)
    quantize(args.llama_cpp, f16_path, final_path, args.quantization)

    if not args.keep_f16:
        f16_path.unlink(missing_ok=True)

    size_gb = final_path.stat().st_size / 1024**3
    print(f">> готово: {final_path} ({size_gb:.1f} ГБ)")
    print(">> дальше: в ml/modelfiles/Modelfile заменить строку FROM на")
    print(f"   FROM ./{final_path.name}")
    print(">> затем собрать модель: make models")


if __name__ == "__main__":
    main()
