import tempfile
import unittest
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock
from unittest.mock import patch

from video_pipeline.config import PipelineConfig
from video_pipeline.edit_plan import edited_duration, map_subtitles_to_output
from video_pipeline.media import (
    extract_audio_chunks,
    prepare_video_for_shotstack,
    probe_video,
    validate_rendered_output,
)
from video_pipeline.models import SubtitleSegment, VideoInfo, WordTiming
from video_pipeline.pipeline import (
    _new_output_path,
    build_dry_run_plan,
    find_local_mp4s,
    process_local_video,
)
from video_pipeline.shotstack import ShotstackClient, ShotstackError, build_edit_payload
from video_pipeline.state import (
    DuplicateProcessingError,
    ProcessingLedger,
    RetryNotAllowedError,
)
from video_pipeline.style import StyleConfigError, VideoStyle, load_video_style
from video_pipeline.subtitles import (
    TranscriptionError,
    build_subtitle_segments,
    captions_to_srt,
    normalize_transcription,
)
from video_pipeline.transcription import transcribe_video
from video_pipeline.drive import DriveUploadError, upload_new_file
import json


class PipelineDryRunTests(unittest.TestCase):
    def test_finds_mp4_and_mov_case_insensitively_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "clip.MP4"
            movie = root / "phone.MOV"
            note = root / "notes.txt"
            video.write_bytes(b"test fixture, not a real video")
            movie.write_bytes(b"test fixture, not a real video")
            note.write_text("leave unchanged", encoding="utf-8")

            before = (video.read_bytes(), movie.read_bytes(), note.read_bytes())
            found = find_local_mp4s(root)

            self.assertEqual([item.name for item in found], ["clip.MP4", "phone.MOV"])
            self.assertEqual((video.read_bytes(), movie.read_bytes(), note.read_bytes()), before)

    def test_dry_run_makes_no_external_calls_and_includes_drive_destinations(self):
        config = PipelineConfig(
            input_folder_id="input-id",
            processed_folder_id="processed-id",
            logos_folder_id="logos-id",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_dry_run_plan(config, temp_dir)

        self.assertEqual(plan["external_services_called"], [])
        self.assertFalse(plan["source_files_modified"])
        self.assertEqual(plan["processed_folder_id"], "processed-id")
        self.assertEqual(plan["discovered_local_mp4_count"], 0)

    def test_missing_input_directory_is_an_empty_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "not-created"
            self.assertEqual(find_local_mp4s(missing), [])
            self.assertFalse(missing.exists())

class TranscriptionAndSubtitleTests(unittest.TestCase):
    def test_prefers_word_timestamps_and_applies_chunk_offset(self):
        response = {
            "text": "Hola mundo",
            "words": [
                {"word": "Hola", "start": 0.1, "end": 0.4},
                {"word": "mundo", "start": 0.4, "end": 0.8},
            ],
        }
        words = normalize_transcription(response, chunk_offset=30.0)
        self.assertEqual(words[0], WordTiming("Hola", 30.1, 30.4))
        self.assertEqual(words[1].start, 30.4)

    def test_falls_back_to_evenly_distributed_segment_timestamps(self):
        words = normalize_transcription(
            {"segments": [{"text": "uno dos tres", "start": 1.0, "end": 4.0}]}
        )
        self.assertEqual([word.text for word in words], ["uno", "dos", "tres"])
        self.assertEqual(words[0].start, 1.0)
        self.assertEqual(words[-1].end, 4.0)
        self.assertTrue(all(words[i].end <= words[i + 1].start for i in range(len(words) - 1)))

    def test_builds_2_to_4_word_segments_with_audio_timing(self):
        words = [
            WordTiming(text, index * 0.4, (index + 1) * 0.4)
            for index, text in enumerate("Uno dos tres cuatro cinco seis siete ocho nueve".split())
        ]
        captions = build_subtitle_segments(words, video_duration=10)
        counts = [len(caption.text.split()) for caption in captions]
        self.assertEqual(counts, [4, 3, 2])
        self.assertEqual(captions[0].start, 0)
        self.assertAlmostEqual(captions[-1].end, 3.6)

    def test_rebalances_sentence_tails_and_keeps_punctuation(self):
        words = [WordTiming(text, index, index + 0.5) for index, text in enumerate(
            ["Esto", "es", "una", "prueba.", "Final."]
        )]
        captions = build_subtitle_segments(words)
        self.assertEqual([len(c.text.split()) for c in captions], [3, 2])
        self.assertEqual(captions[0].text, "Esto es una")
        self.assertEqual(captions[1].text, "prueba. Final.")

    def test_rejects_transcripts_too_short_for_2_word_captions(self):
        with self.assertRaises(TranscriptionError):
            build_subtitle_segments([WordTiming("Solo.", 0, 1)])

    def test_srt_preserves_times_and_allows_configured_relevant_emoji(self):
        srt = captions_to_srt(
            [SubtitleSegment("viaje muy bonito", 1.25, 2.5)],
            {"viaje": "✈️"},
        )
        self.assertIn("00:00:01,250 --> 00:00:02,500", srt)
        self.assertIn("viaje muy bonito ✈️", srt)


class ShotstackPayloadTests(unittest.TestCase):
    def test_builds_vertical_video_with_visible_timed_subtitles(self):
        captions = [
            SubtitleSegment("Hola mundo", 0.2, 1.1),
            SubtitleSegment("Prueba vertical", 1.1, 2.5),
        ]
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            2.5,
            captions,
            "https://media.example/subtitles.srt",
            source_width=360,
            source_height=640,
        )
        self.assertEqual(payload["output"]["size"], {"width": 1080, "height": 1920})
        self.assertEqual(payload["output"]["quality"], "high")
        tracks = payload["timeline"]["tracks"]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]["clips"][0]["asset"]["type"], "rich-caption")
        self.assertEqual(tracks[0]["clips"][0]["asset"]["animation"]["style"], "highlight")
        self.assertEqual(tracks[0]["clips"][0]["asset"]["active"]["font"]["color"], "#FFD84D")
        self.assertEqual(tracks[0]["clips"][0]["position"], "bottom")
        self.assertEqual(tracks[1]["clips"][0]["fit"], "crop")

    def test_landscape_source_uses_contain_instead_of_cropping(self):
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            2.5,
            [SubtitleSegment("Hola mundo", 0, 1)],
            "https://media.example/subtitles.srt",
            source_width=1920,
            source_height=1080,
        )
        self.assertEqual(payload["timeline"]["tracks"][-1]["clips"][0]["fit"], "contain")

    def test_optional_logo_is_a_separate_configurable_overlay(self):
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            2.5,
            [SubtitleSegment("Hola mundo", 0, 1)],
            "https://media.example/subtitles.srt",
            style=VideoStyle(logo_scale=0.1),
            logo_url="https://media.example/logo.png",
            source_width=360,
            source_height=640,
        )
        logo_track = payload["timeline"]["tracks"][1]
        self.assertEqual(logo_track["clips"][0]["asset"]["type"], "image")
        self.assertEqual(logo_track["clips"][0]["scale"], 0.1)

    def test_simple_subtitles_are_static_and_do_not_highlight_words(self):
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            2.5,
            [SubtitleSegment("Hola mundo", 0.2, 1.1)],
            "https://media.example/subtitles.srt",
            subtitle_mode="simple",
        )
        asset = payload["timeline"]["tracks"][0]["clips"][0]["asset"]
        self.assertEqual(asset["animation"]["style"], "none")
        self.assertNotIn("active", asset)

    def test_no_subtitles_omits_caption_track_and_subtitle_asset(self):
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            2.5,
            [],
            None,
            subtitle_mode="none",
            source_width=360,
            source_height=640,
        )
        tracks = payload["timeline"]["tracks"]
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["clips"][0]["asset"]["type"], "video")

    def test_manual_cuts_zoom_and_stickers_build_independent_timeline_tracks(self):
        payload = build_edit_payload(
            "https://media.example/video.mp4",
            10,
            [],
            None,
            style=VideoStyle(output_width=720, output_height=1280, output_fps=50),
            source_width=2160,
            source_height=3840,
            subtitle_mode="none",
            cuts=[{"start_seconds": 4, "end_seconds": 5}],
            zooms=[{"start_seconds": 2, "end_seconds": 4, "effect": "zoomIn"}],
            stickers=[{
                "sticker": "sparkle",
                "start_seconds": 3.6,
                "duration_seconds": 2,
                "position": "topRight",
                "effect": "zoomIn",
                "color": "#FFD84D",
            }],
        )
        self.assertEqual(payload["output"]["size"], {"width": 720, "height": 1280})
        self.assertEqual(payload["output"]["fps"], 50)
        tracks = payload["timeline"]["tracks"]
        self.assertEqual([clip["asset"]["type"] for clip in tracks[0]["clips"]], ["svg", "svg"])
        self.assertTrue(all(clip["effect"] == "zoomIn" for clip in tracks[0]["clips"]))
        video_clips = tracks[-1]["clips"]
        self.assertEqual([clip["trim"] for clip in video_clips], [0, 2, 5])
        self.assertEqual([clip["start"] for clip in video_clips], [0, 2, 4])
        self.assertEqual([clip.get("effect") for clip in video_clips], [None, "zoomIn", None])
        self.assertNotIn("rich-caption", [
            clip["asset"]["type"] for track in tracks for clip in track["clips"]
        ])
        self.assertIn("<svg", tracks[0]["clips"][0]["asset"]["src"])

    def test_edit_plan_rejects_overlapping_cuts_and_unknown_stickers(self):
        with self.assertRaises(ValueError):
            build_edit_payload(
                "https://media.example/video.mp4", 10, [], None, subtitle_mode="none",
                cuts=[
                    {"start_seconds": 1, "end_seconds": 3},
                    {"start_seconds": 2, "end_seconds": 4},
                ],
            )
        with self.assertRaises(ValueError):
            build_edit_payload(
                "https://media.example/video.mp4", 10, [], None, subtitle_mode="none",
                stickers=[{
                    "sticker": "<svg/>",
                    "start_seconds": 1,
                    "duration_seconds": 1,
                }],
            )

    def test_subtitles_shift_with_cuts_and_do_not_extend_into_removed_ranges(self):
        cuts = [{"start_seconds": 4, "end_seconds": 5}]
        mapped = map_subtitles_to_output([
            SubtitleSegment("antes y después", 3.5, 5.5),
            SubtitleSegment("texto eliminado", 4.2, 4.8),
            SubtitleSegment("continúa aquí", 5.5, 6),
        ], cuts)
        self.assertEqual(
            [(item.text, item.start, item.end) for item in mapped],
            [
                ("antes y después", 3.5, 4.5),
                ("continúa aquí", 4.5, 5.0),
            ],
        )
        self.assertEqual(edited_duration(10, cuts), 9)

    def test_rejects_non_https_video_source(self):
        with self.assertRaises(ValueError):
            build_edit_payload(
                "file:///tmp/video.mp4", 5, [SubtitleSegment("Hola mundo", 0, 1)],
                "https://media.example/subtitles.srt",
            )

    def test_rejects_subtitle_outside_video_duration(self):
        with self.assertRaises(ValueError):
            build_edit_payload(
                "https://media.example/video.mp4",
                1,
                [SubtitleSegment("Hola mundo", 2, 3)],
                "https://media.example/subtitles.srt",
            )


class ProviderAdapterTests(unittest.TestCase):
    def test_openai_adapter_requests_verbose_word_and_segment_timestamps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.mp3"
            audio_path.write_bytes(b"test audio placeholder")
            info = VideoInfo("source.mp4", 1.0, 360, 640, True)
            client = Mock()
            client.audio.transcriptions.create.return_value = {
                "words": [{"word": "Hola", "start": 0.0, "end": 0.4},
                          {"word": "mundo", "start": 0.4, "end": 0.9}]
            }
            with unittest.mock.patch(
                "video_pipeline.transcription.extract_audio_chunks",
                return_value=iter([(audio_path, 0.0)]),
            ):
                words = transcribe_video(info, temp_dir, client=client)

        self.assertEqual([word.text for word in words], ["Hola", "mundo"])
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "whisper-1")
        self.assertEqual(kwargs["response_format"], "verbose_json")
        self.assertEqual(kwargs["timestamp_granularities"], ["word", "segment"])

    def test_shotstack_upload_flow_uses_signed_url_without_forwarding_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "clip.mp4"
            source.write_bytes(b"fake media bytes")
            session = Mock()
            session.post.return_value.json.return_value = {
                "data": {
                    "id": "source-id",
                    "attributes": {"url": "https://shotstack-ingest-api-stage-sources.s3.amazonaws.com/signed"},
                }
            }
            session.get.return_value.json.return_value = {
                "data": {
                    "id": "source-id",
                    "attributes": {
                        "status": "ready",
                        "source": "https://shotstack-ingest-api-stage-sources.s3.amazonaws.com/source.mp4",
                    },
                }
            }
            session.put.return_value.raise_for_status.return_value = None
            client = ShotstackClient(
                api_key="test-only-not-a-real-key",
                session=session,
                poll_seconds=0.001,
                timeout_seconds=1,
            )
            url = client.upload_local_video(source)

        self.assertEqual(url, "https://shotstack-ingest-api-stage-sources.s3.amazonaws.com/source.mp4")
        self.assertIn("/ingest/stage/upload", session.post.call_args.args[0])
        self.assertIn("/ingest/stage/sources/", session.get.call_args.args[0])
        self.assertNotIn("x-api-key", session.put.call_args.kwargs["headers"])

    def test_shotstack_client_rejects_production_environment(self):
        with self.assertRaises(ValueError):
            ShotstackClient(api_key="test-only-not-a-real-key", environment="v1", session=Mock())

    def test_render_download_refuses_to_delete_or_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "output.mp4"
            destination.write_bytes(b"keep me")
            session = Mock()
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            session.get.return_value = response
            client = ShotstackClient(api_key="test-only-not-a-real-key", session=session)

            with self.assertRaises(ShotstackError):
                client.download_render("https://media.example/output.mp4", destination)

            self.assertEqual(destination.read_bytes(), b"keep me")


class LocalMediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_probes_and_extracts_audio_from_synthetic_local_mp4(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "fixture.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(video_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            info = probe_video(video_path)
            chunks = list(extract_audio_chunks(info, Path(temp_dir) / "work"))

            self.assertEqual((info.width, info.height), (360, 640))
            self.assertTrue(info.has_audio)
            self.assertEqual(len(chunks), 1)
            self.assertGreater(chunks[0][0].stat().st_size, 0)
            self.assertTrue(video_path.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_probe_accepts_h264_mov_and_leaves_compatible_source_untouched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "iphone.MOV"
            work = root / "work"
            work.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            original_bytes = source.read_bytes()

            source_info = probe_video(source)
            compatible, conversion = prepare_video_for_shotstack(source_info, work)

            self.assertEqual(source_info.video_codec, "h264")
            self.assertTrue(source_info.has_audio)
            self.assertEqual((source_info.width, source_info.height), (360, 640))
            self.assertEqual(Path(compatible.path), source.resolve())
            self.assertFalse(conversion["converted"])
            self.assertEqual(source.read_bytes(), original_bytes)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_hevc_mp4_is_transcoded_to_h264_without_changing_original(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "iphone-hevc.mp4"
            work = root / "work"
            work.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1:r=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1:log-level=error",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            original_bytes = source.read_bytes()

            source_info = probe_video(source)
            compatible, conversion = prepare_video_for_shotstack(source_info, work)

            self.assertEqual(source_info.video_codec, "hevc")
            self.assertEqual(probe_video(compatible.path).video_codec, "h264")
            self.assertTrue(conversion["converted"])
            self.assertEqual(conversion["method"], "transcode")
            self.assertNotEqual(Path(compatible.path), source.resolve())
            self.assertEqual(source.read_bytes(), original_bytes)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_validates_synthetic_h264_1080x1920_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.mp4"
            output_path = root / "output.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=1:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(output_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            source_info = probe_video(source_path)
            output_info = validate_rendered_output(source_info, output_path, VideoStyle())

            self.assertEqual((output_info.width, output_info.height), (1080, 1920))
            self.assertEqual(output_info.video_codec, "h264")
            self.assertEqual(output_info.audio_codec, "aac")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_offline_pipeline_orchestration_preserves_original_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "input.mp4"
            output_dir = root / "outputs"
            state_path = root / "state.sqlite3"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source_path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            original_bytes = source_path.read_bytes()

            blocked_openai = Mock()
            blocked_shotstack = Mock()
            with self.assertRaisesRegex(ValueError, "exceeds the configured limit"):
                process_local_video(
                    source_path,
                    output_dir,
                    paid_api_authorized=True,
                    openai_client=blocked_openai,
                    shotstack_client=blocked_shotstack,
                    config=PipelineConfig(shotstack_environment="stage"),
                    style=VideoStyle(
                        openai_price_per_audio_minute_usd=1.0,
                        shotstack_price_per_render_minute_usd=1.0,
                        max_estimated_cost_usd=0.01,
                    ),
                    state_db_path=root / "blocked.sqlite3",
                )
            blocked_openai.audio.transcriptions.create.assert_not_called()
            blocked_shotstack.upload_local_video.assert_not_called()

            class OfflineShotstackStub:
                def upload_local_video(self, path):
                    self.uploaded_video = Path(path)
                    return "https://media.example/ingested.mp4"

                def upload_local_asset(self, path, content_type):
                    self.subtitle_srt = Path(path).read_text(encoding="utf-8")
                    self.content_type = content_type
                    return "https://media.example/captions.srt"

                def submit_render(self, payload):
                    self.payload = payload
                    return "offline-render-id"

                def wait_for_render(self, render_id):
                    self.waited_for = render_id
                    return "https://media.example/rendered.mp4"

                def download_render(self, url, destination):
                    subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=1:r=30",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-shortest", str(destination),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    return Path(destination)

            renderer = OfflineShotstackStub()
            drive_uploader = Mock(
                return_value={
                    "id": "processed-drive-file-id",
                    "name": "fixture_vertical_subtitled.mp4",
                    "parents": ["processed-folder"],
                }
            )
            with unittest.mock.patch(
                "video_pipeline.pipeline.transcribe_video",
                return_value=[WordTiming("Hola", 0, 0.45), WordTiming("mundo.", 0.45, 1)],
            ) as transcription:
                result = process_local_video(
                    source_path,
                    output_dir,
                    paid_api_authorized=True,
                    subtitle_mode="dynamic",
                    openai_client=Mock(),
                    shotstack_client=renderer,
                    drive_uploader=drive_uploader,
                    drive_access_token="test-google",
                    config=PipelineConfig(
                        shotstack_environment="stage",
                        processed_folder_id="processed-folder",
                    ),
                    state_db_path=state_path,
                )
                transcription.assert_called_once()

            self.assertTrue(Path(result["output"]).is_file())
            self.assertEqual(result["drive_output"]["id"], "processed-drive-file-id")
            drive_uploader.assert_called_once()
            self.assertFalse(result["drive_upload_skipped"])
            self.assertEqual(result["output_dimensions"], {"width": 1080, "height": 1920})
            self.assertEqual(result["shotstack_environment"], "stage")
            self.assertFalse(result["drive_upload_skipped"])
            self.assertEqual(renderer.content_type, "application/x-subrip")
            self.assertIn("Hola mundo.", renderer.subtitle_srt)
            self.assertEqual(renderer.payload["timeline"]["tracks"][0]["clips"][0]["asset"]["type"], "rich-caption")
            self.assertEqual(source_path.read_bytes(), original_bytes)
            renderer.submit_render = Mock(side_effect=AssertionError("duplicate must not render"))
            with self.assertRaises(DuplicateProcessingError):
                process_local_video(
                    source_path,
                    output_dir,
                    paid_api_authorized=True,
                    subtitle_mode="dynamic",
                    openai_client=Mock(),
                    shotstack_client=renderer,
                    drive_uploader=drive_uploader,
                    drive_access_token="test-google",
                    config=PipelineConfig(
                        shotstack_environment="stage",
                        processed_folder_id="processed-folder",
                    ),
                    state_db_path=state_path,
                )
            self.assertEqual(source_path.read_bytes(), original_bytes)

    def test_no_subtitles_skips_openai_and_subtitle_ingest(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "clip.mov"
            source.write_bytes(b"original-video")
            original_bytes = source.read_bytes()
            source_info = VideoInfo(
                str(source), 1.0, 360, 640, True,
                video_codec="h264", audio_codec="aac", fps=30,
                size_bytes=source.stat().st_size,
            )
            output_info = VideoInfo(
                str(root / "outputs" / "clip_vertical_edited.mp4"),
                1.0, 1080, 1920, True,
                video_codec="h264", audio_codec="aac", fps=30,
            )

            class LocalRenderer:
                def __init__(self):
                    self.uploaded_assets = []
                    self.payload = None

                def upload_local_video(self, path):
                    return "https://media.example/source.mov"

                def upload_local_asset(self, path, content_type):
                    self.uploaded_assets.append(content_type)
                    raise AssertionError("no-caption mode must not ingest subtitle assets")

                def submit_render(self, payload):
                    self.payload = payload
                    return "offline-render-id"

                def wait_for_render(self, render_id):
                    return "https://media.example/rendered.mp4"

                def download_render(self, url, destination):
                    Path(destination).write_bytes(b"new-render")
                    return Path(destination)

            renderer = LocalRenderer()
            with (
                patch("video_pipeline.pipeline.probe_video", return_value=source_info),
                patch(
                    "video_pipeline.pipeline.prepare_video_for_shotstack",
                    return_value=(SimpleNamespace(path=source, width=360, height=640), None),
                ),
                patch("video_pipeline.pipeline.validate_rendered_output", return_value=output_info),
                patch("video_pipeline.pipeline.transcribe_video") as transcription,
            ):
                result = process_local_video(
                    source,
                    root / "outputs",
                    paid_api_authorized=True,
                    subtitle_mode="none",
                    shotstack_client=renderer,
                    upload_to_drive=False,
                    config=PipelineConfig(shotstack_environment="stage"),
                    state_db_path=root / "state.sqlite3",
                )

            transcription.assert_not_called()
            self.assertEqual(renderer.uploaded_assets, [])
            self.assertEqual(result["subtitle_segment_count"], 0)
            self.assertEqual(result["paid_operations"]["openai_transcription_chunks"], 0)
            self.assertEqual(len(renderer.payload["timeline"]["tracks"]), 1)
            self.assertEqual(
                renderer.payload["timeline"]["tracks"][0]["clips"][0]["asset"]["volume"],
                1.0,
            )
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertTrue(Path(result["output"]).name.endswith("_vertical_edited.mp4"))


class StateAndStyleTests(unittest.TestCase):
    def test_ledger_prevents_duplicates_and_allows_only_one_explicit_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProcessingLedger(Path(temp_dir) / "state.sqlite3")
            job = ledger.claim("hash-1", "input.mp4")
            self.assertEqual(job["attempts"], 1)
            ledger.update("hash-1", captions_json='[{"text":"hola mundo","start":0,"end":1}]')
            ledger.mark_failed("hash-1", "temporary problem")
            with self.assertRaises(RetryNotAllowedError):
                ledger.claim("hash-1", "input.mp4")
            retried = ledger.claim("hash-1", "input.mp4", retry_failed=True)
            self.assertEqual(retried["attempts"], 2)
            self.assertIsNotNone(retried["captions_json"])
            ledger.mark_completed("hash-1", "outputs/video.mp4")
            with self.assertRaises(DuplicateProcessingError):
                ledger.claim("hash-1", "input.mp4", retry_failed=True)

    def test_ledger_does_not_reuse_provider_assets_for_different_edit_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = ProcessingLedger(Path(temp_dir) / "state.sqlite3")
            options = {"subtitle_mode": "none", "output_fps": 50}
            ledger.claim("same-source", "input.mov", edit_options=options)
            ledger.update("same-source", render_id="stage-render-id")
            ledger.mark_failed("same-source", "temporary problem")

            with self.assertRaisesRegex(RetryNotAllowedError, "settings differ"):
                ledger.claim(
                    "same-source",
                    "input.mov",
                    retry_failed=True,
                    edit_options={"subtitle_mode": "dynamic", "output_fps": 50},
                )

            resumed = ledger.claim(
                "same-source",
                "input.mov",
                retry_failed=True,
                edit_options=options,
            )
            self.assertEqual(resumed["attempts"], 2)
            self.assertEqual(resumed["render_id"], "stage-render-id")

    def test_style_file_validates_supported_visual_and_usage_options(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "style.json"
            path.write_text('{"animation_style":"karaoke","font_size":72}', encoding="utf-8")
            style = load_video_style(path)
            self.assertEqual(style.animation_style, "karaoke")
            self.assertEqual(style.font_size, 72)
            path.write_text('{"animation_style":"made-up"}', encoding="utf-8")
            with self.assertRaises(StyleConfigError):
                load_video_style(path)


class SafetyTests(unittest.TestCase):
    def test_live_processing_requires_explicit_paid_api_authorization(self):
        openai_client = Mock()
        shotstack_client = Mock()
        with self.assertRaises(PermissionError):
            process_local_video(
                "does-not-exist.mp4",
                paid_api_authorized=False,
                openai_client=openai_client,
                shotstack_client=shotstack_client,
            )
        openai_client.audio.transcriptions.create.assert_not_called()
        shotstack_client.upload_local_video.assert_not_called()

    def test_new_output_name_does_not_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"original")
            existing = root / "source_vertical_subtitled.mp4"
            existing.write_bytes(b"prior output")
            output, lock = _new_output_path(root, source)
            self.assertEqual(output.name, "source_vertical_subtitled_2.mp4")
            self.assertTrue(lock.exists())
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(existing.read_bytes(), b"prior output")
            lock.unlink()

class DriveUploadTests(unittest.TestCase):
    def test_creates_new_drive_file_with_resumable_chunks(self):
        chunk_size = 256 * 1024
        first_chunk = Mock(status_code=308, headers={"Range": f"bytes=0-{chunk_size - 1}"})
        second_chunk = Mock(
            status_code=308,
            headers={"Range": f"bytes=0-{2 * chunk_size - 1}"},
        )
        completed = Mock(status_code=200)
        completed.json.return_value = {
            "id": "new-file-id",
            "name": "render.mp4",
            "mimeType": "video/mp4",
            "parents": ["processed-folder"],
        }
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.headers = {
            "Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=opaque"
        }
        session.put.side_effect = [first_chunk, second_chunk, completed]

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "render.mp4"
            source.write_bytes(b"r" * (2 * chunk_size + 17))
            result = upload_new_file(
                source,
                folder_id="processed-folder",
                access_token="test-google-token",
                session=session,
                chunk_size=chunk_size,
            )

        self.assertEqual(result["id"], "new-file-id")
        metadata = session.post.call_args.kwargs["json"]
        self.assertEqual(metadata["name"], "render.mp4")
        self.assertEqual(metadata["parents"], ["processed-folder"])
        self.assertEqual(session.put.call_count, 3)
        self.assertEqual(session.put.call_args_list[0].kwargs["headers"]["Content-Range"],
                         f"bytes 0-{chunk_size - 1}/{2 * chunk_size + 17}")
        self.assertEqual(session.put.call_args_list[-1].kwargs["headers"]["Content-Range"],
                         f"bytes {2 * chunk_size}-{2 * chunk_size + 16}/{2 * chunk_size + 17}")
        self.assertTrue(all(not item.kwargs["allow_redirects"] for item in session.put.call_args_list))

    def test_rejects_an_untrusted_upload_url_without_sending_video(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.headers = {
            "Location": "https://example.invalid/steal-token"
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "render.mp4"
            source.write_bytes(b"render")
            with self.assertRaises(DriveUploadError):
                upload_new_file(
                    source,
                    folder_id="processed-folder",
                    access_token="test-google-token",
                    session=session,
                )
        session.put.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class ExplicitLivePipelineTests(unittest.TestCase):
    def test_paid_processing_is_blocked_before_probe_without_explicit_authorization(self):
        with patch("video_pipeline.pipeline.probe_video") as probe:
            with self.assertRaises(PermissionError):
                process_local_video("missing.mp4")
        probe.assert_not_called()

    def test_drive_token_is_required_before_paid_provider_calls(self):
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
        }
        with patch.dict("os.environ", environment, clear=True):
            with patch(
                "video_pipeline.pipeline.probe_video",
                return_value=VideoInfo(
                    "missing.mp4", 2.0, 360, 640, True,
                    video_codec="h264", audio_codec="aac",
                ),
            ) as probe:
                with patch("video_pipeline.pipeline.transcribe_video") as transcribe:
                    with self.assertRaisesRegex(RuntimeError, "GOOGLE_OAUTH_TOKEN"):
                        process_local_video(
                            "missing.mp4",
                            paid_api_authorized=True,
                            openai_client=Mock(),
                            shotstack_client=Mock(),
                        config=PipelineConfig(processed_folder_id="processed-folder"),
                        )
        probe.assert_called_once()
        transcribe.assert_not_called()

    def test_local_only_mode_skips_drive_and_does_not_require_oauth(self):
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
        }
        renderer = Mock()
        renderer.upload_local_video.return_value = "https://ingest.example/video.mp4"
        renderer.upload_local_asset.return_value = "https://ingest.example/captions.srt"
        renderer.submit_render.return_value = "render-id"
        renderer.wait_for_render.return_value = "https://renders.example/render.mp4"

        def save_render(_url, destination):
            Path(destination).write_bytes(b"new-render")
            return Path(destination)

        renderer.download_render.side_effect = save_render
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"original-video")
            info = VideoInfo(
                str(source), 1.0, 360, 640, True, video_codec="h264", audio_codec="aac"
            )
            with patch.dict("os.environ", environment, clear=True), patch(
                "video_pipeline.pipeline.probe_video", return_value=info
            ), patch(
                "video_pipeline.pipeline.transcribe_video",
                return_value=[WordTiming("Hola", 0.0, 0.5), WordTiming("mundo.", 0.5, 1.0)],
            ), patch(
                "video_pipeline.pipeline.validate_rendered_output",
                return_value=Mock(width=1080, height=1920, video_codec="h264", audio_codec="aac", fps=30),
            ), patch("video_pipeline.pipeline.upload_new_file") as drive_upload:
                result = process_local_video(
                    source,
                    root / "outputs",
                    paid_api_authorized=True,
                    upload_to_drive=False,
                    openai_client=Mock(),
                    shotstack_client=renderer,
                    config=PipelineConfig(shotstack_environment="stage"),
                    state_db_path=root / "state.sqlite3",
                )

            self.assertTrue(Path(result["output"]).is_file())
            self.assertTrue(result["drive_upload_skipped"])
            self.assertIsNone(result["drive_output"])
            self.assertEqual(source.read_bytes(), b"original-video")
            drive_upload.assert_not_called()

    def test_new_render_upload_accepts_token_from_replit_secret(self):
        source_info = VideoInfo(
            "source.mp4", 2.0, 360, 640, True, video_codec="h264", audio_codec="aac"
        )
        words = [WordTiming("Hola", 0.0, 0.5), WordTiming("mundo.", 0.5, 1.0)]
        renderer = Mock()
        renderer.upload_local_video.return_value = "https://ingest.example/video.mp4"
        renderer.upload_local_asset.return_value = "https://ingest.example/captions.srt"
        renderer.submit_render.return_value = "render-id"
        renderer.wait_for_render.return_value = "https://renders.example/render.mp4"

        def save_render(_url, destination):
            Path(destination).write_bytes(b"new-render")
            return Path(destination)

        renderer.download_render.side_effect = save_render
        drive_uploader = Mock(
            return_value={
                "id": "created-file-id",
                "name": "source_vertical_subtitled.mp4",
                "parents": ["processed-folder"],
            }
        )
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
            "GOOGLE_OAUTH_TOKEN": "test-google",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"original-video")
            original_before = source.read_bytes()
            output_dir = root / "outputs"
            state_path = root / "processing.sqlite3"
            with patch.dict("os.environ", environment, clear=True), patch(
                "video_pipeline.pipeline.probe_video", return_value=source_info
            ), patch(
                "video_pipeline.pipeline.transcribe_video", return_value=words
            ), patch(
                "video_pipeline.pipeline.validate_rendered_output",
                return_value=Mock(
                    width=1080,
                    height=1920,
                    video_codec="h264",
                    audio_codec="aac",
                    fps=30,
                ),
            ):
                result = process_local_video(
                    source,
                    output_dir,
                    paid_api_authorized=True,
                    openai_client=Mock(),
                    shotstack_client=renderer,
                    drive_uploader=drive_uploader,
                    config=PipelineConfig(processed_folder_id="processed-folder"),
                    state_db_path=state_path,
                )

            self.assertEqual(source.read_bytes(), original_before)
            self.assertEqual(Path(result["output"]).read_bytes(), b"new-render")

        renderer.wait_for_render.assert_called_once_with("render-id")
        drive_uploader.assert_called_once()
        self.assertEqual(drive_uploader.call_args.args[0], Path(result["output"]))
        self.assertEqual(drive_uploader.call_args.kwargs["folder_id"], "processed-folder")
        self.assertEqual(drive_uploader.call_args.kwargs["access_token"], "test-google")
        self.assertEqual(result["drive_output"]["id"], "created-file-id")
        self.assertTrue(result["render_status_verified"])
        self.assertFalse(result["source_modified"])

    def test_new_render_is_uploaded_only_after_verified_render_and_source_is_untouched(self):
        source_info = VideoInfo(
            "source.mp4", 2.0, 360, 640, True,
            video_codec="h264", audio_codec="aac",
        )
        words = [WordTiming("Hola", 0.0, 0.5), WordTiming("mundo.", 0.5, 1.0)]
        renderer = Mock()
        renderer.upload_local_video.return_value = "https://ingest.example/video.mp4"
        renderer.upload_local_asset.return_value = "https://ingest.example/captions.srt"
        renderer.submit_render.return_value = "render-id"
        renderer.wait_for_render.return_value = "https://renders.example/render.mp4"

        def save_render(_url, destination):
            Path(destination).write_bytes(b"new-render")
            return Path(destination)

        renderer.download_render.side_effect = save_render
        drive_uploader = Mock(
            return_value={
                "id": "created-file-id",
                "name": "source_vertical_subtitled.mp4",
                "parents": ["processed-folder"],
            }
        )
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
            "GOOGLE_OAUTH_TOKEN": "test-google",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"original-video")
            original_before = source.read_bytes()
            output_dir = root / "outputs"
            state_path = root / "processing.sqlite3"
            with patch.dict("os.environ", environment, clear=True), patch(
                "video_pipeline.pipeline.probe_video", return_value=source_info
            ), patch(
                "video_pipeline.pipeline.transcribe_video", return_value=words
            ), patch(
                "video_pipeline.pipeline.validate_rendered_output",
                return_value=Mock(
                    width=1080,
                    height=1920,
                    video_codec="h264",
                    audio_codec="aac",
                    fps=30,
                ),
            ):
                result = process_local_video(
                    source,
                    output_dir,
                    paid_api_authorized=True,
                    openai_client=Mock(),
                    shotstack_client=renderer,
                    drive_uploader=drive_uploader,
                    drive_access_token="test-google",
                    config=PipelineConfig(processed_folder_id="processed-folder"),
                    state_db_path=state_path,
                )

            self.assertEqual(source.read_bytes(), original_before)
            self.assertEqual(Path(result["output"]).read_bytes(), b"new-render")

        renderer.wait_for_render.assert_called_once_with("render-id")
        drive_uploader.assert_called_once()
        self.assertEqual(drive_uploader.call_args.args[0], Path(result["output"]))
        self.assertEqual(drive_uploader.call_args.kwargs["folder_id"], "processed-folder")
        self.assertEqual(drive_uploader.call_args.kwargs["access_token"], "test-google")
        self.assertEqual(result["drive_output"]["id"], "created-file-id")
        self.assertTrue(result["render_status_verified"])
        self.assertFalse(result["source_modified"])

    def test_failed_render_never_uploads_to_drive_with_secret_token(self):
        source_info = VideoInfo(
            "source.mp4", 2.0, 360, 640, True, video_codec="h264", audio_codec="aac"
        )
        renderer = Mock()
        renderer.upload_local_video.return_value = "https://ingest.example/video.mp4"
        renderer.upload_local_asset.return_value = "https://ingest.example/captions.srt"
        renderer.submit_render.return_value = "render-id"
        renderer.wait_for_render.side_effect = ShotstackError("render failed")
        drive_uploader = Mock()
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
            "GOOGLE_OAUTH_TOKEN": "test-google",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"original-video")
            with patch.dict("os.environ", environment, clear=True), patch(
                "video_pipeline.pipeline.probe_video", return_value=source_info
            ), patch(
                "video_pipeline.pipeline.transcribe_video",
                return_value=[
                    WordTiming("Hola", 0.0, 0.5),
                    WordTiming("mundo.", 0.5, 1.0),
                ],
            ):
                with self.assertRaisesRegex(ShotstackError, "render failed"):
                    process_local_video(
                        source,
                        Path(temp_dir) / "outputs",
                        paid_api_authorized=True,
                        openai_client=Mock(),
                        shotstack_client=renderer,
                        drive_uploader=drive_uploader,
                        config=PipelineConfig(processed_folder_id="processed-folder"),
                        state_db_path=Path(temp_dir) / "processing.sqlite3",
                    )
            self.assertEqual(source.read_bytes(), b"original-video")

        drive_uploader.assert_not_called()

    def test_failed_render_never_uploads_to_drive(self):
        source_info = VideoInfo(
            "source.mp4", 2.0, 360, 640, True,
            video_codec="h264", audio_codec="aac",
        )
        renderer = Mock()
        renderer.upload_local_video.return_value = "https://ingest.example/video.mp4"
        renderer.upload_local_asset.return_value = "https://ingest.example/captions.srt"
        renderer.submit_render.return_value = "render-id"
        renderer.wait_for_render.side_effect = ShotstackError("render failed")
        drive_uploader = Mock()
        environment = {
            "OPENAI_API_KEY": "test-openai",
            "SHOTSTACK_API_KEY": "test-shotstack",
            "GOOGLE_OAUTH_TOKEN": "test-google",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"original-video")
            with patch.dict("os.environ", environment, clear=True), patch(
                "video_pipeline.pipeline.probe_video", return_value=source_info
            ), patch(
                "video_pipeline.pipeline.transcribe_video",
                return_value=[
                    WordTiming("Hola", 0.0, 0.5),
                    WordTiming("mundo.", 0.5, 1.0),
                ],
            ):
                with self.assertRaisesRegex(ShotstackError, "render failed"):
                    process_local_video(
                        source,
                        Path(temp_dir) / "outputs",
                        paid_api_authorized=True,
                        openai_client=Mock(),
                        shotstack_client=renderer,
                        drive_uploader=drive_uploader,
                        drive_access_token="test-google",
                        config=PipelineConfig(processed_folder_id="processed-folder"),
                        state_db_path=Path(temp_dir) / "processing.sqlite3",
                    )
            self.assertEqual(source.read_bytes(), b"original-video")

        drive_uploader.assert_not_called()
