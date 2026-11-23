#!/usr/bin/env python3
"""Проверка самодостаточности образа FFprog изнутри контейнера.

Скрипт отвечает на один вопрос: «сможет ли программа на чужом ПК работать,
если в образе есть всё нужное». Проверяются бинарники, Tkinter, наличие
кодеков, шрифты с кириллицей и реальный прогон цепочки через ffmpeg_engine.

Выход: 0 — всё в порядке, 1 — есть проблемы (текст в stdout), 2 — критично.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

NEEDED_ENCODERS = {
    "libx264": "mp4/mkv/mov",
    "mpeg4": "avi",
    "libvpx-vp9": "webm",
    "libmp3lame": "mp3",
    "aac": "aac/m4a",
    "flac": "flac",
    "libvorbis": "ogg",
    "libopus": "opus",
    "pcm_s16le": "wav",
}

OK, FAIL, WARN = "OK ", "FAIL", "?"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0
        self.warned = 0

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        if status == FAIL:
            self.failed += 1
        elif status == WARN:
            self.warned += 1

    def show(self) -> int:
        for status, name, detail in self.rows:
            line = f"[{status}] {name}"
            if detail:
                line += f": {detail}"
            print(line)
        print(f"\nИтог: ошибок {self.failed}, предупреждений {self.warned}")
        return 1 if self.failed else 0


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_binaries(rep: Report) -> None:
    print(f"Python {sys.version.split()[0]}, cwd={os.getcwd()}, uid={os.getuid()}")
    for tool in ("ffmpeg", "ffprobe", "xdg-open", "runuser", "tini"):
        path = shutil.which(tool)
        if path:
            rep.add(OK, tool, path)
        else:
            # ffmpeg/ffprobe критичны, остальное — деградирует мягко.
            rep.add(FAIL if tool in ("ffmpeg", "ffprobe") else WARN, tool, "не найден")
    if not shutil.which("pcmanfm") and not shutil.which("thunar"):
        rep.add(WARN, "файловый менеджер", "кнопку «Открыть папку» нечем открыть")


def check_tk(rep: Report) -> None:
    try:
        import tkinter
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "tkinter", f"не импортируется: {exc}")
        return
    rep.add(OK, "tkinter", f"Tcl/Tk {tkinter.TkVersion}")
    if os.environ.get("DISPLAY"):
        try:
            root = tkinter.Tk()
            root.withdraw()
            root.update_idletasks()
            root.destroy()
            rep.add(OK, "Tk окно", f"DISPLAY={os.environ['DISPLAY']}")
        except Exception as exc:  # noqa: BLE001
            rep.add(FAIL, "Tk окно", f"не открылось: {exc}")
    else:
        rep.add(WARN, "Tk окно", "DISPLAY не задан (для GUI нужен X11-проброс)")


def check_codecs(rep: Report) -> None:
    try:
        out = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-encoders"]).stdout
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "ffmpeg -encoders", str(exc))
        return
    missing = [name for name in NEEDED_ENCODERS if name not in out]
    if missing:
        rep.add(FAIL, "кодеки", "нет: " + ", ".join(missing))
    else:
        rep.add(OK, "кодеки", "все 9 нужных на месте")
    # Демоны-фильтры, без которых ломаются обрезка/скорость/склейка.
    filters = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-filters"]).stdout
    for needed in ("atempo", "setpts", "volume", "concat"):
        rep.add(OK if needed in filters else FAIL, f"фильтр {needed}")


def check_fonts_and_locale(rep: Report) -> None:
    lang = os.environ.get("LANG", "")
    try:
        import locale as _loc
        _loc.setlocale(_loc.LC_ALL, "")
        rep.add(OK, "locale", lang or os.environ.get("LC_ALL", "не задан"))
    except Exception as exc:  # noqa: BLE001
        rep.add(WARN, "locale", f"{lang}: {exc}")
    if not shutil.which("fc-match"):
        rep.add(WARN, "шрифты", "fontconfig отсутствует")
        return
    out = run(["fc-match", "-f", "%{family}", "DejaVu Sans"]).stdout.strip()
    rep.add(OK if out else FAIL, "шрифт по умолчанию", out)
    families = run(["fc-list", ":lang=ru", "family"]).stdout
    count = len([line for line in families.splitlines() if line.strip()])
    rep.add(OK if count else WARN, "кириллица в шрифтах",
            f"{count} семейств с поддержкой ru")


def check_engine(rep: Report) -> None:
    """Реальный прогон: генерируем синтетическое видео и гоняем цепочку."""
    try:
        import ffmpeg_engine as eng
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "ffmpeg_engine", f"не импортируется: {exc}")
        return
    workdir = tempfile.mkdtemp(prefix="ffprog_smoke_")
    try:
        src = os.path.join(workdir, "src.mp4")
        dst = os.path.join(workdir, "результат_на_выходе.mp3")
        gen = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src])
        if gen.returncode != 0 or not os.path.isfile(src):
            rep.add(FAIL, "генерация теста", gen.stderr.strip()[:200])
            return
        rep.add(OK, "lavfi-генерация", os.path.basename(src))

        steps = [eng.ConvertStep(fmt="mp3", quality="Высокое"),
                 eng.TrimStep(start=1.0, end=4.0),
                 eng.VolumeStep(value=eng.normalize_volume("150%")),
                 eng.SpeedStep(factor=eng.parse_factor("1.25"))]
        logs: list[str] = []
        eng.Pipeline(logs.append).run(src, steps, dst)
        streams = eng.probe_streams(dst)
        if streams.has_video or not streams.has_audio:
            rep.add(FAIL, "цепочка", "ожидался аудиофайл без видео")
        else:
            rep.add(OK, "цепочка из 4 шагов", streams.describe())
        if not os.path.isfile(dst) or os.path.getsize(dst) < 1000:
            rep.add(FAIL, "файл результата", "слишком маленький или отсутствует")
        # Кириллица в имени пути проверялась самим названием dst.
        rep.add(OK, "кириллица в путях", os.path.basename(dst))
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "ffmpeg_engine", f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_temp_writable(rep: Report) -> None:
    try:
        with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir()) as fh:
            fh.write(b"ok")
        rep.add(OK, "TMPDIR запись", tempfile.gettempdir())
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "TMPDIR запись", str(exc))


def main() -> int:
    rep = Report()
    check_binaries(rep)
    check_tk(rep)
    check_codecs(rep)
    check_fonts_and_locale(rep)
    check_temp_writable(rep)
    check_engine(rep)
    return rep.show()


if __name__ == "__main__":
    sys.exit(main())
