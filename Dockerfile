# FFprog — самодостаточный образ: Python + Tkinter + FFmpeg + всё, что нужно GUI.
# База trixie (ffmpeg 7.x). Собирается только под linux/amd64.
ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}

ARG APP_VERSION=1.0.0
ARG APP_DIR=/opt/ffprog

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=ru_RU.UTF-8 \
    LC_ALL=ru_RU.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Состав образа по назначению:
#   python3/python3-tk      — среда исполнения и Tkinter (GUI программы)
#   ffmpeg                  — движок обработки; даёт libx264, libvpx-vp9, mpeg4,
#                             libmp3lame, aac, flac, libvorbis, libopus, pcm_s16le
#   xdg-utils + pcmanfm     — кнопка «Открыть папку» (xdg-open -> файловый менеджер)
#   dbus-x11                — xdg-open иногда вызывает dbus-send
#   xauth / x11-*           — подключение окна к X-серверу хоста
#   locales + fonts-*       — кириллица в путях, заголовках и шрифтах Tk
#   ca-certificates/curl    — сетевые проверки при диагностике образа
#   file/shared-mime-info   — корректные MIME-ассоциации для xdg-запусков
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl locales procps util-linux \
      python3 python3-tk \
      ffmpeg \
      xdg-utils xdg-user-dirs pcmanfm dbus-x11 \
      xauth x11-utils x11-xserver-utils \
      libx11-6 libxft2 libxrender1 libxss1 \
      fonts-dejavu-core fonts-dejavu-extra fonts-liberation \
      shared-mime-info file \
 && sed -i 's/^# *ru_RU.UTF-8 UTF-8/ru_RU.UTF-8 UTF-8/; s/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
 && locale-gen \
 && fc-cache -f >/dev/null 2>&1 || true \
 && update-mime-database /usr/share/mime >/dev/null 2>&1 || true \
 && rm -rf /var/lib/apt/lists/*

# Пользователь ffprog. Реальные UID/GID проставляет entrypoint из HOST_UID/HOST_GID,
# чтобы файлы результата в примонтированных папках принадлежали вашему пользователю
# на хосте, а не root.
RUN useradd --create-home --shell /bin/bash --uid 1000 ffprog

WORKDIR ${APP_DIR}

COPY app.py ffmpeg_engine.py ffprog.svg ${APP_DIR}/
COPY tests/ ${APP_DIR}/tests/
COPY docker/smoke_ffmpeg.py ${APP_DIR}/docker/smoke_ffmpeg.py
COPY docker/docker-entrypoint.sh /usr/local/bin/ffprog-entrypoint

RUN chmod 755 /usr/local/bin/ffprog-entrypoint \
 && chmod +x ${APP_DIR}/docker/smoke_ffmpeg.py \
 && mkdir -p ${APP_DIR}/logs \
 && chown -R ffprog:ffprog ${APP_DIR}

ENV FFPROG_APP_DIR=${APP_DIR} \
    HOME=/home/ffprog \
    TMPDIR=/tmp

ENTRYPOINT ["/usr/local/bin/ffprog-entrypoint"]
CMD ["gui"]

LABEL org.opencontainers.image.title="FFprog" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="GUI-конвертер медиа на FFmpeg: монолитный образ с Python, Tkinter и FFmpeg"