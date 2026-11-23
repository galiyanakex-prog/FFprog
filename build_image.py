#!/usr/bin/env python3
"""Автоматическая сборка образа FFprog.

Скрипт:
  1. Проверяет, что docker запущен и доступен.
  2. Собирает образ через `docker build`.
  3. Запускает smoke-тест внутри образа.
  4. Запускает unit-тесты из tests/.
  5. Показывает размер образа и команды для запуска.

Использование:
  python3 build_image.py              # собрать + проверить
  python3 build_image.py --no-test    # собрать, но не проверять
  python3 build_image.py --tag my/ffprog:latest
  python3 build_image.py --help
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------- утилиты
PROJECT_ROOT = Path(__file__).resolve().parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
IMAGE_NAME = "ffprog"
IMAGE_TAG = "latest"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


def ok(text: str) -> str:
    return color(text, GREEN)


def fail(text: str) -> str:
    return color(text, RED)


def warn(text: str) -> str:
    return color(text, YELLOW)


def info(text: str) -> str:
    return color(text, CYAN)


def bold(text: str) -> str:
    return color(text, BOLD)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Запуск команды: печать показывает процесс, capture собирает вывод."""
    print(info(f"$ {' '.join(cmd)}"))
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def check_docker() -> None:
    """Проверяет, что docker доступен и отвечает."""
    if not shutil.which("docker"):
        sys.exit(fail("docker не найден в PATH. Установите Docker."))
    r = run(["docker", "info"])
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()
        sys.exit(fail("Docker не отвечает: " + (detail[0] if detail else "нет связи с демоном")
                      + "\nДобавьте себя в группу docker и перезайдите в сессию."))
    print(ok("Docker доступен."))


def build_image(tag: str, no_test: bool = False) -> None:
    """Собирает образ и (опционально) проверяет."""
    full_ref = f"{IMAGE_NAME}:{tag}"
    print(bold(f"\n=== Сборка образа {full_ref} ==="))
    # Вывод сборки идёт напрямую в терминал: видно прогресс apt и слои.
    r = run(
        ["docker", "build",
         "--platform", "linux/amd64",
         "--tag", full_ref,
         "--progress", "plain",
         "."],
        cwd=str(PROJECT_ROOT),
        capture_output=False,
    )
    if r.returncode != 0:
        sys.exit(fail("Сборка образа не удалась."))
    print(ok(f"Образ {full_ref} собран."))

    # --- smoke-тест ---
    print(bold("\n=== Smoke-тест (ffmpeg + кодеки + движок) ==="))
    r = run(
        ["docker", "run", "--rm",
         "--platform", "linux/amd64",
         full_ref, "smoke"],
        cwd=str(PROJECT_ROOT),
    )
    print(r.stdout or "")
    if r.returncode != 0:
        print(warn("Smoke-тест вернул ошибки — см. вывод выше."))
    else:
        print(ok("Smoke-тест прошёл."))

    # --- unit-тесты ---
    if not no_test:
        print(bold("\n=== Unit-тесты (unittest) ==="))
        r = run(
            ["docker", "run", "--rm",
             "--platform", "linux/amd64",
             full_ref, "test"],
            cwd=str(PROJECT_ROOT),
        )
        print(r.stdout or "")
        print(r.stderr or "")
        if r.returncode != 0:
            print(warn("Некоторые тесты не прошли."))
        else:
            print(ok("Все тесты прошли."))

    # --- размер ---
    print(bold("\n=== Размер образа ==="))
    r = run(["docker", "image", "inspect", full_ref,
             "--format", "{{.Size}}"])
    try:
        size_mb = int(r.stdout.strip()) / (1024 * 1024)
        print(f"  {bold('Размер:')} {size_mb:.0f} МБ")
    except ValueError:
        print(warn("  Не удалось определить размер образа."))

    print_commands(full_ref)


def print_commands(full_ref: str) -> None:
    """Печатает готовые команды запуска и переноса образа."""
    print(bold("\n=== Как запускать ==="))
    print(info("\nGUI (X11-проброс, доступ к домашним папкам):"))
    print("  ./run-ffprog.sh")
    print(f"\n  docker run --rm -it --platform linux/amd64 "
          f"--env DISPLAY=$DISPLAY --env HOST_UID=$(id -u) "
          f"--env HOST_GID=$(id -g) --env HOST_USER=$(whoami) "
          f"--volume /tmp/.X11-unix:/tmp/.X11-unix "
          f"--volume \"$HOME:$HOME\" "
          f"{full_ref}")

    print(info("\nДиагностика внутри образа:"))
    print(f"  docker run --rm --platform linux/amd64 {full_ref} version")
    print(f"  docker run --rm --platform linux/amd64 {full_ref} smoke")
    print(f"  docker run --rm --platform linux/amd64 {full_ref} bash")

    print(info("\nПеренос на другой ПК (без интернета и зависимостей):"))
    print(f"  docker save {full_ref} | gzip > ffprog.tar.gz")
    print("  # на целевом ПК: docker load < ffprog.tar.gz")
    print()



# ------------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Автоматическая сборка образа FFprog.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python3 build_image.py\n"
            "  python3 build_image.py --no-test\n"
            "  python3 build_image.py --tag my/ffprog:1.0\n"
        ),
    )
    parser.add_argument(
        "--tag", default=IMAGE_TAG,
        help=f"Тег образа (по умолчанию: {IMAGE_TAG})",
    )
    parser.add_argument(
        "--no-test", action="store_true",
        help="Не запускать smoke и unit-тесты после сборки",
    )
    args = parser.parse_args()

    check_docker()
    build_image(args.tag, no_test=args.no_test)


if __name__ == "__main__":
    main()
