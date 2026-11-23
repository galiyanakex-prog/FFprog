"""Тесты движка FFmpeg: построение команд и реальные прогоны на синтетике.

Запуск:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ffmpeg_engine as eng  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# Загрузка проверяется без сети: вместо настоящего yt-dlp подставляется
# скрипт-заглушка, который на `-J` отдаёт заготовленный JSON со списком
# вариантов, а при скачивании «скачивает» заранее подготовленный файл.
FAKE_YTDLP = "\n".join([
    "#!/usr/bin/env python3",
    "import os, shutil, sys",
    'args = sys.argv[1:]',
    'if "-J" in args:',
    '    sys.stdout.write(os.environ.get("FAKE_JSON", "{}"))',
    "    sys.exit(0)",
    'if os.environ.get("FAKE_ARGS_LOG"):',
    '    open(os.environ["FAKE_ARGS_LOG"], "w", encoding="utf-8").write('
    'chr(10).join(args))',
    'out = args[args.index("-o") + 1]',
    'src = os.environ["FAKE_MEDIA"]',
    'if os.environ.get("FAKE_YTDLP_FAIL"):',
    '    sys.stderr.write("ERROR: unable to extract" + chr(10))',
    "    sys.exit(1)",
    'if os.environ.get("FAKE_YTDLP_RENAME"):',
    '    out = os.path.splitext(out)[0] + ".webm"',
    "shutil.copyfile(src, out)",
    'print("[download] 100% of 2.00KiB in 00:00:01")',
    "",
])

# Ответ сайта: раздельные видео и звук, готовый файл «видео+звук» и два
# аудиопотока. Именно в таком виде yt-dlp отдаёт их через `-J`.
SITE_INFO = {
    "formats": [
        {"format_id": "248", "ext": "webm", "height": 1080, "fps": 30,
         "vcodec": "vp09.00.40.08", "acodec": "none", "tbr": 2000,
         "filesize_approx": 12_000_000},
        {"format_id": "271", "ext": "webm", "height": 1080, "fps": 60,
         "vcodec": "vp09.00.40.08", "acodec": "none", "tbr": 2600},
        {"format_id": "137", "ext": "mp4", "height": 1080,
         "vcodec": "avc1.640028", "acodec": "none", "tbr": 4000},
        {"format_id": "18", "ext": "mp4", "height": 360,
         "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "tbr": 500,
         "format_note": "tiny"},
        {"format_id": "251", "ext": "webm", "vcodec": "none",
         "acodec": "opus", "abr": 160},
        {"format_id": "140", "ext": "m4a", "vcodec": "none",
         "acodec": "mp4a.40.2", "abr": 129},
        # «Поток» без кодеков — файл из него не соберётся.
        {"format_id": "sb0", "ext": "mp4", "vcodec": "none",
         "acodec": "none"},
    ],
}


def write_fake_ytdlp(directory: str) -> str:
    """Создаёт исполняемую заглушку yt-dlp и возвращает путь к ней."""
    path = os.path.join(directory, "yt-dlp")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(FAKE_YTDLP + "\n")
    os.chmod(path, 0o755)
    return path


def make_media(path: str, duration: float = 2.0, video: bool = True,
               audio: bool = True) -> None:
    """Генерирует тестовый файл: цвет + тон."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i",
                f"testsrc=size=160x120:rate=10:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:duration={duration}"]
    cmd += ["-shortest", path]
    subprocess.run(cmd, check=True, capture_output=True)


class TestDownloadArgs(unittest.TestCase):
    """Сборка аргументов yt-dlp — без запуска процесса и без сети."""

    def test_best_available(self):
        args = eng.build_download_args("https://x/y", "/tmp/out.mp4")
        self.assertIn("-f", args)
        self.assertIn("bestvideo+bestaudio/best", args[args.index("-f") + 1])
        self.assertEqual(args[-1], "https://x/y")
        self.assertEqual(args[args.index("-o") + 1], "/tmp/out.mp4")

    def test_video_without_audio_needs_merge(self):
        """Видео-без-звука → spec с «+», merge_ext задаётся из имени файла."""
        args = eng.build_download_args(
            "https://x/y", "/tmp/out.mp4",
            format_spec="137+bestaudio/137", merge_ext="mp4")
        self.assertIn("-f", args)
        self.assertIn("137+bestaudio/137", args[args.index("-f") + 1])
        self.assertIn("--merge-output-format", args)
        self.assertEqual(args[args.index("--merge-output-format") + 1], "mp4")

    def test_audio_format_no_merge(self):
        """Аудиопоток скачивается как есть — merge не нужен."""
        args = eng.build_download_args(
            "https://x/y", "/tmp/song.mp3",
            format_spec="140")
        self.assertIn("-f", args)
        self.assertEqual(args[args.index("-f") + 1], "140")
        self.assertNotIn("--merge-output-format", args)

    def test_empty_url_rejected(self):
        with self.assertRaises(eng.EngineError):
            eng.build_download_args("   ", "/tmp/o.mp4")

    def test_download_step_validation(self):
        with self.assertRaises(eng.EngineError):
            eng.DownloadStep(url="  ")
        step = eng.DownloadStep(url="https://x/y", merge_ext="webm")
        self.assertEqual(step.merge_ext, "webm")
        self.assertEqual(step.label, "Скачать по ссылке")
        # remux_ext тоже валидируется
        with self.assertRaises(eng.EngineError):
            eng.DownloadStep(url="https://x/y", remux_ext="exe")


class TestFormatOptions(unittest.TestCase):
    """Разбор ответа сайта: что именно предложим пользователю и что попросим."""

    def setUp(self):
        self.options = eng.parse_formats(SITE_INFO)
        self.by_id = {o.format_id: o for o in self.options}

    def test_streams_without_codecs_are_dropped(self):
        self.assertNotIn("sb0", self.by_id)

    def test_same_height_different_fps_collapsed(self):
        """1080p30 и 1080p60 одним кодеком — один пункт списка."""
        self.assertIn("271", self.by_id)
        self.assertNotIn("248", self.by_id)

    def test_labels_are_readable(self):
        self.assertEqual(self.by_id["137"].label,
                         "1080p · MP4 · 4000 кбит/с  [137]")
        self.assertEqual(self.by_id["18"].label,
                         "360p · видео+звук · MP4 · 500 кбит/с · tiny  [18]")
        self.assertEqual(self.by_id["251"].label,
                         "только звук · WEBM · 160 кбит/с  [251]")

    def test_size_shown_when_site_reports_it(self):
        option = eng.parse_formats(
            {"formats": [{"format_id": "1", "ext": "mp4", "vcodec": "avc1",
              "acodec": "none", "height": 720,
              "filesize_approx": 12_000_000}]})[0]
        self.assertIn("МиБ", option.label)

    def test_choices_list_video_then_audio_then_best(self):
        labels = eng.format_choices(self.options)
        self.assertEqual(labels[-1], eng.DOWNLOAD_BEST)
        video_end = next(i for i, text in enumerate(labels)
                         if "только звук" in text)
        self.assertTrue(all("только звук" not in text
                            for text in labels[:video_end]))
        self.assertTrue(all("только звук" in text
                            for text in labels[video_end:-1]))

    def test_find_format_roundtrip(self):
        for option in self.options:
            self.assertIs(eng.find_format(self.options, option.label), option)
        self.assertIsNone(eng.find_format(self.options, eng.DOWNLOAD_BEST))
        self.assertIsNone(eng.find_format(self.options, "несуществующий"))

    def test_video_only_option_pulls_best_audio(self):
        """Видео без дорожки звука иначе скачается немаемым."""
        self.assertEqual(eng.build_format_spec(self.by_id["137"]),
                         "137+bestaudio/137")

    def test_ready_and_audio_options_used_as_is(self):
        self.assertEqual(eng.build_format_spec(self.by_id["18"]), "18")
        self.assertEqual(eng.build_format_spec(self.by_id["251"]), "251")
        self.assertEqual(eng.build_format_spec(None),
                         "bestvideo+bestaudio/best")

    def test_plan_merge_for_split_streams(self):
        spec, merge, remux = eng.plan_download(self.by_id["137"], "вид.mp4")
        self.assertEqual((spec, merge, remux), ("137+bestaudio/137", "mp4", ""))
        # Неизвестное расширение имени не даёт контейнер — берём разумный.
        self.assertEqual(eng.plan_download(self.by_id["137"], "вид")[1], "mp4")

    def test_plan_remux_when_container_differs(self):
        """WEBM-поток и имя .mp4: склейка в нужный контейнер."""
        # 271 — видео без звука, поэтому spec добавляет +bestaudio.
        # merge_ext берётся из имени файла, когда есть склейка.
        self.assertEqual(eng.plan_download(self.by_id["271"], "вид.mp4"),
                         ("271+bestaudio/271", "mp4", ""))
        self.assertEqual(eng.plan_download(self.by_id["271"], "вид.webm"),
                         ("271+bestaudio/271", "webm", ""))

    def test_plan_no_touching_for_ready_file(self):
        self.assertEqual(eng.plan_download(self.by_id["18"], "вид.mp4"),
                         ("18", "", ""))

    def test_plan_remux_for_ready_file_in_other_container(self):
        """Готовый MP4 и имя .mkv: только переупаковка, без перекодирования."""
        self.assertEqual(eng.plan_download(self.by_id["18"], "вид.mkv"),
                         ("18", "", "mkv"))

    def test_plan_audio_option_never_remuxes(self):
        """Аудиопоток скачивается как есть: контейнер имени его не касается."""
        self.assertEqual(eng.plan_download(self.by_id["251"], "песня.mp3"),
                         ("251", "", ""))


class TestDownloadMedia(unittest.TestCase):
    """Загрузка через заглушку yt-dlp: реальный процесс, но без сети."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffprog_dl_")
        self.fake = write_fake_ytdlp(self.tmp)
        self.media = os.path.join(self.tmp, "media.mp4")
        if HAS_FFMPEG:
            make_media(self.media, 2.0)
        else:  # без ffmpeg достаточно любого содержимого
            with open(self.media, "wb") as handle:
                handle.write(b"x" * 4096)
        self._which = eng.shutil.which
        self._environ = dict(os.environ)
        os.environ["FAKE_MEDIA"] = self.media
        eng.shutil.which = lambda name: (self.fake if name == "yt-dlp"
                                         else self._which(name))

    def tearDown(self):
        eng.shutil.which = self._which
        os.environ.clear()
        os.environ.update(self._environ)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_saved_path_and_logs_progress(self):
        target = os.path.join(self.tmp, "sub", "clip.mp4")
        lines: list[str] = []
        result = eng.download_media("https://vk.com/video1", target,
                                    log=lines.append)
        self.assertEqual(result, target)
        self.assertTrue(os.path.isfile(target))
        self.assertTrue(any("100%" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("$ ") for line in lines), lines)

    def test_ytdlp_failure_raises_engine_error(self):
        os.environ["FAKE_YTDLP_FAIL"] = "1"
        target = os.path.join(self.tmp, "clip.mp4")
        with self.assertRaises(eng.EngineError) as ctx:
            eng.download_media("https://vk.com/video1", target)
        self.assertIn("yt-dlp", str(ctx.exception))

    def test_resolve_downloaded_finds_appended_extension(self):
        real = os.path.join(self.tmp, "title.mp4")
        with open(real, "wb") as handle:
            handle.write(b"x")
        self.assertEqual(eng._resolve_downloaded(os.path.join(self.tmp, "title"),
                                                 "mp4"), real)

    def test_ytdlp_rename_extension(self):
        """yt-dlp может переименовать файл — заглушка это симулирует."""
        os.environ["FAKE_YTDLP_RENAME"] = "1"
        target = os.path.join(self.tmp, "clip.mp4")
        lines: list[str] = []
        result = eng.download_media("https://vk.com/video1", target,
                                    log=lines.append)
        # Заглушка переименовывает в .webm, но _resolve_downloaded найдёт.
        self.assertTrue(result.endswith(".webm"))
        self.assertTrue(os.path.isfile(result))

    def test_download_never_reencodes(self):
        """Скачивание не трогает кодеки: ffmpeg не запускается вовсе."""
        args_log = os.path.join(self.tmp, "args.txt")
        os.environ["FAKE_ARGS_LOG"] = args_log
        eng.download_media("https://x/y", os.path.join(self.tmp, "c.mp4"),
                           "18")
        with open(args_log, encoding="utf-8") as handle:
            sent = handle.read()
        self.assertIn("-f\n18\n", sent)
        # Ни remux, ни merge: поток 18 уже «видео+звук» в mp4.
        self.assertNotIn("--remux-video", sent)
        self.assertNotIn("--merge-output-format", sent)

    def test_direct_download_without_ytdlp_needs_direct_link(self):
        """Без yt-dlp сайт не скачать — подсказка должна быть понятной."""
        eng.shutil.which = lambda name: None
        with self.assertRaises(eng.EngineError) as ctx:
            eng.download_media("https://vk.com/video1",
                               os.path.join(self.tmp, "x.mp4"))
        self.assertIn("yt-dlp", str(ctx.exception))

    def test_list_formats_asks_site_once(self):
        """`-J` отдаёт JSON — на выходе готовые варианты для списка."""
        os.environ["FAKE_JSON"] = json.dumps(SITE_INFO)
        options = eng.list_download_formats("https://vk.com/video1")
        self.assertIn("137", {o.format_id for o in options})
        self.assertEqual(eng.format_choices(options)[-1], eng.DOWNLOAD_BEST)

    def test_list_formats_handles_playlist_answer(self):
        """На ссылку-плейлист yt-dlp отдаёт массив записей, а не объект."""
        os.environ["FAKE_JSON"] = json.dumps([SITE_INFO, SITE_INFO])
        options = eng.list_download_formats("https://vk.com/playlist1")
        self.assertIn("18", {o.format_id for o in options})

    def test_list_formats_without_ytdlp_explains_how_to_install(self):
        eng.shutil.which = lambda name: None
        with self.assertRaises(eng.EngineError) as ctx:
            eng.list_download_formats("https://vk.com/video1")
        self.assertIn("yt-dlp", str(ctx.exception))

    def test_remux_runs_without_reencoding(self):
        """Запрос .mkv для mp4-потока: yt-dlp получает --remux-video."""
        args_log = os.path.join(self.tmp, "args.txt")
        os.environ["FAKE_ARGS_LOG"] = args_log
        eng.download_media("https://x/y", os.path.join(self.tmp, "c.mkv"),
                           "18", remux_ext="mkv")
        with open(args_log, encoding="utf-8") as handle:
            sent = handle.read()
        self.assertIn("--remux-video\nmkv\n", sent)
        self.assertNotIn("--merge-output-format", sent)

    def test_pipeline_uses_download_as_first_step(self):
        """Цепочка «Скачать → Конвертация» не требует исходного файла заранее."""
        if not HAS_FFMPEG:
            self.skipTest("нет ffmpeg")
        target = os.path.join(self.tmp, "final.mp3")
        steps = [eng.DownloadStep(url="https://vk.com/video1", format_spec="18"),
                 eng.ConvertStep(fmt="mp3")]
        eng.Pipeline(lambda m: None).run("", steps, target)
        self.assertTrue(os.path.isfile(target))
        streams = eng.probe_streams(target)
        self.assertTrue(streams.has_audio)
        self.assertFalse(streams.has_video)


class TestParsing(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(eng.parse_time("90"), 90.0)
        self.assertEqual(eng.parse_time("1:30"), 90.0)
        self.assertEqual(eng.parse_time("01:02:03.5"), 3723.5)
        self.assertEqual(eng.parse_time("1,5"), 1.5)

    def test_parse_time_bad(self):
        for value in ("", "abc", "1:2:3:4"):
            with self.assertRaises(eng.EngineError):
                eng.parse_time(value)

    def test_atempo_chain(self):
        self.assertEqual(eng.atempo_chain(1.5), "atempo=1.5")
        self.assertEqual(eng.atempo_chain(4.0), "atempo=2.0,atempo=2.0")
        self.assertEqual(eng.atempo_chain(0.25), "atempo=0.5,atempo=0.5")
        self.assertTrue(all(0.5 <= float(f.split("=")[1]) <= 2.0
                            for f in eng.atempo_chain(7.3).split(",")))
        with self.assertRaises(eng.EngineError):
            eng.atempo_chain(0)

    def test_output_extension(self):
        self.assertEqual(eng.output_extension([eng.ConvertStep("mp3", "x")],
                                              "a.mp4"), ".mp3")
        self.assertEqual(eng.output_extension([eng.VolumeStep("1")], "a.mkv"),
                         ".mkv")
        self.assertEqual(eng.output_extension([], "a"), ".mp4")


class TestBuildArgs(unittest.TestCase):
    av = eng.Streams(has_video=True, has_audio=True)
    aud = eng.Streams(has_video=False, has_audio=True)
    vid = eng.Streams(has_video=True, has_audio=False)

    def test_convert_video(self):
        args = eng.build_convert_args("in.mp4", "out.mkv",
                                      eng.ConvertStep("mkv", "Высокое"), self.av)
        self.assertIn("-c:v", args)
        self.assertIn("libx264", args)
        self.assertEqual(args[args.index("-crf") + 1], "18")
        self.assertIn("aac", args)

    def test_convert_audio_only(self):
        args = eng.build_convert_args("in.mp4", "out.mp3",
                                      eng.ConvertStep("mp3", "Низкое"), self.av)
        self.assertIn("-vn", args)
        self.assertIn("libmp3lame", args)
        self.assertEqual(args[args.index("-b:a") + 1], "96k")

    def test_convert_lossless_has_no_bitrate(self):
        args = eng.build_convert_args("in.mp3", "out.flac",
                                      eng.ConvertStep("flac", "Высокое"), self.aud)
        self.assertNotIn("-b:a", args)

    def test_convert_requires_stream(self):
        with self.assertRaises(eng.EngineError):
            eng.build_convert_args("a.mp3", "a.mp4", eng.ConvertStep("mp4", "x"),
                                   self.aud)
        with self.assertRaises(eng.EngineError):
            eng.build_convert_args("a.mp4", "a.mp3", eng.ConvertStep("mp3", "x"),
                                   self.vid)

    def test_unknown_format_rejected_on_step_creation(self):
        with self.assertRaises(eng.EngineError):
            eng.ConvertStep("xyz", "Высокое")
        # нормализация: пробелы, заглавные буквы, ведущая точка
        step = eng.ConvertStep(" .MP3 ", "Высокое")
        self.assertEqual(step.fmt, "mp3")

    def test_convert_audio_only_source_drops_video(self):
        args = eng.build_convert_args("a.mp4", "a.mkv", eng.ConvertStep("mkv", "Среднее"),
                                      self.vid)
        self.assertIn("-an", args)

    # ------------------------------------------------ «— только звук —» (пустой формат)
    def test_empty_format_is_accepted(self):
        """Пустой формат — это легальный режим «вырезать картинку»."""
        step = eng.ConvertStep("")
        self.assertEqual(step.fmt, eng.AUDIO_ONLY_FORMAT)

    def test_empty_format_keeps_container_and_drops_video(self):
        """Контейнер берётся из расширения результата, видео вырезается."""
        args = eng.build_convert_args("in.mp4", "out.mp4",
                                      eng.ConvertStep(""), self.av)
        self.assertIn("-vn", args)
        self.assertNotIn("-c:v", args)
        # Контейнер mp4 → аудио-кодек aac, битрейт по качеству по умолчанию.
        self.assertIn("aac", args)
        self.assertEqual(args[args.index("-b:a") + 1],
                         eng.audio_bitrate(eng.DEFAULT_AUDIO_QUALITY))

    def test_empty_format_follows_target_container(self):
        """Расширение результата диктует кодек: mkv → aac, ogg → vorbis."""
        args = eng.build_convert_args("in.mkv", "out.ogg",
                                      eng.ConvertStep(""), self.av)
        self.assertIn("libvorbis", args)

    def test_empty_format_needs_audio_stream(self):
        with self.assertRaises(eng.EngineError):
            eng.build_convert_args("a.mp4", "a.mp4", eng.ConvertStep(""),
                                   self.vid)

    def test_empty_format_needs_target_extension(self):
        """Без расширения результата непонятно, в какой контейнер писать."""
        with self.assertRaises(eng.EngineError):
            eng.build_convert_args("in.mp4", "out", eng.ConvertStep(""),
                                   self.av)

    def test_empty_format_does_not_change_output_extension(self):
        """Пустой формат контейнер не меняет — расширение остаётся исходным."""
        self.assertEqual(
            eng.output_extension([eng.ConvertStep("")], "a.mp4"), ".mp4")
        # ...а обычная конвертация по-прежнему меняет.
        self.assertEqual(
            eng.output_extension([eng.ConvertStep("mp3")], "a.mp4"), ".mp3")

    def test_trim(self):
        args = eng.build_trim_args("in.mp4", "out.mkv",
                                   eng.TrimStep(60.0, 90.0), self.av)
        self.assertEqual(args[args.index("-ss") + 1], "60.000")
        self.assertEqual(args[args.index("-t") + 1], "30.000")
        self.assertIn("libx264", args)

    def test_trim_copy(self):
        args = eng.build_trim_args("in.mp4", "out.mkv",
                                   eng.TrimStep(1, 2, reencode=False), self.av)
        self.assertIn("copy", args)

    def test_trim_codecs_match_target_container(self):
        # x264/aac в mp3/ogg/webm-контейнер не пишутся — кодеки берутся по цели.
        mp3 = eng.build_trim_args("a.mp3", "out.mp3", eng.TrimStep(0, 1),
                                  self.aud)
        self.assertIn("libmp3lame", mp3)
        self.assertNotIn("libx264", mp3)
        webm = eng.build_trim_args("a.webm", "out.webm", eng.TrimStep(0, 1),
                                   self.av)
        self.assertIn("libvpx-vp9", webm)
        self.assertIn("libopus", webm)
        ogg = eng.build_trim_args("a.ogg", "out.ogg", eng.TrimStep(0, 1),
                                  self.aud)
        self.assertIn("libvorbis", ogg)
        avi = eng.build_trim_args("a.avi", "out.avi", eng.TrimStep(0, 1),
                                  self.av)
        self.assertIn("mpeg4", avi)
        self.assertIn("libmp3lame", avi)

    def test_convert_args_follow_target_extension(self):
        # Цель .mp3 при шаге на mkv (ошибочный путь) — пишем кодеки под цель.
        args = eng.build_convert_args("in.mp4", "out.aac",
                                      eng.ConvertStep("aac", "Высокое"), self.av)
        self.assertIn("aac", args)

    def test_trim_bad_range(self):
        with self.assertRaises(eng.EngineError):
            eng.build_trim_args("a", "b", eng.TrimStep(10, 5), self.av)

    def test_volume(self):
        args = eng.build_volume_args("in.mp4", "out.mp4",
                                     eng.VolumeStep("1.5"), self.av)
        self.assertEqual(args[args.index("-af") + 1], "volume=1.5")
        self.assertIn("copy", args)
        with self.assertRaises(eng.EngineError):
            eng.build_volume_args("a", "b", eng.VolumeStep("1"), self.vid)

    def test_speed(self):
        args = eng.build_speed_args("in.mp4", "out.mp4", eng.SpeedStep(2.0), self.av)
        self.assertEqual(args[args.index("-filter:v") + 1], "setpts=PTS/2.000000")
        self.assertEqual(args[args.index("-filter:a") + 1], "atempo=2.0")

    def test_concat_list_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            list_path = os.path.join(tmp, "c.txt")
            eng.build_concat_list(["/tmp/a b.mp4", "/tmp/it's.mp4"], list_path)
            with open(list_path, encoding="utf-8") as handle:
                content = handle.read()
        self.assertIn("file '/tmp/a b.mp4'", content)
        self.assertIn(r"file '/tmp/it'\''s.mp4'", content)

    def test_concat_copy_args(self):
        args = eng.build_concat_copy_args("l.txt", "o.mp4")
        self.assertEqual(args[:3], ["-y", "-f", "concat"])
        self.assertIn("copy", args)

    def test_concat_filter_needs_matching_stream_sets(self):
        # смешанные наборы потоков склеить одним фильтром нельзя
        with self.assertRaises(eng.EngineError):
            eng.build_concat_filter_args(["a.mp4", "b.mp4"], "o.mp4",
                                         [self.av, self.vid])

    def test_concat_filter_audio_only(self):
        args = eng.build_concat_filter_args(["a.mp3", "b.mp3"], "o.mp3",
                                            [self.aud, self.aud])
        self.assertIn("[outa]", args)
        self.assertNotIn("[outv]", args)
        self.assertIn("n=2:v=0:a=1", args[args.index("-filter_complex") + 1])
        self.assertIn("libmp3lame", args)

    def test_concat_filter_video_only(self):
        args = eng.build_concat_filter_args(["a.mp4", "b.mp4"], "o.mkv",
                                            [self.vid, self.vid])
        self.assertIn("[outv]", args)
        self.assertNotIn("[outa]", args)
        self.assertIn("n=2:v=1:a=0", args[args.index("-filter_complex") + 1])
        self.assertIn("libx264", args)

    def test_concat_filter_av_uses_container_codecs(self):
        args = eng.build_concat_filter_args(["a.mp4", "b.mp4"], "o.webm",
                                            [self.av, self.av])
        self.assertIn("libvpx-vp9", args)
        self.assertIn("libopus", args)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg не установлен")
class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ffprog_test_")
        cls.av = os.path.join(cls.tmp, "av.mp4")
        cls.av2 = os.path.join(cls.tmp, "av2.mp4")
        cls.au = os.path.join(cls.tmp, "a.mp3")
        make_media(cls.av, 3.0)
        make_media(cls.av2, 2.0)
        make_media(cls.au, 2.0, video=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.logs: list[str] = []
        self.out = os.path.join(self.tmp, f"out_{self.id().split('.')[-1]}")

    def duration(self, path: str) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True)
        return float(result.stdout.strip())

    def run_steps(self, steps, ext):
        target = self.out + ext
        eng.Pipeline(lambda m: self.logs.append(m)).run(self.av, steps, target)
        self.assertTrue(os.path.isfile(target))
        self.assertGreater(os.path.getsize(target), 0)
        return target

    def test_trim(self):
        target = self.run_steps([eng.TrimStep(1.0, 2.0)], ".mkv")
        self.assertAlmostEqual(self.duration(target), 1.0, delta=0.4)

    def test_convert_to_mp3(self):
        target = self.run_steps([eng.ConvertStep("mp3", "Среднее")], ".mp3")
        self.assertTrue(eng.probe_streams(target).has_audio)
        self.assertFalse(eng.probe_streams(target).has_video)

    def test_chain_trim_volume_speed(self):
        target = self.run_steps(
            [eng.TrimStep(0.0, 2.0), eng.VolumeStep("150%"),
             eng.SpeedStep(2.0)], ".mkv")
        self.assertAlmostEqual(self.duration(target), 1.0, delta=0.4)

    def test_convert_then_concat(self):
        target = self.out + "_concat.mp4"
        eng.Pipeline(lambda m: self.logs.append(m)).run(
            self.av, [eng.ConcatStep(files=[self.av2])], target)
        self.assertAlmostEqual(self.duration(target), 5.0, delta=0.6)

    def test_concat_inside_chain(self):
        """Склейка не первым шагом: результат ещё и конвертируется."""
        target = self.out + "_chain.mp3"
        eng.Pipeline(lambda m: self.logs.append(m)).run(
            self.av, [eng.ConcatStep(files=[self.av2]),
                      eng.ConvertStep("mp3", "Среднее")], target)
        streams = eng.probe_streams(target)
        self.assertTrue(streams.has_audio)
        self.assertFalse(streams.has_video)
        self.assertAlmostEqual(self.duration(target), 5.0, delta=0.8)

    def test_concat_needs_extra_file(self):
        target = self.out + "_empty.mp4"
        with self.assertRaises(eng.EngineError):
            eng.Pipeline(lambda m: self.logs.append(m)).run(
                self.av, [eng.ConcatStep(files=[])], target)

    def test_concat_fallback_different_containers(self):
        wav = os.path.join(self.tmp, "extra.wav")
        make_media(wav, 1.0)
        target = self.out + "_mix.mp4"
        eng.run_concat_with_fallback(self.av, target,
                                     eng.ConcatStep(files=[wav]),
                                     lambda m: self.logs.append(m))
        self.assertTrue(os.path.isfile(target))

    def test_audio_source_rejects_video_format(self):
        target = self.out + ".mp4"
        with self.assertRaises(eng.EngineError):
            eng.Pipeline(lambda m: self.logs.append(m)).run(
                self.au, [eng.ConvertStep("mp4", "Высокое")], target)

    def test_missing_source(self):
        with self.assertRaises(eng.EngineError):
            eng.Pipeline(lambda m: self.logs.append(m)).run(
                os.path.join(self.tmp, "nope.mp4"),
                [eng.VolumeStep("1")], self.out + ".mp4")

    def test_current_regressions(self):
        # регресс 1: обрезка на входе не-mp4 (кодек обязан подходить контейнеру)
        for name, steps in (
            ("trim_webm.webm", [eng.TrimStep(0.2, 1.2)]),
            ("trim_mp3.mp3", [eng.TrimStep(0.2, 1.2)]),
        ):
            with self.subTest(name=name):
                src = os.path.join(self.tmp, name.replace("trim_", "src_"))
                if name.endswith(".webm"):
                    subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                         "-f", "lavfi", "-i",
                         "testsrc=size=160x120:rate=10:duration=2",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                         "-shortest", "-c:v", "libvpx-vp9", "-c:a", "libopus",
                         src], check=True, capture_output=True)
                else:
                    make_media(src, 2.0, video=False)
                target = os.path.join(self.tmp, name)
                eng.Pipeline(lambda m: None).run(src, steps, target)
                self.assertTrue(os.path.getsize(target) > 0)

    def test_concat_audio_only_different_containers(self):
        # регресс 2: склейка аудиофайлов (mp3 + m4a) не должна падать
        other = os.path.join(self.tmp, "other.m4a")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
             "-c:a", "aac", other], check=True, capture_output=True)
        target = self.out + "_audio.mp3"
        eng.run_concat_with_fallback(self.au, target,
                                     eng.ConcatStep(files=[other]),
                                     lambda m: None)
        self.assertTrue(eng.probe_streams(target).has_audio)
        self.assertAlmostEqual(self.duration(target), 3.0, delta=0.6)

    def test_trim_mp3_source(self):
        target = self.out + "_trim.mp3"
        eng.Pipeline(lambda m: None).run(self.au, [eng.TrimStep(0.2, 1.2)],
                                         target)
        self.assertAlmostEqual(self.duration(target), 1.0, delta=0.4)
        self.assertFalse(eng.probe_streams(target).has_video)

    def test_temp_files_cleaned(self):
        before = set(os.listdir(self.tmp))
        self.run_steps([eng.TrimStep(0.0, 1.0), eng.VolumeStep("0.5")], ".mkv")
        self.assertEqual(set(os.listdir(self.tmp)) - before,
                         {os.path.basename(self.out) + ".mkv"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
