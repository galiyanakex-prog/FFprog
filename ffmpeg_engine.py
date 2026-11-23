"""Построение и выполнение команд FFmpeg для GUI-конвертера.

Модуль не зависит от tkinter, чтобы команды можно было проверять тестами.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

VIDEO_FORMATS: dict[str, str] = {
    "mp4": "libx264",
    "mkv": "libx264",
    "mov": "libx264",
    "avi": "mpeg4",
    "webm": "libvpx-vp9",
}

AUDIO_FORMATS: dict[str, str] = {
    "mp3": "libmp3lame",
    "aac": "aac",
    "m4a": "aac",
    "flac": "flac",
    "ogg": "libvorbis",
    "opus": "libopus",
    "wav": "pcm_s16le",
}

# Кодеки без битрейта: качество задаётся только разрядностью/сжатием.
LOSSLESS_CODECS = {"flac", "pcm_s16le"}

VIDEO_QUALITIES: dict[str, int] = {
    "Максимальное": 16,
    "Высокое": 18,
    "Среднее": 23,
    "Низкое": 28,
}

AUDIO_QUALITIES: dict[str, str] = {
    "Максимальное": "320k",
    "Высокое": "192k",
    "Среднее": "128k",
    "Низкое": "96k",
    "Сверх Низкое": "48k",
}

# Качество по умолчанию: видео — «Высокое» (CRF 18), аудио — «Среднее» (128k).
DEFAULT_VIDEO_QUALITY = "Высокое"
DEFAULT_AUDIO_QUALITY = "Среднее"

# Специальный вариант списка качества: дорожку не трогать, переносить как есть.
# Нужен там, где перекодирование не обязательно (например, шаг «Громкость»).
COPY_QUALITY = "Без изменений"

# Пустой формат в «Конвертации» = «— не выбран —»: картинку вырезаем,
# а звук оставляем в контейнере, который диктует расширение результата.
AUDIO_ONLY_FORMAT = ""

CONTAINER_AUDIO_CODEC: dict[str, str] = {
    "mp4": "aac",
    "mov": "aac",
    "mkv": "aac",
    "avi": "libmp3lame",
    "webm": "libopus",
}


class EngineError(RuntimeError):
    """Ошибка, которую можно показать пользователю в messagebox."""


class CancelledError(EngineError):
    """Пользователь прервал обработку."""


# --------------------------------------------------------------------------- #
# Шаги преобразования
# --------------------------------------------------------------------------- #


@dataclass
class TrimStep:
    start: float
    end: float
    reencode: bool = True
    video_quality: str = DEFAULT_VIDEO_QUALITY
    audio_quality: str = DEFAULT_AUDIO_QUALITY

    label = "Обрезка"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise EngineError("Конец фрагмента должен быть больше начала")


@dataclass
class ConvertStep:
    fmt: str
    quality: str = ""
    video_quality: str = DEFAULT_VIDEO_QUALITY
    audio_quality: str = DEFAULT_AUDIO_QUALITY

    label = "Конвертация"

    def __post_init__(self) -> None:
        self.fmt = self.fmt.strip().lower().lstrip(".")
        # Пустой формат — это «— не выбран —»: остаётся только звук.
        if self.fmt and self.fmt not in VIDEO_FORMATS and self.fmt not in AUDIO_FORMATS:
            raise EngineError(f"Неизвестный формат вывода: {self.fmt!r}")
        # Старое единое «качество формата» раскладываем по типам дорожек,
        # чтобы у конвертации были те же два параметра, что и у прочих шагов.
        if self.quality:
            if self.fmt in VIDEO_FORMATS:
                self.video_quality = self.quality
            else:
                self.audio_quality = self.quality


@dataclass
class ConcatStep:
    files: List[str] = field(default_factory=list)
    video_quality: str = DEFAULT_VIDEO_QUALITY
    audio_quality: str = DEFAULT_AUDIO_QUALITY

    label = "Склеивание"


@dataclass
class VolumeStep:
    value: str
    # Громкость не требует трогать картинку, поэтому видео по умолчанию
    # переносится без перекодирования — шаг остаётся быстрым.
    video_quality: str = COPY_QUALITY
    audio_quality: str = DEFAULT_AUDIO_QUALITY

    label = "Громкость"


@dataclass
class SpeedStep:
    factor: float
    video_quality: str = DEFAULT_VIDEO_QUALITY
    audio_quality: str = DEFAULT_AUDIO_QUALITY

    label = "Скорость"

    def __post_init__(self) -> None:
        if self.factor <= 0:
            raise EngineError("Скорость должна быть больше нуля")


@dataclass
class DownloadStep:
    url: str
    # Готовая строка для `-f`: id варианта с сайта, «137+bestaudio/137» или
    # «bestvideo+bestaudio/best». Пусто — брать лучшее целиком.
    format_spec: str = ""
    # Контейнер, в который yt-dlp склеит раздельные видео и звук.
    merge_ext: str = ""
    # Контейнер для remux: нужен, когда поток с сайта лежит в другом контейнере,
    # чем имя выбранного файла. Перекодирования при этом не происходит.
    remux_ext: str = ""
    output_path: str = ""

    label = "Скачать по ссылке"

    def __post_init__(self) -> None:
        self.format_spec = self.format_spec.strip()
        self.merge_ext = self.merge_ext.strip().lower().lstrip(".")
        self.remux_ext = self.remux_ext.strip().lower().lstrip(".")
        if not self.url.strip():
            raise EngineError("Укажите ссылку для загрузки")
        for name in ("merge_ext", "remux_ext"):
            value = getattr(self, name)
            if value and value not in VIDEO_FORMATS:
                raise EngineError(
                    f"Неизвестный видеоконтейнер ({name}): {value!r}")


# --------------------------------------------------------------------------- #
# Загрузка медиа по URL
# --------------------------------------------------------------------------- #

DOWNLOAD_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Специальный пункт списка вариантов: не выбирать конкретный поток, а дать
# yt-dlp взять лучшее видео+звук сам.
DOWNLOAD_BEST = "Лучшее доступное"

# Расширения контейнеров, которые FFprog понимает как видео.
_NATIVE_VIDEO_EXTS = set(VIDEO_FORMATS)
_NATIVE_AUDIO_EXTS = set(AUDIO_FORMATS)


@dataclass
class FormatOption:
    """Один доступный на сайте вариант файла."""
    format_id: str
    ext: str = ""
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: str = "none"
    acodec: str = "none"
    bitrate: Optional[float] = None
    size: Optional[int] = None
    note: str = ""

    @property
    def is_video(self) -> bool:
        return self.vcodec not in ("", "none")

    @property
    def is_audio(self) -> bool:
        return self.acodec not in ("", "none")

    @property
    def label(self) -> str:
        """Понятная строка для выпадающего списка."""
        parts = []
        if self.is_video and self.height:
            parts.append(f"{self.height}p")
        elif self.is_video:
            parts.append("видео")
        if self.is_video and self.is_audio:
            parts.append("видео+звук")
        elif self.is_audio:
            parts.append("только звук")
        if self.ext:
            parts.append(self.ext.upper())
        if self.bitrate:
            parts.append(_human_bitrate(self.bitrate))
        if self.size:
            parts.append(_human_size(self.size))
        if self.note:
            parts.append(self.note)
        text = " · ".join(p for p in parts if p)
        return f"{text}  [{self.format_id}]" if text else self.format_id


def _human_bitrate(value: Optional[float]) -> str:
    if not value:
        return ""
    return f"{int(round(value))} кбит/с"


def _human_size(value: Optional[int]) -> str:
    if not value:
        return ""
    size = float(value)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if size < 1024 or unit == "ГиБ":
            return f"≈{size:.0f} {unit}" if size >= 10 else f"≈{size:.1f} {unit}"
        size /= 1024
    return ""


def _clean_codec(value: Optional[str]) -> str:
    """yt-dlp пишет кодек как 'av01.0.08M.08' — пользователю нужен профиль."""
    return (value or "none").split(".")[0]


def parse_formats(info: dict) -> List[FormatOption]:
    """Превращает JSON от `yt-dlp -J` в список понятных вариантов."""
    options: List[FormatOption] = []
    for item in info.get("formats") or []:
        if not item.get("format_id"):
            continue
        option = FormatOption(
            format_id=str(item["format_id"]),
            ext=str(item.get("ext") or "").lower(),
            height=item.get("height"),
            fps=item.get("fps"),
            vcodec=_clean_codec(item.get("vcodec")),
            acodec=_clean_codec(item.get("acodec")),
            bitrate=item.get("tbr") or item.get("abr"),
            size=item.get("filesize") or item.get("filesize_approx"),
            note=str(item.get("format_note") or ""),
        )
        # Пустые потоки без кодеков сайтом не отдаются как файл.
        if not option.is_video and not option.is_audio:
            continue
        options.append(option)
    return _dedupe_formats(options)


def _dedupe_formats(options: List[FormatOption]) -> List[FormatOption]:
    """Убирает повторы «того же» варианта, различающиеся только фреймрейтом.

    Сайты часто отдают 1080p30 и 1080p60 одним и тем же id-потоком; список не
    должен превращаться в простыню, поэтому оставляем первый вариант пары
    (разрешение, контейнер, кодеки).
    """
    seen: set = set()
    result: List[FormatOption] = []
    for option in sorted(options, key=lambda o: (-(o.height or 0),
                                                 -(o.bitrate or 0))):
        key = (option.height, option.ext, option.vcodec, option.acodec,
               option.is_video)
        if option.is_video and key in seen:
            continue
        seen.add(key)
        result.append(option)
    return result


def format_choices(options: Sequence[FormatOption]) -> List[str]:
    """Строки для выпадающего списка: видео, затем только звук, затем «Лучшее».

    Разделителей-заголовков здесь нет: подпись каждого варианта уже содержит
    «видео+звук» или «только звук», а выбираемый пункт не должен быть строкой,
    которую невозможно скачать.
    """
    labels = [o.label for o in options if o.is_video]
    labels += [o.label for o in options if o.is_audio and not o.is_video]
    labels.append(DOWNLOAD_BEST)
    return labels


def find_format(options: Sequence[FormatOption],
                label: str) -> Optional[FormatOption]:
    """Вариант по подписи из списка (или None, если выбрано «Лучшее»)."""
    label = (label or "").strip()
    if not label or label == DOWNLOAD_BEST:
        return None
    for option in options:
        if option.label == label:
            return option
    return None


def build_format_spec(option: Optional[FormatOption]) -> str:
    """Строка `-f` для выбранного варианта.

    Видео без звука берём вместе с лучшим аудиопотоком — иначе файл выйдет
    без дорожки. Это единственная «сильная необходимость» что-то объединять:
    во всех остальных случаях поток забирается с сайта как есть.
    """
    if option is None:
        return "bestvideo+bestaudio/best"
    if option.is_video and not option.is_audio:
        return f"{option.format_id}+bestaudio/{option.format_id}"
    return option.format_id


def target_container(output_path: str) -> str:
    """Контейнер, который пользователь запросил именем файла результата."""
    ext = os.path.splitext(output_path)[1].lstrip(".").lower()
    if ext in VIDEO_FORMATS or ext in AUDIO_FORMATS:
        return ext
    return ""


def plan_download(option: Optional[FormatOption], output_path: str) -> tuple:
    """Параметры скачивания: (строка `-f`, контейнер склейки, контейнер remux).

    Правило «берём то, что есть на сайте» означает отсутствие перекодирования
    при загрузке. Remux просим только для видео: звуковые форматы вроде mp3 —
    не контейнер, и упаковать в них готовый поток без перекодирования нельзя.
    Формат и качество звука выбираются самим вариантом на сайте, поэтому
    перекодирование звука здесь не предусмотрено вовсе.
    """
    spec = build_format_spec(option)
    wanted = target_container(output_path)

    if "+" in spec:
        # Раздельные видео и звук обязаны склеиться в один файл.
        return spec, wanted if wanted in VIDEO_FORMATS else "mp4", ""
    if (option is not None and option.is_video and wanted in VIDEO_FORMATS
            and option.ext and option.ext != wanted):
        return spec, "", wanted
    return spec, "", ""


def build_download_args(url: str, output_path: str,
                        format_spec: str = "",
                        merge_ext: str = "",
                        remux_ext: str = "") -> List[str]:
    """Собирает аргументы yt-dlp для выбранного варианта (без запуска).

    Ключевое правило: пока хватает того, что отдаёт сайт, ничего не
    перекодируем. Контейнер подклеиваем только когда видео и звук лежат
    раздельно и их обязательно надо объединить.
    """
    if not url.strip():
        raise EngineError("Укажите ссылку для загрузки")

    args = ["--no-playlist", "--newline"]
    spec = (format_spec or "").strip() or "bestvideo+bestaudio/best"
    args += ["-f", spec]
    # "+": в запросе несколько потоков — без явного контейнера yt-dlp может
    # выбрать mkv даже там, где пользователь рассчитывает на mp4.
    if merge_ext and "+" in spec:
        args += ["--merge-output-format", merge_ext]
    elif remux_ext:
        args += ["--remux-video", remux_ext]

    args += ["-o", output_path, url]
    return args


def list_download_formats(
    url: str,
    log: Optional[LogCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> List[FormatOption]:
    """Спрашивает сайт, какие варианты файлов доступны.

    Без yt-dlp список получить нельзя — возвращаем пустой, и интерфейс
    предложит скачать «Лучшее доступное» напрямую.
    """
    log = log or (lambda _msg: None)
    if not url.strip():
        raise EngineError("Укажите ссылку для загрузки")

    yt_dlp_path = shutil.which("yt-dlp")
    if yt_dlp_path is None:
        raise EngineError(
            "Чтобы увидеть доступные варианты, нужен yt-dlp. "
            "Установите его: sudo apt install yt-dlp (или pip install yt-dlp).")

    cmd = [yt_dlp_path, "--no-playlist", "-J", url]
    log("$ " + " ".join(_quote(part) for part in cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=60)
    except OSError as exc:
        raise EngineError(f"Не удалось запустить yt-dlp: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EngineError("Сайт не ответил за 60 секунд — попробуйте позже") from exc

    if should_cancel and should_cancel():
        raise CancelledError("Получение списка вариантов прервано")
    if proc.returncode != 0:
        raise EngineError(
            f"yt-dlp не смог прочитать ссылку (код {proc.returncode}). "
            "Проверьте адрес или обновите yt-dlp командой 'yt-dlp -U'.")
    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise EngineError("Не удалось разобрать ответ сайта") from exc
    # Для плейлиста yt-dlp отдаёт массив записей; нам нужна первая —
    # --no-playlist не спасает, когда ссылаются прямо на плейлист.
    if isinstance(info, list):
        info = info[0] if info else {}
    return parse_formats(info)


def download_media(
    url: str,
    output_path: str,
    format_spec: str = "",
    merge_ext: str = "",
    remux_ext: str = "",
    log: Optional[LogCallback] = None,
    should_cancel: Optional[CancelCheck] = None,
) -> str:
    """Скачать выбранный вариант по URL через yt-dlp (или fallback на urllib).

    Возвращает путь к сохранённому файлу. Перекодирования здесь нет вовсе:
    формат и качество даёт сам сайт, а файл с сайта остаётся как есть —
    максимум выполняется remux (переупаковка без перекодирования). Если нужен
    другой формат или качество, это делает шаг «Конвертация» уже после
    скачивания.
    """
    log = log or (lambda _msg: None)
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    yt_dlp_path = shutil.which("yt-dlp")
    if yt_dlp_path is None:
        return _download_direct(url, output_path, log, should_cancel)

    args = build_download_args(url, output_path, format_spec, merge_ext,
                                remux_ext)
    _run_yt_dlp(yt_dlp_path, args, log, should_cancel)
    downloaded = _resolve_downloaded(
        output_path, remux_ext or merge_ext or "mp4")
    log(f"Загружено: {downloaded}")
    return downloaded


def _run_yt_dlp(yt_dlp_path: str, args: List[str], log: LogCallback,
               should_cancel: Optional[CancelCheck]) -> None:
    """Запускает yt-dlp, пишет в журнал прогресс и реагирует на отмену."""
    cmd = [yt_dlp_path, *args]
    log("$ " + " ".join(_quote(part) for part in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        raise EngineError(f"Не удалось запустить yt-dlp: {exc}") from exc

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait()
                raise CancelledError("Загрузка прервана пользователем")
            line = line.strip()
            if line:
                log(line)
    finally:
        proc.stdout.close()
    returncode = proc.wait()

    if returncode != 0:
        raise EngineError(
            f"yt-dlp завершился с кодом {returncode}. Проверьте ссылку и "
            f"попробуйте обновить yt-dlp командой 'yt-dlp -U'.")


def _resolve_downloaded(output_path: str, fmt: str) -> str:
    """yt-dlp иногда дописывает расширение — находим реальный файл."""
    if os.path.isfile(output_path):
        return output_path
    base, _ext = os.path.splitext(output_path)
    for candidate_ext in (f".{fmt}", ".mp4", ".mkv", ".webm", ".mp3",
                          ".m4a", ".opus", ".ogg", ".flac", ".wav"):
        candidate = base + candidate_ext
        if os.path.isfile(candidate):
            return candidate
    raise EngineError(f"Не удалось найти загруженный файл: {output_path}")


def _download_direct(url: str, output_path: str,
                     log: LogCallback,
                     should_cancel: Optional[CancelCheck]) -> str:
    """Загрузка по прямой ссылке без yt-dlp (прямые .mp4/.mp3 и т.п.)."""
    parsed = url.split("?")[0].split("#")[0]
    ext = os.path.splitext(parsed)[1].lower()
    site = parsed.split("/")[2] if "//" in parsed else parsed
    if ext not in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".aac",
                   ".m4a", ".flac", ".ogg", ".opus", ".wav"):
        raise EngineError(
            "Для загрузки с VK, YouTube и подобных сайтов нужен yt-dlp. "
            "Установите его: sudo apt install yt-dlp (или pip install yt-dlp), "
            "после чего перезапустите FFprog.")

    request = urllib.request.Request(url, headers={"User-Agent": DOWNLOAD_UA})
    try:
        with urllib.request.urlopen(request) as response, \
                open(output_path, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if should_cancel and should_cancel():
                    raise CancelledError("Загрузка прервана пользователем")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    log(f"… {done // 1024} КиБ из {total // 1024} КиБ")
    except urllib.error.URLError as exc:
        raise EngineError(f"Не удалось скачать {url}: {exc}") from exc
    except OSError as exc:
        raise EngineError(f"Ошибка записи файла загрузки: {exc}") from exc

    log(f"Загружено: {output_path}")
    return output_path


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def audio_bitrate(quality: Optional[str]) -> str:
    """Битрейт по названию качества; неизвестное имя → среднее."""
    return AUDIO_QUALITIES.get(quality or "", AUDIO_QUALITIES[DEFAULT_AUDIO_QUALITY])


def step_qualities(step: object) -> tuple:
    """Пара (видеокачество, аудиокачество) шага; у старых шагов — дефолты."""
    return (getattr(step, "video_quality", DEFAULT_VIDEO_QUALITY),
            getattr(step, "audio_quality", DEFAULT_AUDIO_QUALITY))


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def check_binaries() -> None:
    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            raise EngineError(f"Не найдена утилита {tool!r} в PATH. Установите FFmpeg.")


def parse_time(value: str) -> float:
    """Принимает '90', '1:30', '01:02:03.5' и возвращает секунды."""
    text = (value or "").strip().replace(",", ".")
    if not text:
        raise EngineError("Укажите время в формате СС, ММ:СС или ЧЧ:ММ:СС")
    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise EngineError(f"Не удалось понять время: {value!r}")
        total = 0.0
        for part in parts:
            try:
                total = total * 60 + float(part)
            except ValueError as exc:
                raise EngineError(f"Не удалось понять время: {value!r}") from exc
        return total
    try:
        return float(text)
    except ValueError as exc:
        raise EngineError(f"Не удалось понять время: {value!r}") from exc


def _num(value: float) -> str:
    """Короткая запись числа без потери нужного знака (2.0, а не 2)."""
    text = f"{value:.6f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def parse_factor(value: str) -> float:
    """Чистое число без разбора времени (для множителя скорости)."""
    text = (value or "").strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError as exc:
        raise EngineError(f"Ожидается число, получено: {value!r}") from exc
    if number <= 0:
        raise EngineError("Значение должно быть больше нуля")
    return number


def atempo_chain(factor: float) -> str:
    """atsolo фильтр atempo принимает только 0.5..2.0, поэтому длинные
    множители раскладываются в цепочку."""
    if factor <= 0:
        raise EngineError("Скорость должна быть больше нуля")
    filters: List[str] = []
    rest = factor
    while rest > 2.0:
        filters.append("atempo=2.0")
        rest /= 2.0
    while rest < 0.5:
        filters.append("atempo=0.5")
        rest /= 0.5
    filters.append(f"atempo={_num(rest)}")
    return ",".join(filters)


def normalize_volume(value: str) -> str:
    """Приводит ввод пользователя ('150%', '1,5') к числу для volume=."""
    text = (value or "").strip().replace(",", ".")
    if not text:
        raise EngineError("Укажите значение громкости (например 1.5 или 150%)")
    try:
        number = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
    except ValueError as exc:
        raise EngineError(f"Не удалось понять громкость: {value!r}") from exc
    if number < 0:
        raise EngineError("Громкость не может быть отрицательной")
    return _num(number)


def format_time(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def is_video_format(fmt: str) -> bool:
    return fmt in VIDEO_FORMATS


def output_extension(steps: Sequence[object], source: str) -> str:
    """Итоговое расширение: формат последней конвертации, иначе — как у входа.

    Пустой формат («— не выбран —», только звук) контейнер не меняет: звук
    остаётся в том же контейнере, из которого вырезали картинку.
    """
    for step in reversed(list(steps)):
        if isinstance(step, ConvertStep) and step.fmt:
            return "." + step.fmt
    ext = os.path.splitext(source)[1].lstrip(".").lower()
    return "." + ext if ext else ".mp4"


def container_ext(target: str) -> str:
    """Расширение целевого файла — оно определяет допустимые кодеки."""
    return os.path.splitext(target)[1].lstrip(".").lower()


def video_codec_for(ext: str) -> str:
    return VIDEO_FORMATS.get(ext, "libx264")


def audio_codec_for(ext: str) -> str:
    return CONTAINER_AUDIO_CODEC.get(ext) or AUDIO_FORMATS.get(ext) or "aac"


def video_encode_args(ext: str, quality: Optional[str] = None,
                      preset: str = "veryfast") -> List[str]:
    """Кодек видео под контейнер: x264 в mp4/mkv, vp9 в webm, mpeg4 в avi.

    «Без изменений» означает «не навязывать качество»: кодек по контейнеру
    берётся всегда (иначе он может не подойти расширению), а вот CRF/q:v
    добавляется только при явном выборе качества.
    """
    codec = video_codec_for(ext)
    args = ["-c:v", codec]
    if quality == COPY_QUALITY:
        return args
    if codec == "libx264":
        args += ["-preset", preset]
        if quality:
            args += ["-crf", str(VIDEO_QUALITIES.get(quality, 23))]
    elif codec == "libvpx-vp9":
        # у vp9 качество задаётся CRF, но целевой битрейт должен быть нулевым
        args += ["-crf", str(VIDEO_QUALITIES.get(quality, 32)), "-b:v", "0"]
    elif quality:
        args += ["-q:v", str(VIDEO_QUALITIES.get(quality, 23))]
    return args


def video_copy_or_encode_args(ext: str, quality: Optional[str]) -> List[str]:
    """Для шагов, где перекодирование видео необязательно (громкость):
    «Без изменений» → поток копируется байт-в-байт, иначе — кодируется."""
    if quality == COPY_QUALITY:
        return ["-c:v", "copy"]
    return video_encode_args(ext, quality)


def audio_encode_args(ext: str, bitrate: Optional[str] = None) -> List[str]:
    """Кодек аудио под контейнер; для flac/wav битрейт не задаётся."""
    codec = audio_codec_for(ext)
    args = ["-c:a", codec]
    if bitrate and codec not in LOSSLESS_CODECS:
        args += ["-b:a", bitrate]
    return args


# --------------------------------------------------------------------------- #
# Информация о медиафайле
# --------------------------------------------------------------------------- #


@dataclass
class Streams:
    has_video: bool = False
    has_audio: bool = False

    def describe(self) -> str:
        kinds = []
        if self.has_video:
            kinds.append("видео")
        if self.has_audio:
            kinds.append("аудио")
        return " + ".join(kinds) if kinds else "без потоков"


def probe_streams(path: str) -> Streams:
    if not os.path.isfile(path):
        raise EngineError(f"Файл не найден: {path}")
    cmd = [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineError(f"ffprobe не запустился: {exc}") from exc
    if result.returncode != 0:
        raise EngineError(f"Не удалось прочитать файл {os.path.basename(path)}: "
                          f"{result.stderr.strip() or 'ffprobe вернул ошибку'}")
    out = result.stdout.lower()
    return Streams(has_video="video" in out, has_audio="audio" in out)


# --------------------------------------------------------------------------- #
# Построение команд
# --------------------------------------------------------------------------- #


def build_convert_args(source: str, target: str, step: ConvertStep,
                       streams: Streams) -> List[str]:
    fmt = step.fmt.lower().lstrip(".")
    if fmt and fmt not in VIDEO_FORMATS and fmt not in AUDIO_FORMATS:
        raise EngineError(f"Неизвестный формат вывода: {fmt}")

    # Контейнер пишется по расширению цели; если расширения нет — по формату шага.
    ext = container_ext(target)
    if ext not in VIDEO_FORMATS and ext not in AUDIO_FORMATS:
        ext = fmt
    if not ext:
        raise EngineError(
            "Не выбран формат: укажите расширение файла результата")

    args = ["-y", "-i", source]
    if fmt in VIDEO_FORMATS:
        if not streams.has_video:
            raise EngineError(
                f"В файле нет видеопотока, конвертировать в {fmt.upper()} нельзя")
        args += video_encode_args(ext, step.video_quality)
        if streams.has_audio:
            args += audio_encode_args(ext, audio_bitrate(step.audio_quality))
        else:
            args += ["-an"]
    else:
        if not streams.has_audio:
            raise EngineError(
                "В файле нет аудиопотока — оставить только звук не получится")
        args += ["-vn"]
        args += audio_encode_args(ext, audio_bitrate(step.audio_quality))
    args.append(target)
    return args


def build_trim_args(source: str, target: str, step: TrimStep,
                    streams: Streams) -> List[str]:
    if step.end <= step.start:
        raise EngineError("Конец фрагмента должен быть больше начала")
    duration = step.end - step.start
    args = ["-y", "-ss", f"{step.start:.3f}", "-i", source,
            "-t", f"{duration:.3f}"]
    if step.reencode:
        # Кодек обязан соответствовать контейнеру цели: x264 в mp3/ogg/webm
        # невозможен, поэтому берём кодеки по расширению результата.
        ext = container_ext(target)
        if streams.has_video:
            args += video_encode_args(ext, step.video_quality)
        if streams.has_audio:
            args += audio_encode_args(ext, audio_bitrate(step.audio_quality))
    else:
        args += ["-c", "copy"]
    args.append(target)
    return args


def build_volume_args(source: str, target: str, step: VolumeStep,
                      streams: Streams) -> List[str]:
    if not streams.has_audio:
        raise EngineError("В файле нет аудиопотока — менять громкость нечего")
    vol = normalize_volume(step.value)
    ext = container_ext(target)
    # Фильтр громкости всегда перекодирует звук, поэтому битрейт управляемый.
    args = ["-y", "-i", source, "-af", f"volume={vol}"]
    args += audio_encode_args(ext, audio_bitrate(step.audio_quality))
    if streams.has_video:
        # Картинку по умолчанию не трогаем: quality == «Без изменений» → copy.
        args += video_copy_or_encode_args(ext, step.video_quality)
    args.append(target)
    return args


def build_speed_args(source: str, target: str, step: SpeedStep,
                     streams: Streams) -> List[str]:
    if not (streams.has_video or streams.has_audio):
        raise EngineError("В файле нет потоков для изменения скорости")
    ext = container_ext(target)
    args = ["-y", "-i", source]
    if streams.has_video:
        args += ["-filter:v", f"setpts=PTS/{step.factor:.6f}"]
        args += video_encode_args(ext, step.video_quality)
    if streams.has_audio:
        args += ["-filter:a", atempo_chain(step.factor)]
        args += audio_encode_args(ext, audio_bitrate(step.audio_quality))
    args.append(target)
    return args


def build_concat_list(files: Iterable[str], list_path: str) -> None:
    with open(list_path, "w", encoding="utf-8") as handle:
        for path in files:
            safe = os.path.abspath(path).replace("'", r"'\''")
            handle.write(f"file '{safe}'\n")


def build_concat_copy_args(list_path: str, target: str) -> List[str]:
    return ["-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", target]


def build_concat_filter_args(files: Sequence[str], target: str,
                             streams_by_file: Sequence[Streams],
                             video_quality: str = DEFAULT_VIDEO_QUALITY,
                             audio_quality: str = DEFAULT_AUDIO_QUALITY) -> List[str]:
    args: List[str] = ["-y"]
    for path in files:
        args += ["-i", path]
    for index, streams in enumerate(streams_by_file):
        if not streams.has_video and not streams.has_audio:
            raise EngineError(f"Файл {os.path.basename(files[index])} пуст")

    # Набор потоков должен совпадать: иначе фильтр concat ждёт одинаковые входы.
    with_video = sum(1 for s in streams_by_file if s.has_video)
    with_audio = sum(1 for s in streams_by_file if s.has_audio)
    total = len(streams_by_file)
    if with_video not in (0, total) or with_audio not in (0, total):
        raise EngineError(
            "Для склейки все файлы должны иметь одинаковый набор потоков "
            "(только видео или только аудио). Сначала приведите файлы к одному виду.")

    ext = container_ext(target)
    parts = [f"[{index}:{'v' if with_video else 'a'}]" for index in range(total)]
    sinks = ("[outv][outa]" if with_video and with_audio
             else "[outv]" if with_video else "[outa]")
    chains = "".join(parts) + (
        f"concat=n={total}:v={int(bool(with_video))}:a={int(bool(with_audio))}"
        + sinks)
    args += ["-filter_complex", chains]
    if with_video:
        args += ["-map", "[outv]"] + video_encode_args(ext, video_quality)
    if with_audio:
        args += ["-map", "[outa]"] + audio_encode_args(
            ext, audio_bitrate(audio_quality))
    args.append(target)
    return args


# --------------------------------------------------------------------------- #
# Выполнение
# --------------------------------------------------------------------------- #


def run_ffmpeg(args: Sequence[str], log: LogCallback,
               should_cancel: Optional[CancelCheck] = None) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", *args]
    log("$ " + " ".join(_quote(part) for part in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise EngineError(f"Не удалось запустить ffmpeg: {exc}") from exc

    if should_cancel is not None:
        while proc.poll() is None:
            if should_cancel():
                proc.kill()
                proc.wait()
                raise CancelledError("Обработка прервана пользователем")
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue

    stdout, stderr = proc.communicate()
    if stdout.strip():
        log(stdout.strip())
    if proc.returncode != 0:
        detail = (stderr or "").strip()
        if should_cancel is not None and should_cancel():
            raise CancelledError("Обработка прервана пользователем")
        raise EngineError(detail or f"ffmpeg завершился с кодом {proc.returncode}")


def _quote(part: str) -> str:
    return f'"{part}"' if any(ch in part for ch in ' "') else part


class Pipeline:
    """Прогоняет цепочку шагов, сохраняя промежуточные файлы в temp-каталоге."""

    def __init__(self, log: LogCallback,
                 should_cancel: Optional[CancelCheck] = None) -> None:
        self.log = log
        self.should_cancel = should_cancel

    def run(self, source: str, steps: Sequence[object], target: str) -> None:
        check_binaries()
        if not steps:
            raise EngineError("Не выбрано ни одного инструмента преобразования")
        # Первым шагом может быть загрузка по ссылке — тогда исходный файл
        # появится только после её выполнения, и проверять его заранее нельзя.
        starts_with_download = isinstance(steps[0], DownloadStep)
        if not starts_with_download and not os.path.isfile(source):
            raise EngineError(f"Исходный файл не найден: {source}")

        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="ffprog_")
        current = os.path.abspath(source)
        try:
            for index, step in enumerate(steps, start=1):
                if self.should_cancel and self.should_cancel():
                    raise CancelledError("Обработка прервана пользователем")
                last = index == len(steps)

                if isinstance(step, DownloadStep):
                    # Загрузка — отдельная ветка: здесь работает yt-dlp,
                    # а не ffmpeg, и потоки ещё неизвестны.
                    dest = target if last else os.path.join(
                        workdir, f"download_{index}")
                    self.log(f"--- Шаг {index}/{len(steps)}: {step.label} ---")
                    current = download_media(
                        step.url, dest, step.format_spec, step.merge_ext,
                        step.remux_ext, self.log, self.should_cancel)
                    continue

                streams = probe_streams(current)
                stage_target = target if last else self._temp_name(workdir, streams)
                self.log(f"--- Шаг {index}/{len(steps)}: "
                         f"{getattr(step, 'label', type(step).__name__)} "
                         f"({streams.describe()}) ---")
                if isinstance(step, ConcatStep):
                    run_concat_with_fallback(current, stage_target, step,
                                             self.log, self.should_cancel,
                                             workdir=workdir)
                else:
                    run_ffmpeg(self._build(current, stage_target, step, streams,
                                           workdir),
                               self.log, self.should_cancel)
                if not last:
                    current = stage_target
            self.log(f"Готово: {target}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _temp_name(self, workdir: str, streams: Streams) -> str:
        ext = ".mkv" if streams.has_video else ".mka"
        handle, path = tempfile.mkstemp(dir=workdir, suffix=ext)
        os.close(handle)
        return path

    def _build(self, source: str, target: str, step: object,
               streams: Streams, workdir: str) -> List[str]:
        if isinstance(step, TrimStep):
            return build_trim_args(source, target, step, streams)
        if isinstance(step, ConvertStep):
            return build_convert_args(source, target, step, streams)
        if isinstance(step, VolumeStep):
            return build_volume_args(source, target, step, streams)
        if isinstance(step, SpeedStep):
            return build_speed_args(source, target, step, streams)
        raise EngineError(f"Неизвестный инструмент: {step!r}")


def run_concat_with_fallback(source: str, target: str, step: ConcatStep,
                             log: LogCallback,
                             should_cancel: Optional[CancelCheck] = None,
                             workdir: Optional[str] = None) -> None:
    """Склейка: сначала быстрый режим `-c copy`, при неудаче — concat-фильтр
    с перекодированием. Список файлов строится как [исходный, ...добавленные]."""
    files = [source, *[p for p in step.files if p]]
    if len(files) < 2:
        raise EngineError("Для склеивания добавьте хотя бы один файл")
    for path in files[1:]:
        if not os.path.isfile(path):
            raise EngineError(f"Файл для склейки не найден: {path}")

    own_dir = workdir is None
    if own_dir:
        workdir = tempfile.mkdtemp(prefix="ffprog_concat_")
    try:
        list_path = os.path.join(workdir, "concat.txt")
        build_concat_list(files, list_path)
        try:
            run_ffmpeg(build_concat_copy_args(list_path, target), log,
                       should_cancel)
        except CancelledError:
            raise
        except EngineError as exc:
            if should_cancel and should_cancel():
                raise CancelledError("Обработка прервана пользователем") from exc
            log(f"Быстрая склейка не удалась ({exc}), "
                f"пересобираю с перекодированием…")
            streams_by_file = [probe_streams(p) for p in files]
            # Качество из шага применяется только здесь: при быстрой склейке
            # потоки копируются байт-в-байт и перекодирования нет.
            run_ffmpeg(build_concat_filter_args(
                files, target, streams_by_file,
                getattr(step, "video_quality", DEFAULT_VIDEO_QUALITY),
                getattr(step, "audio_quality", DEFAULT_AUDIO_QUALITY)),
                log, should_cancel)
    finally:
        if own_dir:
            shutil.rmtree(workdir, ignore_errors=True)
