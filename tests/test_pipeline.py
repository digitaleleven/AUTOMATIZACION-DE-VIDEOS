import tempfile
import unittest
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

from video_pipeline.config import PipelineConfig
from video_pipeline.media import extract_audio_chunks, probe_video
from video_pipeline.models import SubtitleSegment, VideoInfo, WordTiming
from video_pipeline.pipeline import (
    _new_output_path,
    build_dry_run_plan,
    find_local_mp4s,
    process_local_video,
)
from video_pipeline.shotstack import ShotstackClient, ShotstackError, build_edit_payload
from video_pipeline.subtitles import (
    TranscriptionError,
    build_subtitle_segments,
    normalize_transcription,
)
from video_pipeline.transcription import transcribe_video


class PipelineDryRunTests(unittest.TestCase):
    def test_finds_mp4_case_insensitively_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "clip.MP4"
            note = root / "notes.txt"
            video.write_bytes(b"test fixture, not a real video")
            note.write_text("leave unchanged", encoding="utf-8")

            before = (video.read_bytes(), note.read_bytes())
            found = find_local_mp4s(root)

            self.assertEqual([item.name for item in found], ["clip.MP4"])
            self.assertEqual((video.read_bytes(), note.read_bytes()), before)

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


class ShotstackPayloadTests(unittest.TestCase):
    def test_builds_vertical_video_with_visible_timed_subtitles(self):
        captions = [
            SubtitleSegment("Hola mundo", 0.2, 1.1),
            SubtitleSegment("Prueba vertical", 1.1, 2.5),
        ]
        payload = build_edit_payload("https://media.example/video.mp4", 2.5, captions)
        self.assertEqual(payload["output"]["size"], {"width": 1080, "height": 1920})
        tracks = payload["timeline"]["tracks"]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(len(tracks[0]["clips"]), 2)
        self.assertEqual(tracks[0]["clips"][0]["asset"]["text"], "Hola mundo")
        self.assertEqual(tracks[0]["clips"][0]["position"], "bottom")
        self.assertEqual(tracks[1]["clips"][0]["asset"]["fit"], "crop")

    def test_rejects_non_https_video_source(self):
        with self.assertRaises(ValueError):
            build_edit_payload("file:///tmp/video.mp4", 5, [SubtitleSegment("Hola mundo", 0, 1)])

    def test_rejects_subtitle_outside_video_duration(self):
        with self.assertRaises(ValueError):
            build_edit_payload(
                "https://media.example/video.mp4",
                1,
                [SubtitleSegment("Hola mundo", 2, 3)],
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
                    "attributes": {"url": "https://shotstack-ingest-api-v1-sources.s3.amazonaws.com/signed"},
                }
            }
            session.get.return_value.json.return_value = {
                "data": {
                    "id": "source-id",
                    "attributes": {
                        "status": "ready",
                        "source": "https://shotstack-ingest-api-v1-sources.s3.amazonaws.com/source.mp4",
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

        self.assertEqual(url, "https://shotstack-ingest-api-v1-sources.s3.amazonaws.com/source.mp4")
        self.assertNotIn("x-api-key", session.put.call_args.kwargs["headers"])

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


if __name__ == "__main__":
    unittest.main()

