#!/usr/bin/env python3
"""Сборка deb-пакета ffprog.

Пакет собирается без debhelper и исходного дерева Debian: скрипт сам
готовит каталог сборки, пишет DEBIAN/control, maintainer-скрипты, md5sums
и вызывает dpkg-deb.

Запуск:
    python3 build_deb.py            # собрать deb/ffprog_<VERSION>_all.deb
    python3 build_deb.py --version 1.1.0-1
    python3 build_deb.py --clean    # удалить артефакты сборки
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PKG = "ffprog"
VERSION = "1.2.0-1"
MAINTAINER = "galiyanakex-prog <galiyanakex@gmail.com>"
HOMEPAGE = "https://github.com/galiyanakex-prog/ffprog"
SUMMARY = "GUI media converter powered by FFmpeg"
DESCRIPTION = (
    " Tkinter wrapper around ffmpeg: pick a file or download one from a link\n"
    " (YouTube, VK Video and ~1800 other sites via yt-dlp), chain up to five\n"
    " tools (trim, convert, concatenate, volume, speed) in any order, set\n"
    " video and audio quality per step, watch the command log, cancel\n"
    " anytime. Intermediate files live in a temp dir and are removed after\n"
    " the run."
)

# В пакет попадает только рантайм. Тесты, докер, сборщики и отчёты — лишнее:
# они раздували старый .deb и тянули в /usr/lib модули, которых там быть не
# должно.
RUNTIME_FILES = ("app.py", "ffmpeg_engine.py")
DEB_DIR = PROJECT_ROOT / "deb"


def log(msg: str) -> None:
    print(f"[build_deb] {msg}")


def fail(msg: str) -> None:
    print(f"[build_deb] ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(1)


def normalize_perms(build_dir: Path) -> None:
    """Приводит права к виду, который ждёт dpkg: 755 каталоги и бинари, 644 код.

    shutil.copy2 сохраняет права исходных файлов, а в рабочем дереве они 664
    (группа может писать). В /usr/lib и /usr/share такое — нарушение FHS, и
    lintian на него ругается как на потенциальную проблему безопасности.
    """
    for root, dirs, files in os.walk(build_dir):
        for name in dirs:
            path = Path(root) / name
            if path.parent == build_dir / "DEBIAN":
                continue
            path.chmod(0o755)
        for name in files:
            path = Path(root) / name
            if path.parent == build_dir / "DEBIAN":
                path.chmod(0o755 if name in ("postinst", "preinst",
                                             "postrm", "config") else 0o644)
            elif path.suffix == ".py" or name.endswith((".svg", ".desktop",
                                                        ".md", ".gz")):
                path.chmod(0o644)
    (build_dir / "usr" / "bin" / PKG).chmod(0o755)
    log("права нормализованы (каталоги 755, код 644, бинари 755)")


# --------------------------------------------------------------------- files
def copy_runtime(build_dir: Path) -> None:
    dst = build_dir / "usr" / "lib" / PKG
    dst.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        src = PROJECT_ROOT / name
        if not src.is_file():
            fail(f"нет файла {src} — соберите пакет из корня проекта")
        shutil.copy2(src, dst / name)
    log(f"скопировано в пакет: {', '.join(RUNTIME_FILES)}")


def write_launcher(build_dir: Path) -> None:
    """/usr/bin/ffprog — тот же проверочный wrapper, что и run.sh.

    Старый лаунчер вызывал `python3 app.py` вслепую: без Tkinter пользователь
    получал голый Traceback, а при отсутствии DISPLAY — зависание. Здесь те же
    проверки, что в ./run.sh, и поддержка --check.
    """
    path = build_dir / "usr" / "bin" / PKG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LAUNCHER_SH, encoding="utf-8")
    path.chmod(0o755)
    log("написан usr/bin/ffprog")


def write_desktop(build_dir: Path) -> None:
    path = build_dir / "usr" / "share" / "applications" / f"{PKG}.desktop"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DESKTOP_ENTRY, encoding="utf-8")
    path.chmod(0o644)
    log("написан .desktop")


def write_icon(build_dir: Path) -> None:
    src = PROJECT_ROOT / "ffprog.svg"
    if not src.is_file():
        fail(f"нет иконки {src}")
    dst = build_dir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst / "ffprog.svg")
    log("скопирована иконка")


def write_docs(build_dir: Path) -> None:
    doc = build_dir / "usr" / "share" / "doc" / PKG
    doc.mkdir(parents=True, exist_ok=True)

    readme = PROJECT_ROOT / "README.md"
    if readme.is_file():
        shutil.copy2(readme, doc / "README.md")

    stamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    entry = (
        f"{PKG} ({VERSION}) stable; urgency=medium\n"
        "\n"
        "  * Download tool: fetch video/audio from YouTube, VK Video and\n"
        "    ~1800 other sites with yt-dlp, then process as a regular file.\n"
        "  * Separate video/audio quality controls for all five tools.\n"
        "  * Launcher now checks tkinter, ffmpeg and DISPLAY and prints\n"
        "    an actionable hint instead of a traceback.\n"
        "  * Added --check mode, md5sums and complete maintainer scripts.\n"
        "\n"
        f" -- {MAINTAINER}  {stamp}\n"
    )
    with gzip.GzipFile(filename="", mode="wb",
                       fileobj=open(doc / "changelog.Debian.gz", "wb"),
                       mtime=0) as handle:
        handle.write(entry.encode("utf-8"))

    (doc / "copyright").write_text(COPYRIGHT, encoding="utf-8")
    for f in doc.iterdir():
        f.chmod(0o644)
    log("написаны changelog.Debian.gz, copyright, README.md")


def write_control(build_dir: Path) -> None:
    """DEBIAN/control.

    Installed-Size обязателен и считается в КиБ: dpkg на его основе проверяет
    место. В старом пакете он был проставлен вручную и врал.
    """
    debian = build_dir / "DEBIAN"
    debian.mkdir(parents=True, exist_ok=True)

    size_kib = dir_size_kib(build_dir)
    control = "\n".join([
        f"Package: {PKG}",
        f"Version: {VERSION}",
        "Section: video",
        "Priority: optional",
        "Architecture: all",
        # ffmpeg в main, python3-tk тоже — зависимости удовлетворимы на
        # Debian/Ubuntu без сторонних репозиториев.
        "Depends: python3 (>= 3.8), python3-tk, ffmpeg",
        # yt-dlp нужен только для скачивания по ссылке. Recommends, а не
        # Depends: apt поставит его по умолчанию, но пакет установится и
        # без него — тогда просто недоступна одна из функций.
        "Recommends: yt-dlp, xdg-utils",
        f"Maintainer: {MAINTAINER}",
        f"Homepage: {HOMEPAGE}",
        f"Installed-Size: {size_kib}",
        f"Description: {SUMMARY}",
        DESCRIPTION,
        "",
    ])
    (debian / "control").write_text(control, encoding="utf-8")
    log(f"написан DEBIAN/control (Installed-Size: {size_kib} КиБ)")


def write_maintainer_scripts(build_dir: Path) -> None:
    debian = build_dir / "DEBIAN"
    scripts = {"postinst": POSTINST, "postrm": POSTRM}
    for name, body in scripts.items():
        path = debian / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    log("написаны postinst, postrm")


def write_md5sums(build_dir: Path) -> None:
    """DEBIAN/md5sums — контрольные суммы управляемых dpkg файлов.

    Файлы из DEBIAN/ в список не входят: dpkg сверяет только содержимое
    дерева установки. Без md5sums `dpkg -V` и часть инсталляторов считают
    пакет битым.
    """
    debian = build_dir / "DEBIAN"
    lines: list[str] = []
    for root, dirs, files in os.walk(build_dir):
        root_path = Path(root)
        if debian in root_path.parents or root_path == debian:
            dirs[:] = []
            continue
        for name in sorted(files):
            fpath = root_path / name
            rel = fpath.relative_to(build_dir).as_posix()
            lines.append(f"{hashlib.md5(fpath.read_bytes()).hexdigest()}  {rel}")
    (debian / "md5sums").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"написан DEBIAN/md5sums ({len(lines)} файлов)")


def dir_size_kib(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += Path(root, name).stat().st_size
            except OSError:
                pass
    return max(1, (total + 1023) // 1024)


# --------------------------------------------------------------------- build
def build(version: str) -> Path:
    global VERSION
    VERSION = version

    if not shutil.which("dpkg-deb"):
        fail("dpkg-deb не найден — deb-пакет собирается только на Debian/Ubuntu")

    out = DEB_DIR / f"{PKG}_{VERSION}_all.deb"
    build_dir = DEB_DIR / f"{PKG}-{VERSION}.build"

    log(f"сборка {PKG} {VERSION} (architecture: all)")
    DEB_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    copy_runtime(build_dir)
    write_launcher(build_dir)
    write_desktop(build_dir)
    write_icon(build_dir)
    write_docs(build_dir)
    write_control(build_dir)
    write_maintainer_scripts(build_dir)
    write_md5sums(build_dir)
    normalize_perms(build_dir)

    # --root-owner-group: без него файлы принадлежат сборщику, и на чужом
    # хосте dpkg ставит /usr/lib/ffprog от имени несуществующего uid.
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group",
                    str(build_dir), str(out)], check=True)
    shutil.rmtree(build_dir)

    verify(out)
    log(f"готово: {out} ({out.stat().st_size / 1024:.0f} КИБ)")
    return out


def verify(path: Path) -> None:
    """Локальная приёмка пакета: то, на чём молча падает чужой хост."""
    info = subprocess.run(["dpkg-deb", "-f", str(path)],
                          capture_output=True, text=True, check=True).stdout
    fields = {}
    for line in info.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    for field in ("Package", "Version", "Architecture", "Maintainer",
                  "Description", "Installed-Size", "Depends"):
        if not fields.get(field):
            fail(f"в control нет поля {field}")
    if fields["Installed-Size"] == "0":
        fail("Installed-Size = 0, dpkg сочтёт пакет битым")
    if fields["Architecture"] != "all":
        fail("Architecture должна быть all — пакет не содержит бинарей")

    contents = subprocess.run(["dpkg-deb", "-c", str(path)],
                              capture_output=True, text=True, check=True).stdout
    entries = {}
    for line in contents.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) == 6:
            entries[parts[5]] = parts[0]
    for expected in (f"./usr/bin/{PKG}", f"./usr/lib/{PKG}/app.py",
                     f"./usr/lib/{PKG}/ffmpeg_engine.py",
                     f"./usr/share/applications/{PKG}.desktop",
                     f"./usr/share/doc/{PKG}/copyright",
                     f"./usr/share/doc/{PKG}/changelog.Debian.gz",
                     f"./usr/share/icons/hicolor/scalable/apps/{PKG}.svg"):
        if expected not in entries:
            fail(f"в архиве нет {expected}")
    if not entries[f"./usr/bin/{PKG}"].startswith("-rwx"):
        fail("/usr/bin/ffprog не исполняемый — запуск из меню не сработает")
    for doc in (f"./usr/share/doc/{PKG}/copyright",
                f"./usr/share/doc/{PKG}/changelog.Debian.gz"):
        if entries[doc].startswith("-rwx"):
            fail(f"{doc} не должен быть исполняемым")

    staging = Path("/tmp/ffdeb-check")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["dpkg-deb", "-e", str(path), str(staging)], check=True)
        for name in ("control", "md5sums", "postinst", "postrm"):
            script = staging / name
            if not script.is_file():
                fail(f"в DEBIAN нет {name}")
            if name in ("postinst", "postrm"):
                if not script.stat().st_mode & 0o111:
                    fail(f"DEBIAN/{name} не исполняемый — dpkg прервёт установку")
                for shell in ("sh", "bash"):
                    probe = shutil.which(shell)
                    if probe and subprocess.run(
                            [probe, "-n", str(script)]).returncode != 0:
                        fail(f"DEBIAN/{name}: синтаксическая ошибка ({shell})")

        # md5sums должен покрывать всё дерево установки: без полной сверки
        # `dpkg -V` на чужом хосте молча ничего не проверяет.
        md5_names = {ln.split("  ", 1)[1].strip()
                     for ln in (staging / "md5sums").read_text().splitlines()
                     if "  " in ln}
        installed = {p[2:] for p in entries
                     if p.startswith("./") and not p.endswith("/")}
        if missing := sorted(installed - md5_names):
            fail(f"в md5sums нет записей для: {', '.join(missing)}")
        if extra := sorted(md5_names - installed):
            fail(f"в md5sums есть лишние пути: {', '.join(extra)}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    log("приёмка пакета пройдена")


def clean() -> None:
    """Удаляет каталоги сборки, оставшиеся после прерванной сборки."""
    removed = []
    if DEB_DIR.is_dir():
        for path in sorted(DEB_DIR.glob(f"{PKG}-*.build")):
            shutil.rmtree(path)
            removed.append(path.name)
    print("Удалено: " + ", ".join(removed) if removed else "Нечего очищать.")


# ------------------------------------------------------------------ payloads
LAUNCHER_SH = """#!/bin/sh
# FFprog (deb-пакет). POSIX-совместим: если /bin/sh — dash, уходим под bash.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash -- "$0" "$@"
    fi
    echo "FFprog: для запуска нужен bash. Установите пакет bash." >&2
    exit 1
fi
set -eu

APP_DIR=/usr/lib/ffprog
PYTHON="${PYTHON:-python3}"
GUI=1
for arg in "$@"; do
    case "$arg" in
        --no-gui|--check) GUI=0 ;;
    esac
done

fail() {
    printf 'FFprog: %s\n' "$1" >&2
    [ $# -gt 1 ] && printf '  → %s\n' "$2" >&2
    exit 1
}

command -v "$PYTHON" >/dev/null 2>&1 ||
    fail "не найден $PYTHON в PATH" "sudo apt install python3"

[ -f "$APP_DIR/app.py" ] ||
    fail "не найден $APP_DIR/app.py" "пакет повреждён, переустановите его"

if [ "$GUI" = "1" ] && ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    fail "для $PYTHON недоступен Tkinter" \\
         "sudo apt install python3-tk"
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    fail "в PATH не найдены ffmpeg/ffprobe" "sudo apt install ffmpeg"
fi

if [ "$GUI" = "1" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    fail "DISPLAY не задан — открыть окно некуда" \\
         "запустите из графической сессии либо: DISPLAY=:1 ffprog"
fi

if [ "$GUI" = "0" ]; then
    printf 'OK: python=%s, ffmpeg=%s\n' \\
        "$("$PYTHON" -V 2>&1)" "$(ffmpeg -version | head -1 | cut -d' ' -f3)"
    exit 0
fi

cd "$APP_DIR"
exec "$PYTHON" "$APP_DIR/app.py" "$@"
"""

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=FFprog
GenericName=Media converter
Comment=GUI-конвертер медиа на FFmpeg
Exec=ffprog %U
Icon=ffprog
Terminal=false
Categories=AudioVideo;AudioVideoEditing;GTK;
Keywords=ffmpeg;video;audio;convert;trim;
MimeType=audio/mpeg;video/mp4;video/x-matroska;audio/x-flac;audio/ogg;
StartupNotify=true
"""

COPYRIGHT = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: ffprog
Upstream-Contact: {MAINTAINER}
Source: {HOMEPAGE}

Files: *
Copyright: 2026 {MAINTAINER.split('<')[0].strip()}
License: GPL-3.0-or-later

License: GPL-3.0-or-later
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 You should have received a copy of the GNU General Public License
 along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

POSTINST = """#!/bin/sh
# Обновляет кэш меню и иконок после установки.
# set +e: если update-desktop-database недоступен, пакет не должен
# стать «half-configured» — dpkg на это ругается.
set +e

case "$1" in
    configure)
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q /usr/share/applications || true
        fi
        if command -v gtk-update-icon-cache >/dev/null 2>&1 \\
           && [ -d /usr/share/icons/hicolor ]; then
            gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
        fi
        ;;
esac

exit 0
"""

POSTRM = """#!/bin/sh
# Чистит кэш при удалении/очистке пакета.
set +e

case "$1" in
    purge)
        rm -rf "/var/lib/ffprog" 2>/dev/null || true
        ;;
esac

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 \\
   && [ -d /usr/share/icons/hicolor ]; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi

exit 0
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать deb-пакет ffprog")
    parser.add_argument("--version", default=VERSION,
                        help=f"версия пакета (сейчас {VERSION})")
    parser.add_argument("--clean", action="store_true",
                        help="удалить каталоги сборки")
    args = parser.parse_args()
    if args.clean:
        clean()
        return
    build(args.version)


if __name__ == "__main__":
    main()