#!/usr/bin/env bash
# Запуск GUI FFprog из Docker-образа на любом Linux-ПК (amd64).
#
# Скрипт сам определяет DISPLAY, UID/GID и домашнюю папку текущего пользователя,
# пробрасывает X11 и монтирует домашний каталог, чтобы диалог «Поиск…» видел
# ваши файлы, а результат сохранялся рядом с исходником.
#
#   ./run-ffprog.sh                 # окно программы
#   ./run-ffprog.sh smoke           # проверка самодостаточности образа
#   ./run-ffprog.sh test            # unit-тесты внутри образа
#   ./run-ffprog.sh version         # версии Python / Tk / FFmpeg в образе
#   ./run-ffprog.sh bash            # оболочка внутри образа
#   FFPROG_IMAGE=my/ffprog:1.0 ./run-ffprog.sh
#
# Скрипт использует массивы и [[ ]], то есть требует bash. Если его вызвали
# через /bin/sh (в Debian/Ubuntu это dash), перезапускаемся под bash — иначе
# на новом хосте получаем «Bad substitution» вместо окна программы.
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
set -euo pipefail

IMAGE="${FFPROG_IMAGE:-ffprog:latest}"
ACTION="${1:-gui}"
shift || true

if ! command -v docker >/dev/null 2>&1; then
    echo "Не найден docker. Установите Docker или запускайте ./run.sh напрямую." >&2
    exit 1
fi

# ------------------------------------------------------------- X11-доступ
if [[ -z "${DISPLAY:-}" ]]; then
    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
        # Wayland-сессия: Tk работает через XWayland, берём его DISPLAY.
        export DISPLAY=":0"
    else
        echo "DISPLAY не задан — графическое окно открыть некуда." >&2
        echo "Для проверки образа без GUI: $0 smoke | test | version" >&2
        exit 1
    fi
fi

X11_ARGS=(--env "DISPLAY=$DISPLAY" --volume /tmp/.X11-unix:/tmp/.X11-unix:rw)

# Разрешаем подключение из контейнера (для Xephyr/Xhost-закрытых сессий).
if command -v xhost >/dev/null 2>&1; then
    xhost +local: >/dev/null 2>&1 || true
    trap 'command -v xhost >/dev/null 2>&1 && xhost -local: >/dev/null 2>&1 || true' EXIT
fi

XAUTH_ARGS=()
if [[ -n "${XAUTHORITY:-}" && -r "${XAUTHORITY}" ]]; then
    XAUTH_ARGS=(--env "XAUTHORITY=/tmp/ffprog.Xauthority"
                --volume "${XAUTHORITY}:/tmp/ffprog.Xauthority:ro")
fi

# --------------------------------------------------------- пользователь/дом
UID_ARGS=(--env "HOST_UID=$(id -u)" --env "HOST_GID=$(id -g)"
          --env "HOST_USER=${USER:-$(id -un)}")

HOME_PATH="${HOME:-/home/$(id -un)}"
MOUNTS=(--volume "${HOME_PATH}:${HOME_PATH}")
# Дополнительные точки: съёмные носители и общие каталоги, если существуют.
for extra in /mnt /media /srv; do
    [[ -d "$extra" ]] && MOUNTS+=(--volume "$extra:$extra")
done

# ------------------------------------------------------------- образ есть?
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Образ $IMAGE не найден. Соберите: python3 build_image.py" >&2
    exit 1
fi

exec docker run --rm -it \
    --platform linux/amd64 \
    --network host \
    --shm-size 512m \
    "${X11_ARGS[@]}" "${XAUTH_ARGS[@]}" "${UID_ARGS[@]}" "${MOUNTS[@]}" \
    "$IMAGE" "$ACTION" "$@"
