"""Смоук-тест графического интерфейса.

Требует доступного X-дисплея; запускает реальное окно, прогоняет цепочку
«Обрезка → Громкость → Скорость → Конвертация» и проверяет результат.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

if HAS_DISPLAY:
    try:
        import tkinter as tk  # noqa: F401
    except ImportError:
        HAS_DISPLAY = False

if HAS_DISPLAY:
    import app as app_mod
    import ffmpeg_engine as eng


@unittest.skipUnless(HAS_DISPLAY, "нет графического дисплея")
class TestGui(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffprog_gui_")
        self.source = os.path.join(self.tmp, "clip.mp4")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-shortest", self.source], check=True, capture_output=True)
        self.root = tk.Tk()  # type: ignore[name-defined]
        self.ui = app_mod.App(self.root)
        self.ui.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self):
        if self.ui.worker and self.ui.worker.is_alive():
            self.ui.cancel()
            self.ui.worker.join(timeout=10)
        self.ui.shutdown()
        self.root.destroy()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, timeout: float = 90.0) -> None:
        """Крутится в event loop, пока фоновая обработка не завершится."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if not (self.ui.worker and self.ui.worker.is_alive()):
                time.sleep(0.2)
                self.root.update()
                return
            time.sleep(0.1)
        self.fail("обработка не завершилась за отведённое время")

    def tools(self):
        return [row.var.get() for row in self.ui.rows]

    def test_starts_with_single_row(self):
        """По умолчанию видна одна строка — окно не разъезжается в высоту."""
        self.assertEqual(len(self.ui.rows), 1)
        self.assertEqual(self.tools()[0], "Обрезка")

    def test_plus_button_adds_row(self):
        self.ui._add_row()
        self.assertEqual(len(self.ui.rows), 2)
        self.assertEqual(self.ui.rows[1].var.get(), app_mod.EMPTY_TOOL)
        self.assertEqual(self.ui.rows[1].title.cget("text"), "Инструмент 2:")

    def test_minus_button_removes_own_row(self):
        self.ui._add_row()
        second = self.ui.rows[1]
        second.var.set("Громкость")
        self.assertIsNotNone(second.remove_btn, "у добавленной строки нет «−»")
        self.ui._remove_row(second)
        self.assertEqual(len(self.ui.rows), 1)
        # Базовая строка не пострадала.
        self.assertEqual(self.tools(), ["Обрезка"])

    def test_first_row_has_no_minus_button(self):
        self.assertIsNone(self.ui.rows[0].remove_btn)

    def test_cannot_remove_last_row(self):
        self.ui._remove_row(self.ui.rows[0])
        self.assertEqual(len(self.ui.rows), 1)

    def test_row_numbers_renumbered_after_removal(self):
        for _ in range(3):
            self.ui._add_row()
        self.assertEqual(len(self.ui.rows), 4)
        self.ui._remove_row(self.ui.rows[1])
        self.assertEqual([r.title.cget("text") for r in self.ui.rows],
                         ["Инструмент 1:", "Инструмент 2:", "Инструмент 3:"])
        self.assertEqual([r.index for r in self.ui.rows], [0, 1, 2])

    def test_plus_disabled_at_limit(self):
        while len(self.ui.rows) < app_mod.MAX_ROWS:
            self.ui._add_row()
        self.ui._add_row()
        self.assertEqual(len(self.ui.rows), app_mod.MAX_ROWS)
        self.assertEqual(str(self.ui._add_row_btn["state"]), "disabled")

    def test_new_row_knows_source_streams(self):
        """Строка, добавленная после выбора файла, получает его дорожки."""
        self.ui.source_var.set(self.source)
        self.ui._inspect_source(self.source)
        audio_only = os.path.join(self.tmp, "only.mp3")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        audio_only], check=True, capture_output=True)
        self.ui._inspect_source(audio_only)
        self.ui._add_row()
        combo = self.ui.rows[1].combo_video_quality["Обрезка"]
        self.assertEqual(str(combo["state"]), "disabled")

    def test_switching_tool_shows_matching_frame(self):
        row = self.ui.rows[0]
        self.assertTrue(row.frames["Обрезка"].winfo_ismapped())
        row.var.set("Скорость")
        self.root.update()
        self.assertFalse(row.frames["Обрезка"].winfo_ismapped())
        self.assertTrue(row.frames["Скорость"].winfo_ismapped())

    def test_convert_frame_lists_video_and_audio_formats(self):
        values = self.ui.rows[0].frames["Конвертация"]
        combo = values.winfo_children()[1]
        self.assertIn("mp4", combo["values"])
        self.assertIn("mp3", combo["values"])
        self.assertIn(app_mod.AUDIO_ONLY_CHOICE, combo["values"])

    def test_audio_only_choice_disables_video_quality(self):
        row = self.ui.rows[0]
        row.var.set("Конвертация")
        row.fmt.set(app_mod.AUDIO_ONLY_CHOICE)
        self.root.update()
        self.assertEqual(str(row.combo_video_quality["Конвертация"]["state"]),
                         "disabled")
        self.assertEqual(str(row.combo_audio_quality["Конвертация"]["state"]),
                         "readonly")

    def test_audio_only_choice_builds_empty_format(self):
        row = self.ui.rows[0]
        row.var.set("Конвертация")
        row.fmt.set(app_mod.AUDIO_ONLY_CHOICE)
        step = row.build_step()
        self.assertEqual(step.fmt, eng.AUDIO_ONLY_FORMAT)

    def test_audio_only_choice_keeps_target_extension(self):
        """Режим «только звук» контейнер не меняет: .mp4 остаётся .mp4."""
        self.ui.source_var.set(self.source)
        self.ui._inspect_source(self.source)
        self.root.update()
        row = self.ui.rows[0]
        row.var.set("Конвертация")
        row.fmt.set(app_mod.AUDIO_ONLY_CHOICE)
        self.root.update()
        self.assertTrue(self.ui.target_var.get().endswith("_out.mp4"),
                        self.ui.target_var.get())

    def test_audio_only_choice_strips_video_from_file(self):
        """Реальный прогон: на выходе не должно быть видеопотока."""
        self.ui.source_var.set(self.source)
        self.ui._inspect_source(self.source)
        row = self.ui.rows[0]
        row.var.set("Конвертация")
        row.fmt.set(app_mod.AUDIO_ONLY_CHOICE)
        self.root.update()
        target = os.path.join(self.tmp, "sound.mp4")
        self.ui.target_var.set(target)
        self.ui.start()
        self.wait()
        self.assertTrue(os.path.isfile(target),
                        msg="\n".join(self.ui.log_text.get("1.0", "end").splitlines()[-6:]))
        streams = eng.probe_streams(target)
        self.assertFalse(streams.has_video)
        self.assertTrue(streams.has_audio)

    def test_run_button_disabled_without_source(self):
        self.assertEqual(str(self.ui.run_btn["state"]), "disabled")
        self.ui.source_var.set(self.source)
        self.ui._update_run_state()
        self.assertEqual(str(self.ui.run_btn["state"]), "normal")

    def test_target_extension_follows_conversion(self):
        self.ui.source_var.set(self.source)
        self.ui._inspect_source(self.source)
        self.root.update()
        self.assertTrue(self.ui.target_var.get().endswith(".mp4"),
                        self.ui.target_var.get())
        row = self.ui.rows[0]
        row.var.set("Конвертация")
        row.fmt.set("mp3")
        self.root.update()
        self.assertTrue(self.ui.target_var.get().endswith("_out.mp3"),
                        self.ui.target_var.get())
        # ручная правка пути больше не перезаписывается
        self.ui.target_var.set(os.path.join(self.tmp, "my.mp4"))
        self.root.update()
        row.fmt.set("mkv")
        self.root.update()
        self.assertEqual(self.ui.target_var.get(), os.path.join(self.tmp, "my.mp4"))

    def test_full_chain(self):
        self.ui.source_var.set(self.source)
        self.ui._inspect_source(self.source)
        self.assertIn("видео + аудио", self.ui.info_var.get())

        rows = self.ui.rows
        for _ in range(3):
            self.ui._add_row()
        rows[0].var.set("Обрезка")
        rows[0].trim_start.delete(0, "end")
        rows[0].trim_start.insert(0, "0:00:01")
        rows[0].trim_end.delete(0, "end")
        rows[0].trim_end.insert(0, "0:00:03")
        rows[1].var.set("Громкость")
        rows[1].volume.set("200%")
        rows[2].var.set("Скорость")
        rows[2].speed.set("2")
        rows[3].var.set("Конвертация")
        rows[3].fmt.set("mkv")
        rows[3].video_quality["Конвертация"].set("Среднее")
        rows[3].audio_quality["Конвертация"].set("Низкое")
        self.root.update()

        steps = self.ui._collect_steps()
        self.assertEqual([type(s).__name__ for s in steps],
                         ["TrimStep", "VolumeStep", "SpeedStep", "ConvertStep"])

        target = os.path.join(self.tmp, "result.mkv")
        self.ui.target_var.set(target)
        self.ui.start()
        self.wait()
        self.assertTrue(os.path.isfile(target),
                        msg="\n".join(self.ui.log_text.get("1.0", "end").splitlines()[-6:]))
        size = os.path.getsize(target)
        self.assertGreater(size, 1000)

    def test_invalid_input_shows_error_not_crash(self):
        self.ui.source_var.set(self.source)
        row = self.ui.rows[0]
        row.var.set("Обрезка")
        row.trim_start.delete(0, "end")
        row.trim_start.insert(0, "5")
        row.trim_end.delete(0, "end")
        row.trim_end.insert(0, "1")
        self.ui.target_var.set(os.path.join(self.tmp, "bad.mkv"))

        shown = []
        original = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        try:
            self.ui.start()
        finally:
            app_mod.messagebox.showerror = original
        self.assertTrue(shown, "ошибка не показана")
        self.assertIsNone(self.ui.worker)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "bad.mkv")))

    def test_concat_row_tracks_added_files(self):
        self.ui._add_row()
        row = self.ui.rows[1]
        row.var.set("Склеивание")
        self.root.update()
        # сначала видна только кнопка «+»
        self.assertEqual(len(row.picker_frame.winfo_children()), 1)
        row._add()
        self.assertEqual(len(row.picker_frame.winfo_children()), 2)
        row.extra_files[0] = self.source
        row._refresh_files()
        self.root.update()
        self.assertIn("clip.mp4", row.file_label.cget("text"))
        # «+» добавляет ещё одну кнопку выбора
        row._add()
        self.assertEqual(len(row.picker_frame.winfo_children()), 3)
        row._remove(0)
        self.assertEqual(row.extra_files, [""])

    def test_concat_step_skips_empty_slots(self):
        self.ui._add_row()
        row = self.ui.rows[1]
        row.var.set("Склеивание")
        row._add()
        step = row.build_step()
        self.assertEqual(step.files, [])

    # ------------------------------------------------- панель «Скачать по ссылке»
    def test_download_button_disabled_without_url(self):
        self.assertEqual(str(self.ui.download_btn["state"]), "disabled")
        self.ui.download_url_var.set("https://vk.com/video1")
        self.assertEqual(str(self.ui.download_btn["state"]), "normal")

    def test_download_panel_has_no_audio_controls(self):
        """Формат и качество звука задаёт сайт — отдельных полей больше нет."""
        for name in ("download_audio_fmt_var", "download_audio_fmt_combo",
                     "download_audio_quality_var", "download_audio_quality_combo"):
            self.assertFalse(hasattr(self.ui, name), name)

    def test_download_formats_include_video_and_audio(self):
        values = list(self.ui.download_variant_combo["values"])
        self.assertIn(eng.DOWNLOAD_BEST, values)

    def test_download_result_becomes_source_file(self):
        """Скачанный файл должен подставиться в поле «Исходный файл»."""
        downloaded = os.path.join(self.tmp, "from_net.mp4")
        shutil.copyfile(self.source, downloaded)
        self.ui.log_queue.put(("downloaded", downloaded))
        self.ui._drain_log()
        self.assertEqual(self.ui.source_var.get(), downloaded)
        self.assertIn("загружен", self.ui.info_var.get())

    def test_download_worker_reports_error_without_crash(self):
        """Ошибка движка показывается messagebox'ом, а не роняет окно."""
        shown = []
        original = app_mod.messagebox.showerror
        app_mod.messagebox.showerror = lambda *a, **k: shown.append(a)
        original_download = eng.download_media
        eng.download_media = lambda *a, **k: (_ for _ in ()).throw(
            eng.EngineError("yt-dlp не установлен"))
        try:
            self.ui.download_url_var.set("https://vk.com/video1")
            self.ui.download_target_var.set(os.path.join(self.tmp, "x.mp4"))
            self.ui.start_download()
            self.wait(timeout=15)
        finally:
            eng.download_media = original_download
            app_mod.messagebox.showerror = original
        self.assertTrue(shown, "ошибка не показана")
        self.assertIn("yt-dlp", shown[0][1])
        self.assertEqual(str(self.ui.download_btn["state"]), "normal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
