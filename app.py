"""GUI-конвертер медиафайлов на базе FFmpeg.

Интерфейс: выбор входного файла, строки инструментов (одна видна сразу,
остальные добавляются кнопкой «+»), параметры выбранного инструмента рядом,
выбор файла результата, запуск цепочки преобразований и лог команд FFmpeg.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ffmpeg_engine as eng

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv",
             ".mpg", ".mpeg", ".ts"}
AUDIO_EXT = {".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".wma"}
FILE_TYPES = [
    ("Медиафайлы", sorted(VIDEO_EXT | AUDIO_EXT)),
    ("Видео", sorted(VIDEO_EXT)),
    ("Аудио", sorted(AUDIO_EXT)),
    ("Все файлы", ["*"]),
]

TOOLS = ["Обрезка", "Конвертация", "Склеивание", "Громкость", "Скорость"]
EMPTY_TOOL = "— не выбран —"
TOOL_CHOICES = [EMPTY_TOOL, *TOOLS]

# Вариант «Конечного формата»: картинку выкинуть, оставить только звук.
# Пустое значение понимает движок (см. eng.AUDIO_ONLY_FORMAT).
AUDIO_ONLY_CHOICE = "— только звук —"

# Разумный предел: больше десяти шагов в одном окне уже нечитаемо.
MAX_ROWS = 10


class RowPanel(ttk.Frame):
    """Одна строка: выпадающий список инструмента + набор фреймов параметров.

    Каждый список содержит полный перечень инструментов; показывается только
    тот фрейм параметров, который соответствует выбранному значению.
    """

    def __init__(self, master, index: int, on_change, on_pick_file,
                 on_remove=None) -> None:
        super().__init__(master, style="Row.TFrame")
        self.index = index
        self._on_change = on_change
        self._on_pick_file = on_pick_file

        self.var = tk.StringVar(value=EMPTY_TOOL)
        self.frames: dict[str, ttk.Frame] = {}
        # Качество хранится отдельно для каждого инструмента: цепочка может
        # обрезать в «Низком», а финальную конвертацию делать в «Максимальном».
        self.video_quality: dict[str, tk.StringVar] = {}
        self.audio_quality: dict[str, tk.StringVar] = {}
        self.combo_video_quality: dict[str, ttk.Combobox] = {}
        self.combo_audio_quality: dict[str, ttk.Combobox] = {}
        # Какие дорожки есть в исходном файле — по ним гасятся списки качества.
        self._has_video = True
        self._has_audio = True

        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)

        self.title = ttk.Label(self, text=f"Инструмент {index + 1}:",
                               style="RowLabel.TLabel")
        self.title.grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.combo = ttk.OptionMenu(self, self.var, EMPTY_TOOL, *TOOL_CHOICES,
                                    command=self._changed)
        self.combo.config(width=14)
        self.combo.grid(row=0, column=1, sticky="w")

        # Кнопка «−» появляется только у строк, добавленных кнопкой «+»:
        # базовая строка остаётся, чтобы окно не осталось совсем без шагов.
        self.remove_btn = None
        if on_remove is not None:
            self.remove_btn = ttk.Button(self, text="−", width=3,
                                         command=on_remove)
            self.remove_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")

        self._build_trim()
        self._build_convert()
        self._build_concat()
        self._build_volume()
        self._build_speed()

        # Реакция и на выбор из меню, и на программное изменение значения.
        self.var.trace_add("write", lambda *_: self._changed())
        self._changed(self.var.get())

    def set_index(self, index: int) -> None:
        """Перенумеровывает строку после добавления или удаления соседа."""
        self.index = index
        self.title.config(text=f"Инструмент {index + 1}:")

    # ---------------------------------------------------------------- строки
    def _add_quality_controls(self, frame: ttk.Frame, tool: str, row: int,
                              column: int, video_default: str,
                              video_values: list[str] | None = None) -> None:
        """Добавляет в фрейм инструмента пару «Качество видео / Качество звука».

        Списки общие для всех режимов: у видео это шкала CRF, у звука —
        битрейт. Доступность каждого списка зависит от дорожек исходника
        (см. `set_streams`): в аудиофайле выбор качества видео не нужен.
        """
        ttk.Label(frame, text="Качество видео:").grid(
            row=row, column=column, sticky="w", pady=(4, 0))
        video_var = tk.StringVar(value=video_default)
        video_combo = ttk.Combobox(frame, textvariable=video_var, width=12,
                                   state="readonly",
                                   values=(video_values or
                                           list(eng.VIDEO_QUALITIES)))
        video_combo.grid(row=row, column=column + 1, padx=(4, 12), pady=(4, 0))

        ttk.Label(frame, text="Качество звука:").grid(
            row=row, column=column + 2, sticky="w", pady=(4, 0))
        audio_var = tk.StringVar(value=eng.DEFAULT_AUDIO_QUALITY)
        audio_combo = ttk.Combobox(frame, textvariable=audio_var, width=12,
                                   state="readonly",
                                   values=list(eng.AUDIO_QUALITIES))
        audio_combo.grid(row=row, column=column + 3, padx=(4, 0), pady=(4, 0))

        self.video_quality[tool] = video_var
        self.audio_quality[tool] = audio_var
        self.combo_video_quality[tool] = video_combo
        self.combo_audio_quality[tool] = audio_combo
        self._sync_quality_state(tool)

    def _sync_quality_state(self, tool: str) -> None:
        """Гасит список качества, если соответствующей дорожки не будет на выходе.

        Для «Конвертации» ориентир — целевой формат (в аудиоформат видео не
        пишется), для остальных инструментов — дорожки исходного файла.
        """
        video_ok, audio_ok = self._has_video, self._has_audio
        if tool == "Конвертация":
            # В режим «только звук» и в любой аудиоформат видео не пишется.
            if self._is_audio_only_convert() or self.fmt.get() in eng.AUDIO_FORMATS:
                video_ok = False
            else:
                video_ok = self._has_video
        video = self.combo_video_quality.get(tool)
        audio = self.combo_audio_quality.get(tool)
        if video is not None:
            video.config(state="readonly" if video_ok else "disabled")
        if audio is not None:
            audio.config(state="readonly" if audio_ok else "disabled")

    def set_streams(self, streams: "eng.Streams | None") -> None:
        """Сообщает строке, какие дорожки есть в выбранном исходном файле."""
        self._has_video = True if streams is None else streams.has_video
        self._has_audio = True if streams is None else streams.has_audio
        for tool in self.frames:
            self._sync_quality_state(tool)

    def _changed(self, _value: str | None = None) -> None:
        tool = self.var.get()
        for name, frame in self.frames.items():
            if name == tool:
                frame.grid(row=1, column=0, columnspan=2, sticky="w",
                           pady=(4, 0))
            else:
                frame.grid_remove()
        if callable(self._on_change):
            self._on_change()

    def _build_trim(self) -> None:
        frame = ttk.Frame(self)
        ttk.Label(frame, text="Начало:").grid(
            row=0, column=0, sticky="w")
        self.trim_start = ttk.Entry(frame, width=12)
        self.trim_start.grid(row=0, column=1, padx=6)
        self.trim_start.insert(0, "00:00:00")
        ttk.Label(frame, text="Конец:").grid(
            row=0, column=2, sticky="w")
        self.trim_end = ttk.Entry(frame, width=12)
        self.trim_end.grid(row=0, column=3, padx=(6, 12))
        self.trim_end.insert(0, "00:00:10")
        ttk.Label(frame, text="(ЧЧ:ММ:СС)",
                  style="Hint.TLabel").grid(row=0, column=4, sticky="w")
        self._add_quality_controls(frame, "Обрезка", row=1, column=0,
                                   video_default=eng.DEFAULT_VIDEO_QUALITY)
        self.frames["Обрезка"] = frame

    def _build_convert(self) -> None:
        frame = ttk.Frame(self)
        ttk.Label(frame, text="Конечный формат:").grid(row=0, column=0,
                                                       sticky="w")
        self.fmt = tk.StringVar(value="mp4")
        combo_fmt = ttk.Combobox(frame, textvariable=self.fmt, width=14,
                                 state="readonly",
                                 values=[AUDIO_ONLY_CHOICE,
                                         *sorted(eng.VIDEO_FORMATS),
                                         *sorted(eng.AUDIO_FORMATS)])
        combo_fmt.grid(row=0, column=1, padx=6)
        # Пустой формат меняет только набор дорожек, поэтому расширение
        # результата остаётся прежним — подсказка объясняет это с первого взгляда.
        # Текст статичный, поэтому он задан через text=: вложенный StringVar,
        # не сохранённый в атрибут, сборщик мусора мог бы отделить от виджета.
        self.convert_hint = ttk.Label(
            frame, text=self._convert_hint_text(), style="Hint.TLabel")
        self.convert_hint.grid(row=0, column=2, sticky="w")
        # Качество теперь задаётся общими списками ниже (см. _add_quality_controls).
        self._add_quality_controls(frame, "Конвертация", row=1, column=0,
                                   video_default=eng.DEFAULT_VIDEO_QUALITY)
        self.fmt.trace_add("write", lambda *_: self._format_changed())
        self.frames["Конвертация"] = frame

    @staticmethod
    def _convert_hint_text() -> str:
        return (f"«{AUDIO_ONLY_CHOICE}» — вырезать картинку, оставив звук "
                "в текущем контейнере")

    def _is_audio_only_convert(self) -> bool:
        """Выбран ли режим «только звук» (явный вариант или пустой формат)."""
        return self.fmt.get().strip() in (AUDIO_ONLY_CHOICE,
                                          eng.AUDIO_ONLY_FORMAT)

    def _format_changed(self) -> None:
        self._sync_quality_state("Конвертация")
        if callable(self._on_change):
            self._on_change()

    def _build_concat(self) -> None:
        frame = ttk.Frame(self)
        ttk.Label(frame, text="Дополнительные файлы:").grid(
            row=0, column=0, sticky="nw")
        self.extra_files: list[str] = []
        # У каждого файла своя кнопка выбора; «+» добавляет следующую кнопку.
        self.picker_frame = ttk.Frame(frame)
        self.picker_frame.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.file_label = ttk.Label(frame, text="", style="Hint.TLabel")
        self.file_label.grid(row=1, column=1, sticky="w", pady=(3, 0))
        self._add_quality_controls(frame, "Склеивание", row=2, column=0,
                                   video_default=eng.DEFAULT_VIDEO_QUALITY)
        self.frames["Склеивание"] = frame
        self._refresh_files()

    def _refresh_files(self) -> None:
        """Перерисовывает кнопки: по одной на каждый файл и одну «+» в конце."""
        for child in self.picker_frame.winfo_children():
            child.destroy()
        for index, path in enumerate(self.extra_files):
            slot = ttk.Frame(self.picker_frame)
            slot.grid(row=index, column=0, columnspan=2, pady=1, sticky="w")
            ttk.Button(slot, text=os.path.basename(path) or "— не выбран —",
                       width=22,
                       command=lambda i=index: self._pick(i)).grid(row=0, column=0)
            ttk.Button(slot, text="×", width=3,
                       command=lambda i=index: self._remove(i)).grid(
                row=0, column=1, padx=(3, 0))
        ttk.Button(self.picker_frame, text="+", width=3,
                   command=self._add).grid(row=len(self.extra_files), column=0,
                                           pady=1, sticky="w")
        chosen = [p for p in self.extra_files if p]
        self.file_label.config(
            text="порядок: исходный файл, затем "
                 + " → ".join(os.path.basename(p) for p in chosen)
            if chosen else "склейка идёт после выбранного исходного файла")
        if callable(self._on_change):
            self._on_change()

    def _pick(self, index: int) -> None:
        path = self._on_pick_file()
        if not path:
            return
        if index < len(self.extra_files):
            self.extra_files[index] = path
        else:
            self.extra_files.append(path)
        self._refresh_files()

    def _add(self) -> None:
        """Кнопка «+» добавляет следующую кнопку выбора файла."""
        self.extra_files.append("")
        self._refresh_files()

    def _remove(self, index: int) -> None:
        if 0 <= index < len(self.extra_files):
            del self.extra_files[index]
        self._refresh_files()

    def _build_volume(self) -> None:
        frame = ttk.Frame(self)
        ttk.Label(frame, text="Громкость:").grid(row=0, column=0, sticky="w")
        self.volume = tk.StringVar(value="1.5")
        ttk.Entry(frame, textvariable=self.volume, width=10).grid(
            row=0, column=1, padx=6)
        for value in ("0.5", "1.0", "1.5", "2.0"):
            ttk.Button(frame, text=value, width=5,
                       command=lambda v=value: self.volume.set(v)).grid(
                row=0, column=2, padx=2)
        ttk.Label(frame, text="(1.0 = без изменений; можно %: 150%)",
                  style="Hint.TLabel").grid(row=0, column=3, padx=(8, 0))
        # У громкости картинку можно не трогать — добавляем вариант «copy».
        self._add_quality_controls(
            frame, "Громкость", row=1, column=0,
            video_default=eng.COPY_QUALITY,
            video_values=[eng.COPY_QUALITY, *eng.VIDEO_QUALITIES])
        self.frames["Громкость"] = frame

    def _build_speed(self) -> None:
        frame = ttk.Frame(self)
        ttk.Label(frame, text="Скорость:").grid(row=0, column=0, sticky="w")
        self.speed = tk.StringVar(value="1.25")
        ttk.Entry(frame, textvariable=self.speed, width=10).grid(
            row=0, column=1, padx=6)
        for value in ("0.5", "1.0", "1.25", "1.5", "2.0"):
            ttk.Button(frame, text=value, width=5,
                       command=lambda v=value: self.speed.set(v)).grid(
                row=0, column=2, padx=2)
        ttk.Label(frame, text="(множитель: 2.0 = вдвое быстрее)",
                  style="Hint.TLabel").grid(row=0, column=3, padx=(8, 0))
        self._add_quality_controls(frame, "Скорость", row=1, column=0,
                                   video_default=eng.DEFAULT_VIDEO_QUALITY)
        self.frames["Скорость"] = frame

    # ---------------------------------------------------------------- шаги
    def build_step(self) -> object:
        tool = self.var.get()
        if tool == "Обрезка":
            return eng.TrimStep(
                start=eng.parse_time(self.trim_start.get()),
                end=eng.parse_time(self.trim_end.get()),
                video_quality=self.video_quality["Обрезка"].get(),
                audio_quality=self.audio_quality["Обрезка"].get(),
            )
        if tool == "Конвертация":
            # «— только звук —» превращается в пустой формат: движок вырежет
            # видеодорожку, а контейнер возьмёт из расширения результата.
            fmt = (eng.AUDIO_ONLY_FORMAT
                   if self._is_audio_only_convert()
                   else self.fmt.get().strip().lstrip("."))
            return eng.ConvertStep(
                fmt=fmt,
                video_quality=self.video_quality["Конвертация"].get(),
                audio_quality=self.audio_quality["Конвертация"].get(),
            )
        if tool == "Склеивание":
            return eng.ConcatStep(
                files=[p for p in self.extra_files if p],
                video_quality=self.video_quality["Склеивание"].get(),
                audio_quality=self.audio_quality["Склеивание"].get(),
            )
        if tool == "Громкость":
            return eng.VolumeStep(
                value=eng.normalize_volume(self.volume.get()),
                video_quality=self.video_quality["Громкость"].get(),
                audio_quality=self.audio_quality["Громкость"].get(),
            )
        if tool == "Скорость":
            return eng.SpeedStep(
                factor=eng.parse_factor(self.speed.get()),
                video_quality=self.video_quality["Скорость"].get(),
                audio_quality=self.audio_quality["Скорость"].get(),
            )
        return None


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master = master
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.source_info: eng.Streams | None = None
        # Имя результата подставлено автоматически и ещё не правилось вручную.
        self._target_auto = True
        # Флаг на время программной установки пути, чтобы не спутать её с правкой.
        self._updating_target = False

        self._setup_style()
        self._build_ui()
        self.rows[0].var.set("Обрезка")
        self.rows[0]._changed()
        self._alive = True
        self._inspect_job: str | None = None
        # Путь можно и вставить вручную — тогда проверяем файл с задержкой.
        self.source_var.trace_add("write", lambda *_: self._on_source_change())
        self.target_var.trace_add("write", lambda *_: self._on_target_change())
        self._after_id = self.after(150, self._drain_log)

    def _on_source_change(self) -> None:
        self._update_run_state()
        if self._inspect_job is not None:
            try:
                self.after_cancel(self._inspect_job)
            except Exception:  # noqa: BLE001 - окно могло закрыться
                pass
        self._inspect_job = self.after(500, self._inspect_source_async)

    def _inspect_source_async(self) -> None:
        self._inspect_job = None
        path = self.source_var.get().strip()
        if path and os.path.isfile(path):
            self._inspect_source(path, silent=True)
        elif path:
            self.info_var.set("файл не найден")

    def shutdown(self) -> None:
        """Останавливает цикл журнала перед уничтожением окна."""
        self._alive = False
        self.cancel_event.set()
        for job in (self._after_id, self._inspect_job):
            if job is None:
                continue
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001 - окно уже могло быть разрушено
                pass

    # ------------------------------------------------------------------- UI
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Sans", 12, "bold"))
        style.configure("Hint.TLabel", foreground="#6a737d")
        style.configure("Row.TFrame", padding=(0, 4))
        style.configure("RowLabel.TLabel", width=14)
        style.configure("Run.TButton", font=("Sans", 10, "bold"))
        style.configure("TLabelframe", padding=8)

    def _build_ui(self) -> None:
        ttk.Label(self, text="FFmpeg: пакетное преобразование медиа",
                  style="Title.TLabel").grid(row=0, column=0, sticky="w")

        # --- загрузка по ссылке (предварительный шаг к обработке)
        download_box = ttk.Labelframe(
            self, text="Скачать по ссылке (YouTube, VK Video и др.)")
        download_box.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        download_box.columnconfigure(1, weight=1)
        self.download_url_var = tk.StringVar()
        ttk.Label(download_box, text="Ссылка:").grid(
            row=0, column=0, sticky="w")
        ttk.Entry(download_box, textvariable=self.download_url_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 6), pady=(0, 4))
        ttk.Button(download_box, text="Вставить", width=9,
                   command=self._paste_download_url).grid(
            row=1, column=2, padx=(0, 6), pady=(0, 4))
        self.formats_btn = ttk.Button(download_box, text="Показать варианты",
                                      width=15, command=self.load_formats,
                                      state="disabled")
        self.formats_btn.grid(row=1, column=3, pady=(0, 4))

        # Варианты берутся с сайта: скачиваем ровно то, что там есть, и пока
        # этого хватает — ничего не перекодируем. Формат и качество звука тоже
        # выбираются на сайте, отдельной настройки не нужно — при желании звук
        # приводится к нужному шагом «Конвертация» уже после скачивания.
        ttk.Label(download_box, text="Вариант:").grid(
            row=2, column=0, sticky="w")
        self.download_variant_var = tk.StringVar(value=eng.DOWNLOAD_BEST)
        self.download_variant_combo = ttk.Combobox(
            download_box, textvariable=self.download_variant_var, width=44,
            state="readonly", values=[eng.DOWNLOAD_BEST])
        self.download_variant_combo.grid(row=2, column=1, columnspan=3,
                                         sticky="ew", pady=(0, 4))

        self.download_target_var = tk.StringVar()
        ttk.Label(download_box, text="Сохранить как:").grid(
            row=3, column=0, sticky="w")
        ttk.Entry(download_box, textvariable=self.download_target_var).grid(
            row=4, column=1, sticky="ew", padx=(0, 6))
        ttk.Button(download_box, text="Файл…", width=9,
                   command=self._pick_download_target).grid(
            row=4, column=2, padx=(0, 6))
        self.download_btn = ttk.Button(download_box, text="Скачать", width=10,
                                       command=self.start_download,
                                       state="disabled")
        self.download_btn.grid(row=4, column=3, sticky="e")
        self.download_hint_var = tk.StringVar(value=self._download_hint_text())
        ttk.Label(download_box, textvariable=self.download_hint_var,
                  style="Hint.TLabel").grid(row=5, column=0, columnspan=4,
                                            sticky="w", pady=(4, 0))

        # Реакция на смену варианта: она определяет, просить ли у сайта
        # склейку или remux.
        self.download_variant_var.trace_add(
            "write", lambda *_: self._update_download_state())
        self.download_url_var.trace_add(
            "write", lambda *_: self._update_download_state())
        # Доступные варианты и выбранная строка → параметры скачивания.
        self._download_options: list[eng.FormatOption] = []

        # --- входной файл
        input_box = ttk.Labelframe(self, text="Исходный файл")
        input_box.grid(row=2, column=0, sticky="ew", pady=(8, 4))
        input_box.columnconfigure(0, weight=1)
        self.source_var = tk.StringVar()
        ttk.Entry(input_box, textvariable=self.source_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(input_box, text="Поиск…", width=10,
                   command=self.pick_source).grid(row=0, column=1)
        self.info_var = tk.StringVar(value="файл не выбран")
        ttk.Label(input_box, textvariable=self.info_var,
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2,
                                            sticky="w", pady=(4, 0))

        self._formats_loading = False

        # --- инструменты
        self._tools_box = ttk.Labelframe(
            self, text="Инструменты (шаги выполняются сверху вниз)")
        self._tools_box.grid(row=3, column=0, sticky="ew", pady=4)
        self._tools_box.columnconfigure(0, weight=1)
        self.rows: list[RowPanel] = []
        self._add_row_btn = ttk.Button(
            self._tools_box, text="+", width=3, command=self._add_row)
        self._add_row()

        # --- выход
        output_box = ttk.Labelframe(self, text="Результат")
        output_box.grid(row=4, column=0, sticky="ew", pady=4)
        output_box.columnconfigure(0, weight=1)
        self.target_var = tk.StringVar()
        ttk.Entry(output_box, textvariable=self.target_var).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(output_box, text="Сохранить как…", width=14,
                   command=self.pick_target).grid(row=0, column=1)

        # --- кнопки
        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, sticky="ew", pady=(4, 6))
        self.run_btn = ttk.Button(buttons, text="Преобразовать",
                                  style="Run.TButton", command=self.start)
        self.run_btn.grid(row=0, column=0, padx=(0, 6))
        self.cancel_btn = ttk.Button(buttons, text="Отмена", width=10,
                                     command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        ttk.Button(buttons, text="Открыть папку", width=14,
                   command=self.reveal_target).grid(row=0, column=2, padx=6)
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=180)
        self.progress.grid(row=0, column=3, sticky="ew")
        buttons.columnconfigure(3, weight=1)

        # --- лог
        log_box = ttk.Labelframe(self, text="Журнал")
        log_box.grid(row=6, column=0, sticky="nsew", pady=(4, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=12, wrap="word",
                                background="#1e1f22", foreground="#d7dae0",
                                insertbackground="#d7dae0", relief="flat")
        scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scroll.set, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(6, weight=1)
        self._update_run_state()
        self._update_download_state()

    # ------------------------------------------------- строки инструментов
    def _add_row(self) -> None:
        """Кнопка «+»: создаёт ещё одну строку инструмента."""
        if len(self.rows) >= MAX_ROWS:
            return
        index = len(self.rows)
        # Базовая строка остаётся всегда: без неё в окне не от чего оттолкнуться,
        # а «−» на последней строке позволил бы оставить цепочку пустой.
        removable = bool(self.rows)
        row = RowPanel(
            self._tools_box, index, self._update_run_state,
            self.pick_extra_file,
            on_remove=(lambda: self._remove_row(row)) if removable else None)
        self.rows.append(row)
        # Строка могла появиться уже после выбора исходника — передаём ей
        # известные дорожки, иначе списки качества будут неактуальны.
        row.set_streams(self.source_info)
        self._relayout_rows()
        self._update_run_state()

    def _remove_row(self, row: RowPanel) -> None:
        """Кнопка «−»: убирает строку и перенумеровывает оставшиеся."""
        if row not in self.rows or len(self.rows) <= 1:
            return
        self.rows.remove(row)
        row.destroy()
        self._relayout_rows()
        self._update_run_state()

    def _relayout_rows(self) -> None:
        """Заново раскладывает строки и кнопку «+», пересчитывая нумерацию."""
        for index, row in enumerate(self.rows):
            row.set_index(index)
            row.grid(row=index, column=0, sticky="ew", pady=2)
        self._add_row_btn.grid(row=len(self.rows), column=0, sticky="w",
                               pady=(2, 0))
        self._add_row_btn.config(state="normal"
                                 if len(self.rows) < MAX_ROWS else "disabled")

    # ------------------------------------------------------- загрузка по URL
    @staticmethod
    def _download_hint_text() -> str:
        return ("для VK/YouTube нужен yt-dlp; скачанный файл подставится "
                "как исходный")

    def _update_download_state(self) -> None:
        if not hasattr(self, "download_btn"):
            return
        ready = bool(self.download_url_var.get().strip())
        state = "normal" if ready else "disabled"
        if str(self.download_btn["state"]) != state:
            self.download_btn.config(state=state)
        # Пока список вариантов загружается, кнопку не возвращаем: правка
        # ссылки в этот момент запустила бы второй запрос к сайту параллельно.
        if hasattr(self, "formats_btn") and not self._formats_loading:
            self.formats_btn.config(state="normal" if ready else "disabled")

    def _paste_download_url(self) -> None:
        """Берёт ссылку из буфера обмена (если там текст)."""
        try:
            text = self.master.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if text:
            self.download_url_var.set(text.splitlines()[0])

    def load_formats(self) -> None:
        """Спрашиваем сайт, какие варианты доступны, и заполняем список."""
        url = self.download_url_var.get().strip()
        if not url:
            return
        self.formats_btn.config(state="disabled", text="Загрузка…")
        self._formats_loading = True
        self._download_options = []
        self.download_variant_var.set(eng.DOWNLOAD_BEST)
        self.download_variant_combo.config(values=[eng.DOWNLOAD_BEST])
        self.cancel_event.clear()

        def _load() -> None:
            try:
                options = eng.list_download_formats(
                    url, self.log, self.cancel_event.is_set)
                self.log_queue.put(("formats", options))
            except eng.EngineError as exc:
                self.log_queue.put(("formats_failed", str(exc)))
            except Exception as exc:  # noqa: BLE001 - показываем всё в лог
                self.log_queue.put(("formats_failed",
                                    f"Непредвиденная ошибка: {exc}"))

        threading.Thread(target=_load, daemon=True).start()

    def _apply_download_formats(self, options: list) -> None:
        """Заполняет список вариантов; вызывается только из потока Tkinter."""
        self._formats_loading = False
        self._download_options = options
        labels = eng.format_choices(options)
        self.download_variant_combo.config(values=labels)
        self.download_variant_var.set(labels[0] if labels else eng.DOWNLOAD_BEST)
        self.formats_btn.config(state="normal", text="Показать варианты")
        if not options:
            self._append_log("Сайт не вернул список вариантов — "
                             "будет скачано лучшее доступное")

    def _download_extension(self) -> str:
        """Расширение файла по умолчанию: берём из варианта сайта."""
        option = eng.find_format(self._download_options,
                                 self.download_variant_var.get().strip())
        if option is not None and option.ext:
            return option.ext
        return "mp4"

    def _download_default_name(self) -> str:
        return os.path.join(self._home_dir(),
                            "download." + self._download_extension())

    def _pick_download_target(self) -> None:
        fmt = self._download_extension()
        initial = self.download_target_var.get() or self._download_default_name()
        path = filedialog.asksaveasfilename(
            title="Куда сохранить загруженный файл",
            initialdir=os.path.dirname(initial) or self._home_dir(),
            initialfile=os.path.basename(initial),
            defaultextension="." + fmt,
            filetypes=FILE_TYPES)
        if path:
            self.download_target_var.set(path)

    def start_download(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        url = self.download_url_var.get().strip()
        if not url:
            messagebox.showerror("Ошибка", "Укажите ссылку для загрузки")
            return
        variant = self.download_variant_var.get().strip()
        target = self.download_target_var.get().strip() or self._download_default_name()
        # Параметры скачивания считаем здесь, в потоке Tkinter: они зависят от
        # выбранного варианта, а список вариантов в рабочем потоке менять нельзя.
        option = eng.find_format(self._download_options, variant)
        format_spec, merge_ext, remux_ext = eng.plan_download(option, target)

        self._set_busy(True)
        self.download_btn.config(state="disabled")
        self.cancel_event.clear()
        self.log("=" * 68)
        self.log(f"Загрузка: {url}")
        self.worker = threading.Thread(
            target=self._download_worker,
            args=(url, format_spec, merge_ext, remux_ext, target),
            daemon=True)
        self.worker.start()

    def _download_worker(self, url: str, format_spec: str, merge_ext: str,
                         remux_ext: str, target: str) -> None:
        try:
            path = eng.download_media(
                url, target, format_spec, merge_ext, remux_ext, self.log,
                self.cancel_event.is_set)
            self.log_queue.put(("downloaded", path))
        except eng.EngineError as exc:
            self.log_queue.put(("cancel" if isinstance(exc, eng.CancelledError)
                                else "error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - показываем всё в лог
            self.log_queue.put(("error", f"Непредвиденная ошибка: {exc}"))

    # ----------------------------------------------------------- выбор файлов
    @staticmethod
    def _home_dir() -> str:
        """Каталог по умолчанию для диалогов выбора файлов.

        APP_DIR для deb-установки — это /usr/lib/ffprog: системный каталог,
        куда пользователь не пойдёт и где часто нет прав на запись.
        """
        return os.path.expanduser("~")

    def pick_source(self) -> None:
        path = filedialog.askopenfilename(title="Выберите файл для преобразования",
                                          initialdir=self._home_dir(),
                                          filetypes=FILE_TYPES)
        if not path:
            return
        # Проверка файла запустится по изменению значения (см. _on_source_change).
        self.source_var.set(path)

    def _inspect_source(self, path: str, silent: bool = False) -> None:
        try:
            self.source_info = eng.probe_streams(path)
        except eng.EngineError as exc:
            self.source_info = None
            self.info_var.set("не удалось прочитать файл")
            if not silent:
                messagebox.showerror("Ошибка", str(exc))
            return
        duration = self._probe_duration(path)
        self.info_var.set(
            f"{self.source_info.describe()}, длительность {duration}")
        # Сообщаем строкам о наличии дорожек — гасим ненужные списки качества.
        for row in self.rows:
            row.set_streams(self.source_info)
        if self._target_auto or not self.target_var.get():
            # Новый исходник — заново подставляем имя на основе его названия.
            self._suggest_target(path)
        self._update_run_state()

    def _probe_duration(self, path: str) -> str:
        cmd = [eng.FFPROBE, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return eng.format_time(float(result.stdout.strip()))
        except Exception:  # noqa: BLE001 - длительность только для справки
            return "неизвестна"

    def _suggest_target(self, source: str) -> None:
        steps = self._collect_steps(strict=False)
        ext = eng.output_extension([s for s in steps if s], source)
        base = os.path.splitext(os.path.basename(source))[0]
        self._set_target(os.path.join(os.path.dirname(source),
                                      f"{base}_out{ext}"), auto=True)

    def _set_target(self, path: str, auto: bool) -> None:
        """Ставит путь результата, не принимая его за ручную правку."""
        self._updating_target = True
        try:
            self.target_var.set(path)
        finally:
            self._updating_target = False
        self._target_auto = auto

    def _on_target_change(self) -> None:
        if self._updating_target:
            return
        # Пользователь вписал свой путь — больше не переписываем его.
        self._target_auto = False

    def _sync_target_extension(self) -> None:
        """Держит расширение авто-имени в согласии с выбранными инструментами."""
        if not self._target_auto:
            return
        source = self.source_var.get().strip()
        if not source:
            return
        ext = eng.output_extension(self._collect_steps(strict=False), source)
        current = self.target_var.get().strip()
        if not current:
            self._suggest_target(source)
            return
        if os.path.splitext(current)[1].lower() != ext.lower():
            self._set_target(os.path.splitext(current)[0] + ext, auto=True)

    def pick_extra_file(self) -> str | None:
        return filedialog.askopenfilename(title="Добавьте файл для склейки",
                                          initialdir=self._home_dir(),
                                          filetypes=FILE_TYPES)

    def pick_target(self) -> None:
        steps = self._collect_steps(strict=False)
        ext = eng.output_extension([s for s in steps if s],
                                   self.source_var.get() or "x.mp4")
        initial = self.target_var.get() or os.path.join(
            os.path.dirname(self.source_var.get()) or self._home_dir(),
            f"result{ext}")
        path = filedialog.asksaveasfilename(title="Куда сохранить результат",
                                            initialfile=os.path.basename(initial),
                                            defaultextension=ext,
                                            filetypes=FILE_TYPES)
        if path:
            self.target_var.set(path)

    def reveal_target(self) -> None:
        folder = os.path.dirname(self.target_var.get()) or self._home_dir()
        try:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку: {exc}")

    # --------------------------------------------------------------- запуск
    def _collect_steps(self, strict: bool = True) -> list:
        steps = []
        for row in self.rows:
            tool = row.var.get()
            if tool == EMPTY_TOOL:
                continue
            try:
                step = row.build_step()
            except eng.EngineError:
                if strict:
                    raise
                continue
            if step is None:
                continue
            if strict and isinstance(step, eng.ConcatStep) and not step.files:
                raise eng.EngineError(
                    f"В строке «Склеивание №{row.index + 1}» не добавлены файлы")
            steps.append(step)
        if strict and not steps:
            raise eng.EngineError("Выберите хотя бы один инструмент")
        return steps

    def _update_run_state(self) -> None:
        # Метод вызывается и на этапе сборки интерфейса, когда кнопок ещё нет.
        if not hasattr(self, "run_btn"):
            return
        if hasattr(self, "target_var"):
            self._sync_target_extension()
        ready = bool(self.source_var.get()) and any(
            row.var.get() != EMPTY_TOOL for row in self.rows)
        state = "normal" if ready else "disabled"
        if str(self.run_btn["state"]) != state:
            self.run_btn.config(state=state)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            source = self.source_var.get().strip()
            if not source:
                raise eng.EngineError("Выберите файл для преобразования")
            steps = self._collect_steps(strict=True)
            target = self.target_var.get().strip()
            if not target:
                self._suggest_target(source)
                target = self.target_var.get().strip()
            if os.path.abspath(target) == os.path.abspath(source):
                raise eng.EngineError("Файл результата совпадает с исходным")
        except eng.EngineError as exc:
            messagebox.showerror("Ошибка", str(exc))
            return

        self._set_busy(True)
        self.cancel_event.clear()
        self.log("=" * 68)
        self.log(f"Старт: {os.path.basename(source)} → {os.path.basename(target)}")
        self.worker = threading.Thread(target=self._worker,
                                       args=(source, steps, target),
                                       daemon=True)
        self.worker.start()

    def _worker(self, source: str, steps: list, target: str) -> None:
        try:
            eng.Pipeline(self.log, self.cancel_event.is_set).run(
                source, steps, target)
            self.log_queue.put(("done", None))
        except eng.EngineError as exc:
            self.log_queue.put(("cancel" if isinstance(exc, eng.CancelledError)
                                else "error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - показываем всё в лог
            self.log_queue.put(("error", f"Непредвиденная ошибка: {exc}"))

    def cancel(self) -> None:
        self.cancel_event.set()
        self.log("Отмена… дожидаюсь завершения ffmpeg")

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.progress.start(12)
            self.run_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
        else:
            self.progress.stop()
            self.cancel_btn.config(state="disabled")
            self._update_run_state()

    # ------------------------------------------------------------------ лог
    def log(self, message: str) -> None:
        self.log_queue.put(("line", message))

    def _drain_log(self) -> None:
        try:
            for _ in range(200):
                kind, payload = self.log_queue.get_nowait()
                if kind == "line":
                    self._append_log(payload)
                elif kind == "done":
                    self._set_busy(False)
                    self._append_log("✓ Преобразование завершено")
                elif kind == "cancel":
                    self._set_busy(False)
                    self._append_log("✗ Отменено")
                elif kind == "error":
                    self._set_busy(False)
                    self._update_download_state()
                    self._append_log("✗ " + (payload or ""))
                    messagebox.showerror("Ошибка", payload or "Неизвестная ошибка")
                elif kind == "formats":
                    self._apply_download_formats(payload)
                elif kind == "formats_failed":
                    self.formats_btn.config(state="normal",
                                            text="Показать варианты")
                    self._formats_loading = False
                    self._append_log("✗ " + (payload or ""))
                    messagebox.showerror("Ошибка", payload or "Неизвестная ошибка")
                elif kind == "downloaded":
                    self._set_busy(False)
                    self.download_btn.config(state="normal")
                    self._append_log("✓ Загрузка завершена")
                    # Автоподстановка скачанного файла как исходника.
                    self.source_var.set(payload)
                    self.info_var.set("загружен и подставлен как исходный файл")
                    self._update_run_state()
        except queue.Empty:
            pass
        finally:
            self._reschedule_log_drain()

    def _reschedule_log_drain(self) -> None:
        """Планирует следующий проход цикла журнала, отменяя предыдущий.

        Без отмены прямой вызов _drain_log (например, из тестов) оставил бы
        в очереди Tcl «висячий» after-скрипт, который падает после destroy().
        """
        if not self._alive:
            return
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001 - ID уже мог отработать
                pass
            self._after_id = None
        try:
            self._after_id = self.after(150, self._drain_log)
        except tk.TclError:  # окно разрушено
            self._alive = False

    def _append_log(self, text: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")


def main() -> None:
    root = tk.Tk()
    root.title("Программа преобразования FFmpeg")
    root.geometry("980x760")
    root.minsize(860, 620)
    try:
        eng.check_binaries()
    except eng.EngineError as exc:
        messagebox.showerror("FFmpeg не найден", str(exc))
        root.destroy()
        return
    ui = App(root)
    ui.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", lambda: (ui.shutdown(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
