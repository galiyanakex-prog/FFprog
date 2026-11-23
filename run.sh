#!/usr/bin/env bash
# Запуск FFprog одним действием (двойной клик / ярлык / ./run.sh).
#
# Строка ниже лечит перенос через Windows: если в файле остались CRLF,
# sed убирает их, а exec перечитывает уже чистый скрипт. Всё в одной строке
# и с комментарием в конце — иначе сам CRLF сломал бы разбор многострочного
# синтаксиса раньше, чем мы дошли бы до починки.
grep -q "$(printf '\r')" "$0" 2>/dev/null && { sed -i 's/\r$//' -- "$0" 2>/dev/null || { echo "FFprog: в run.sh windows-переносы строк, а прав на запись нет — выполните: dos2unix run.sh" >&2; exit 1; }; exec bash -- "$0" "$@"; } # heal-crlf
#

# Скрипт совместим с POSIX sh: во многих дистрибутивах /bin/sh — это dash,
# и запуск «sh run.sh» не должен спотыкаться о bash-измы вроде ${BASH_SOURCE}.
# Если bash нужен — перезапускаемся под ним, сохранив аргументы.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        _self=$0
        case $_self in
            */*) ;;
            *) _self=$(command -v -- "$_self" 2>/dev/null || printf '%s' "$0") ;;
        esac
        exec bash -- "$_self" "$@"
    fi
    echo "FFprog: для запуска нужен bash. Установите пакет bash." >&2
    exit 1
fi
set -eu

# Каталог скрипта определяем через $0 (POSIX), а не через ${BASH_SOURCE[0]},
# чтобы скрипт одинаково работал из любой папки и у любого вызывающего shell.
SELF=$0
case $SELF in
    */*) APP_DIR=$(cd -- "$(dirname -- "$SELF")" && pwd) ;;
    *)   APP_DIR=$(cd -- "$(dirname -- "$(command -v -- "$SELF")")" && pwd) ;;
esac

PYTHON="${PYTHON:-python3}"
GUI=1
for arg in "$@"; do
    case "$arg" in
        --no-gui|--check) GUI=0 ;;
    esac
done

fail() {
    printf 'FFprog: %s\n' "$1" >&2
    if [ -n "${2:-}" ]; then
        printf '  → %s\n' "$2" >&2
    fi
    exit 1
}

# --- сам файл программы ----------------------------------------------------
# Главный признак «перенесли не то»: рядом с run.sh нет app.py.
[ -f "$APP_DIR/app.py" ] ||
    fail "рядом с run.sh не найден app.py (каталог: $APP_DIR)" \
         "скопируйте всю папку проекта целиком, а не только run.sh"

# --- python ----------------------------------------------------------------
command -v "$PYTHON" >/dev/null 2>&1 ||
    fail "не найден $PYTHON в PATH" "установите Python 3 (python3)"

# Tkinter — отдельный пакет, и на «голом» хосте его чаще всего нет.
# Без этой проверки программа падала с непонятным ModuleNotFoundError.
if [ "$GUI" = "1" ] && ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    fail "для $PYTHON недоступен Tkinter (модуль tkinter)" \
         "Debian/Ubuntu: sudo apt install python3-tk | Fedora: sudo dnf install python3-tkinter | Arch: sudo pacman -S tk"
fi

# --- ffmpeg ----------------------------------------------------------------
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    fail "в PATH не найдены ffmpeg/ffprobe" \
         "Debian/Ubuntu: sudo apt install ffmpeg | Fedora: sudo dnf install ffmpeg | Arch: sudo pacman -S ffmpeg"
fi

# --- дисплей ---------------------------------------------------------------
# Типичная ситуация при переносе: запуск по ssh или в systemd-сессии без X.
if [ "$GUI" = "1" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    fail "DISPLAY не задан — открыть окно некуда" \
         "запустите из графической сессии, либо экспортируйте DISPLAY (например: DISPLAY=:1 ./run.sh)"
fi

if [ "$GUI" = "0" ]; then
    printf 'OK: каталог %s, python=%s, ffmpeg=%s\n' \
        "$APP_DIR" "$("$PYTHON" -V 2>&1)" "$(ffmpeg -version | head -1 | cut -d' ' -f3)"
    exit 0
fi

exec "$PYTHON" "$APP_DIR/app.py" "$@"