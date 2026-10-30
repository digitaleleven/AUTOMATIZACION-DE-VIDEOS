from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import render_normalized_4k_once as runner
from video_pipeline.models import VideoInfo, WordTiming
from video_pipeline.state import DuplicateProcessingError, ProcessingLedger


class NormalizedRenderPreflightTests(unittest.TestCase):
    def test_panel_attempt_lookup_is_read_only_and_blocks_existing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "panel.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE dashboard_uploads (
                        id TEXT PRIMARY KEY, original_filename TEXT NOT NULL
                    );
                    CREATE TABLE dashboard_jobs (
                        id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                        status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    INSERT INTO dashboard_uploads VALUES ('source-1', 'IMG_1254.MOV');
                    INSERT INTO dashboard_jobs VALUES ('job-1', 'source-1', 'failed', 'render', 1.0);
                    """
                )

            attempts = runner._existing_panel_attempts(database)
            self.assertEqual(
                attempts,
                [{"job_id": "job-1", "status": "failed", "phase": "render"}],
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM dashboard_jobs WHERE id = 'job-1'"
                    ).fetchone()[0],
                    "failed",
                )

    def test_preflight_checks_exact_hash_and_verified_4k_sdr_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "normalized.mp4"
            source.write_bytes(b"normalized 4k source")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            video = VideoInfo(
                path=str(source),
                duration=59.98,
                width=2160,
                height=3840,
                has_audio=True,
                video_codec="h264",
                audio_codec="aac",
                fps=30,
                size_bytes=source.stat().st_size,
                pixel_format="yuv420p",
                color_primaries="bt709",
                color_transfer="bt709",
                color_space="bt709",
                color_range="tv",
            )
            with (
                patch.object(runner, "probe_video", return_value=video),
                patch.object(
                    runner,
                    "_probe_audio_profile",
                    return_value={"codec_name": "aac", "sample_rate": "48000", "channels": 2},
                ),
                patch.object(runner, "_verify_full_decode") as decode,
            ):
                checked, actual, report = runner._validate_source(source, expected_sha256=expected)
            self.assertEqual(checked, source.resolve())
            self.assertEqual(actual, video)
            self.assertEqual(report["sha256"], expected)
            self.assertTrue(report["full_decode_verified"])
            decode.assert_called_once_with(source.resolve(), video.duration)

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                runner._validate_source(source, expected_sha256="0" * 64)

    def test_preflight_rejects_wrong_dimensions_or_audio_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "normalized.mp4"
            source.write_bytes(b"normalized")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            video = VideoInfo(
                path=str(source), duration=59.98, width=1080, height=1920,
                has_audio=True, video_codec="h264", audio_codec="aac", fps=30,
                pixel_format="yuv420p", color_primaries="bt709",
                color_transfer="bt709", color_space="bt709", color_range="tv",
            )
            with (
                patch.object(runner, "probe_video", return_value=video),
                patch.object(runner, "_probe_audio_profile") as audio,
                patch.object(runner, "_verify_full_decode") as decode,
            ):
                with self.assertRaisesRegex(ValueError, "perfil máster 4K"):
                    runner._validate_source(source, expected_sha256=expected)
            audio.assert_not_called()
            decode.assert_not_called()


class NormalizedRenderExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "IMG_1254-2160x3840-30.mp4"
        self.source.write_bytes(b"verified normalized source")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.video = VideoInfo(
            path=str(self.source),
            duration=59.98,
            width=2160,
            height=3840,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
            fps=30,
            size_bytes=self.source.stat().st_size,
            pixel_format="yuv420p",
            color_primaries="bt709",
            color_transfer="bt709",
            color_space="bt709",
            color_range="tv",
        )
        self.ledger = self.root / "data" / "modal_processing.sqlite3"
        self.output_dir = self.root / "outputs"
        self.work_root = self.root / "data" / "work"
        self.authorizations = {
            "openai": True,
            "gemini": True,
            "modal": True,
            "r2": True,
            "estimated_cost": True,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def _patch_preflight(self):
        return (
            patch.object(
                runner,
                "_validate_source",
                return_value=(
                    self.source.resolve(),
                    self.video,
                    {
                        "sha256": self.digest,
                        "size_bytes": self.source.stat().st_size,
                        "duration_seconds": self.video.duration,
                        "width": 2160,
                        "height": 3840,
                        "fps": 30,
                        "video_codec": "h264",
                        "audio_codec": "aac",
                        "pixel_format": "yuv420p",
                        "color_range": "tv",
                        "color_primaries": "bt709",
                        "color_transfer": "bt709",
                        "color_space": "bt709",
                        "full_decode_verified": True,
                    },
                ),
            ),
            patch.object(
                runner,
                "enforce_job_budget",
                return_value=SimpleNamespace(maximum_usd=1.23),
            ),
            patch.object(
                runner,
                "modal_configuration_status",
                return_value={"sdk_installed": True, "credentials_configured": True},
            ),
            patch.object(runner, "r2_configuration_status", return_value={"ready": True}),
        )

    def test_preflight_only_calls_no_providers_or_renderer(self):
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
            patch.object(runner, "transcribe_video") as transcribe,
            patch.object(runner, "analyze_video") as analyze,
            patch.object(runner, "process_local_video_modal") as render,
        ):
            result = runner.run_normalized_render_once(
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
            )
        self.assertEqual(result["status"], "preflight_ready")
        self.assertTrue(result["normalization_reused"])
        self.assertEqual(result["output_profile"], {"width": 2160, "height": 3840, "fps": 30})
        self.assertEqual(result["external_services_called"], [])
        transcribe.assert_not_called()
        analyze.assert_not_called()
        render.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_preflight_reports_existing_dashboard_jobs_without_mutating_them(self):
        panel_database = self.root / "panel.sqlite3"
        with sqlite3.connect(panel_database) as connection:
            connection.executescript(
                """
                CREATE TABLE dashboard_uploads (
                    id TEXT PRIMARY KEY, original_filename TEXT NOT NULL
                );
                CREATE TABLE dashboard_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                );
                INSERT INTO dashboard_uploads VALUES ('source-1', 'IMG_1254.MOV');
                INSERT INTO dashboard_jobs VALUES ('job-1', 'source-1', 'failed', 'render', 1.0);
                """
            )
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
        ):
            result = runner.run_normalized_render_once(
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=panel_database,
            )
        self.assertEqual(result["status"], "preflight_blocked")
        self.assertEqual(result["existing_panel_attempts"], 1)
        self.assertFalse(self.ledger.exists())
        with sqlite3.connect(panel_database) as connection:
            status = connection.execute(
                "SELECT status FROM dashboard_jobs WHERE id = 'job-1'"
            ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_authorized_execution_reuses_source_and_claims_one_attempt(self):
        output = self.output_dir / "IMG_1254-MATRIX-MOTION-test.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"rendered MP4 placeholder")
        fake_modal = SimpleNamespace(ensure_worker_available=lambda: None)
        analysis = {
            "summary": "summary",
            "suggested_zooms": [{"start_seconds": 5, "end_seconds": 8, "reason": "visual emphasis"}],
            "suggested_cuts": [{"start_seconds": 12, "end_seconds": 13, "reason": "pause candidate"}],
            "copy_and_seo": {"hook": "hook"},
        }
        words = [WordTiming("Hola", 0.0, 0.5), WordTiming("mundo", 0.5, 0.9)]
        plan = {"cuts": [], "zooms": [], "stickers": []}

        def fake_process(input_path, output_dir, **kwargs):
            self.assertEqual(Path(input_path), self.source.resolve())
            self.assertEqual(kwargs["subtitle_mode"], "dynamic")
            self.assertEqual((kwargs["style"].output_width, kwargs["style"].output_height), (2160, 3840))
            self.assertEqual(kwargs["style"].output_fps, 30)
            self.assertEqual(kwargs["style"].highlight_color, "#FFC0CB")
            self.assertEqual(kwargs["subtitle_words"], words)
            self.assertEqual(kwargs["render_mode"], "full")
            return {"output": str(output), "modal_output_sha256": "a" * 64}

        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
            patch.object(runner, "ModalRenderClient", return_value=fake_modal),
            patch.object(runner, "transcribe_video", return_value=words) as transcribe,
            patch.object(runner, "detect_silence_cut_suggestions", return_value=[]),
            patch.object(runner, "analyze_video", return_value=analysis),
            patch.object(
                runner,
                "_prepare_modal_render_plan",
                return_value=(plan, [], [], [], []),
            ),
            patch.object(runner, "process_local_video_modal", side_effect=fake_process) as render,
            patch.object(
                runner,
                "upload_mp4_to_r2",
                return_value={"object_key": "renders/test/video.mp4", "verified": True},
            ),
        ):
            result = runner.run_normalized_render_once(
                execute=True,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
                authorizations=self.authorizations,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"]["r2_storage"]["verified"], True)
            transcribe.assert_called_once()
            render.assert_called_once()
            ledger_key = hashlib.sha256(f"{self.digest}:full".encode()).hexdigest()
            record = ProcessingLedger(self.ledger).get(ledger_key)
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["attempts"], 1)

            with self.assertRaises(DuplicateProcessingError):
                runner.run_normalized_render_once(
                    execute=True,
                    source_path=self.source,
                    expected_sha256=self.digest,
                    ledger_path=self.ledger,
                    panel_database_path=self.root / "panel.sqlite3",
                    output_dir=self.output_dir,
                    work_root=self.work_root,
                    authorizations=self.authorizations,
                )
            transcribe.assert_called_once()
            render.assert_called_once()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def test_missing_authorization_stops_before_modal_or_provider_calls(self):
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
            patch.object(runner, "ModalRenderClient") as modal_client,
            patch.object(runner, "transcribe_video") as transcribe,
        ):
            with self.assertRaisesRegex(PermissionError, "authorize_openai"):
                runner.run_normalized_render_once(
                    execute=True,
                    source_path=self.source,
                    expected_sha256=self.digest,
                    ledger_path=self.ledger,
                    panel_database_path=self.root / "panel.sqlite3",
                    authorizations={},
                )
        modal_client.assert_not_called()
        transcribe.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_prior_panel_job_is_reported_as_preflight_blocker(self):
        panel_database = self.root / "panel.sqlite3"
        with sqlite3.connect(panel_database) as connection:
            connection.executescript(
                """
                CREATE TABLE dashboard_uploads (
                    id TEXT PRIMARY KEY, original_filename TEXT NOT NULL
                );
                CREATE TABLE dashboard_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                );
                INSERT INTO dashboard_uploads VALUES ('source-1', 'IMG_1254.MOV');
                INSERT INTO dashboard_jobs VALUES ('job-1', 'source-1', 'failed', 'render', 1.0);
                """
            )
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
        ):
            result = runner.run_normalized_render_once(
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=panel_database,
            )
        self.assertEqual(result["status"], "preflight_blocked")
        self.assertEqual(result["existing_panel_attempts"], 1)
        self.assertTrue(any("historial previo" in item for item in result["preflight_blockers"]))
        self.assertFalse(self.ledger.exists())

    def test_budget_over_limit_blocks_before_any_service(self):
        patches = self._patch_preflight()
        with (
            patches[0],
            patch.object(runner, "enforce_job_budget", side_effect=RuntimeError("over budget")),
            patch.object(runner, "ModalRenderClient") as modal_client,
            patch.object(runner, "transcribe_video") as transcribe,
        ):
            with self.assertRaisesRegex(RuntimeError, "over budget"):
                runner.run_normalized_render_once(
                    execute=True,
                    source_path=self.source,
                    expected_sha256=self.digest,
                    ledger_path=self.ledger,
                    panel_database_path=self.root / "panel.sqlite3",
                    authorizations=self.authorizations,
                )
        modal_client.assert_not_called()
        transcribe.assert_not_called()
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
