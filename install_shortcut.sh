#!/usr/bin/env bash
# Ставит ярлык FFprog в меню приложений (запуск одним кликом).
#
# POSIX-совместим: не зависит от того, какой shell стоит в /bin/sh.
#
# Этот скрипт — «точка восстановления» после переноса: он чинит CRLF и бит
# исполнения у всех .sh в папке. Сам он тоже может прийти с Windows-переносами,
# поэтому починка идёт ПЕРВОЙ ЖЕ КОМАНДОЙ, до `set -eu` и до любого многострочного
# if/for: с `\r` на конце `set -eu` превращается в «Illegal option», а `then`/`do`
# перестают распознаваться. Всё в одной строке и с комментарием в конце.
grep -q "$(printf '\r')" "$0" 2>/dev/null && { sed -i 's/\r$//' -- "$0" 2>/dev/null && exec bash -- "$0" "$@"; } # heal-crlf
set -eu

SELF=$0
case $SELF in
    */*) APP_DIR=$(cd -- "$(dirname -- "$SELF")" && pwd) ;;
    *)   APP_DIR=$(cd -- "$(dirname -- "$(command -v -- "$SELF")")" && pwd) ;;
esac

TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$TARGET_DIR/ffprog.desktop"

[ -f "$APP_DIR/app.py" ] || {
    echo "FFprog: рядом с install_shortcut.sh нет app.py — скопируйте папку проекта целиком." >&2
    exit 1
}

# При переносе (zip / NTFS / флешка) бит исполнения теряется, и ярлык
# «Отказано в доступе» молча не стартует. Возвращаем его всем скриптам.
#
# Заодно снимаем windows-переносы строк со всех .sh в папке: run.sh с CRLF
# не способен починить себя сам при запуске вида ./run.sh — ядро читает
# shebang «#!/usr/bin/env bash\r» до выполнения любой команды скрипта.
for script in "$APP_DIR"/*.sh; do
    [ -f "$script" ] || continue
    grep -q "$(printf '\r')" "$script" 2>/dev/null &&
        sed -i 's/\r$//' -- "$script" 2>/dev/null || true
done

chmod +x "$APP_DIR"/*.sh 2>/dev/null || true

mkdir -p "$TARGET_DIR"

# В Exec= по спецификации desktop-entry пробелы и табы экранируются слэшем,
# а внешние кавычки ломали бы парсинг. Path= и Icon= — обычные строки,
# их пишут как есть (пути с пробелами и кириллицей там допустимы).
esc_exec() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/ /\\ /g' -e 's/\t/\\t/g'
}

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=FFprog
Comment=GUI-конвертер медиа на FFmpeg
Exec=$(esc_exec "$APP_DIR/run.sh")
Path=$APP_DIR
Icon=$APP_DIR/ffprog.svg
Terminal=false
Categories=AudioVideo;AudioVideoEditing;
EOF

chmod +x "$DESKTOP_FILE"
echo "Ярлык установлен: $DESKTOP_FILE"
echo "Проект: $APP_DIR"
echo "Если меню не обновилось: update-desktop-database ~/.local/share/applications"