#!/usr/bin/env bash
# Entrypoint образа FFprog.
#
# Работает в три стадии, чтобы избежать рекурсии:
#   STAGE=0 — если есть tini, становимся под него (PID 1, сбор зомби ffmpeg);
#   STAGE=1 — под root приводим UID/GID пользователя ffprog к значениям хоста
#             (HOST_UID/HOST_GID) и понижаем привилегии;
#   STAGE=2 — от имени непривилегированного пользователя исполняем действие.
#
# Благодаря этому файлы результата в примонтированных папках принадлежат вашему
# обычному пользователю на хосте, а не root.
set -euo pipefail

APP_DIR="${FFPROG_APP_DIR:-/opt/ffprog}"
HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"
HOST_USER="${HOST_USER:-}"
RUN_AS="ffprog"
SELF="$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")"
STAGE="${FFPROG_STAGE:-0}"

log() { printf '[ffprog-entrypoint] %s\n' "$*" >&2; }

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

# ------------------------------------------------------------------- действия
dispatch() {
    local action="${1:-gui}"
    shift || true
    case "$action" in
        gui)
            exec /usr/bin/python3 "$APP_DIR/app.py" "$@"
            ;;
        smoke)
            exec /usr/bin/python3 "$APP_DIR/docker/smoke_ffmpeg.py" "$@"
            ;;
        test|tests)
            exec /usr/bin/python3 -m unittest discover -s "$APP_DIR/tests" -v "$@"
            ;;
        version)
            printf 'FFprog image\n'
            /usr/bin/python3 --version
            ffmpeg -version | head -1
            ffprobe -version | head -1
            printf 'Tk: '
            /usr/bin/python3 -c 'import tkinter; print(tkinter.TkVersion)' 2>/dev/null || printf 'нет\n'
            printf 'ffmpeg в PATH: %s\n' "$(command -v ffmpeg || echo 'не найден')"
            ;;
        bash|sh)
            exec /bin/bash "$@"
            ;;
        *)
            exec "$action" "$@"
            ;;
    esac
}

# --------------------------------------------------------------- STAGE 2: run
if [[ "$STAGE" == "2" ]]; then
    dispatch "$@"
fi

# ------------------------------------------------------- STAGE 0: init (tini)
if [[ "$STAGE" == "0" ]]; then
    export FFPROG_STAGE=1
    if command -v tini >/dev/null 2>&1; then
        exec tini -s -- "$SELF" "$@"
    fi
fi

# ---------------------------------------- STAGE 1: root-prepare + drop privs
if [[ "$(id -u)" != "0" ]]; then
    # Контейнер запущен с готовым --user: менять ID некому, сразу к действию.
    export FFPROG_STAGE=2
    dispatch "$@"
fi

if ! is_uint "$HOST_GID" || ! is_uint "$HOST_UID"; then
    log "HOST_UID/HOST_GID должны быть числами, использую 1000"
    HOST_UID=1000
    HOST_GID=1000
fi

# --- UID/GID: освобождаем занятый идентификатор, затем переназначаем ffprog
if [[ "$HOST_GID" != "0" ]]; then
    if owner_group="$(getent group "$HOST_GID" || true)"; then
        current="$(printf '%s' "$owner_group" | cut -d: -f1)"
        [[ -n "$current" && "$current" != "$RUN_AS" ]] \
            && groupmod -o -g "$HOST_GID" "$current" 2>/dev/null || true
    fi
    groupmod -g "$HOST_GID" "$RUN_AS" 2>/dev/null || true
fi

if [[ "$HOST_UID" != "0" ]]; then
    if owner_user="$(getent passwd "$HOST_UID" || true)"; then
        current="$(printf '%s' "$owner_user" | cut -d: -f1)"
        [[ -n "$current" && "$current" != "$RUN_AS" ]] \
            && usermod -o -u "$HOST_UID" "$current" 2>/dev/null || true
    fi
    usermod -o -u "$HOST_UID" "$RUN_AS" 2>/dev/null || true
fi

# --- домашний каталог: если папка хоста примонтирована по тому же пути,
#     используем её — диалог «Поиск…» откроется в привычных разделах.
HOME_DIR=""
if [[ -n "$HOST_USER" && -d "/home/${HOST_USER}" ]]; then
    HOME_DIR="/home/${HOST_USER}"
elif [[ -d "/home/$RUN_AS" ]]; then
    HOME_DIR="/home/$RUN_AS"
else
    HOME_DIR="/home/ffprog"
fi
mkdir -p "$HOME_DIR"
usermod -d "$HOME_DIR" "$RUN_AS" 2>/dev/null || true
# Владелец ставится только на сам каталог: рекурсивный chown примонтированной
# домашней папки трогал бы чужие файлы.
chown "$RUN_AS:$RUN_AS" "$HOME_DIR" 2>/dev/null || true
chown -R "$RUN_AS:$RUN_AS" "$APP_DIR" 2>/dev/null || true

# --- окружение
export HOME="$HOME_DIR"
export TMPDIR="${TMPDIR:-/tmp}"
export XDG_RUNTIME_DIR="$TMPDIR/ffprog-runtime-$$"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
chown "$RUN_AS:$RUN_AS" "$XDG_RUNTIME_DIR" 2>/dev/null || true
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export GDK_BACKEND=x11
if [[ -z "${XAUTHORITY:-}" && -f "$HOME/.Xauthority" ]]; then
    export XAUTHORITY="$HOME/.Xauthority"
fi

log "uid=$(id -u "$RUN_AS") gid=$(id -g "$RUN_AS") HOME=$HOME DISPLAY=${DISPLAY:-<нет>} XAUTHORITY=${XAUTHORITY:-<нет>}"

# --- понижение привилегий: runuser (util-linux) -> setpriv -> su
export FFPROG_STAGE=2
if command -v runuser >/dev/null 2>&1; then
    exec runuser -m -u "$RUN_AS" -- "$SELF" "$@"
elif command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid "$RUN_AS" --regid "$RUN_AS" --init-groups env "$SELF" "$@"
else
    exec su "$RUN_AS" --session-command="$(printf '%q ' "$SELF" "$@")"
fi