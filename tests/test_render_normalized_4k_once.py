from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts import render_normalized_4k_once as runner
from video_pipeline.models import VideoInfo, WordTiming
from video_pipeline.state import DuplicateProcessingError, ProcessingLedger


class NormalizedRenderPreflightTests(unittest.TestCase):
    def test_dotenv_loads_silently_and_does_not_override_service_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            dotenv = Path(temporary) / ".env"
            dotenv.write_text(
                "MATRIX_TEST_DOTENV_VALUE=loaded-from-file\n"
                "MATRIX_TEST_DOTENV_PRECEDENCE=file-value\n",
                encoding="utf-8",
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(
                os.environ,
                {"MATRIX_TEST_DOTENV_PRECEDENCE": "service-value"},
                clear=False,
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                loaded = runner.load_project_environment(dotenv)
                self.assertTrue(loaded)
                self.assertEqual(os.environ["MATRIX_TEST_DOTENV_VALUE"], "loaded-from-file")
                self.assertEqual(
                    os.environ["MATRIX_TEST_DOTENV_PRECEDENCE"],
                    "service-value",
                )
            self.assertNotIn("loaded-from-file", stdout.getvalue() + stderr.getvalue())

    def test_panel_attempt_lookup_is_read_only_and_blocks_existing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "panel.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE dashboard_uploads (
                        id TEXT PRIMARY KEY, original_filename TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL
                    );
                    CREATE TABLE dashboard_jobs (
                        id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                        status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                    );
                    INSERT INTO dashboard_uploads VALUES ('source-1', 'IMG_1254.MOV', 'a');
                    INSERT INTO dashboard_jobs VALUES ('job-1', 'source-1', 'failed', 'render', 1.0);
                    """
                )

            attempts = runner._existing_panel_attempts(database)
            self.assertEqual(
                attempts,
                [{
                    "job_id": "job-1",
                    "status": "failed",
                    "phase": "render",
                    "source_sha256": "a",
                }],
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM dashboard_jobs WHERE id = 'job-1'"
                    ).fetchone()[0],
                    "failed",
                )

    def test_active_or_uncertain_history_blocks_a_new_attempt(self):
        normalized_key = "new-normalized-key"
        panel = [
            {
                "job_id": "active-job",
                "status": "processing",
                "phase": "rendering",
                "source_sha256": "a" * 64,
            }
        ]
        blockers, failed = runner._prior_attempt_blockers(
            panel,
            [],
            normalized_source_key=normalized_key,
        )
        self.assertIn("panel_job_processing", blockers)
        self.assertEqual(failed, 0)

        blockers, failed = runner._prior_attempt_blockers(
            [],
            [{"source_sha256": "old-key", "status": "submission_unknown", "attempts": 1}],
            normalized_source_key=normalized_key,
        )
        self.assertIn("prior_modal_ledger_submission_unknown", blockers)
        self.assertEqual(failed, 0)

    def test_retry_gate_allows_only_one_failed_normalized_attempt(self):
        key = "normalized-key"
        failed_attempt = [{"source_sha256": key, "status": "failed", "attempts": 1}]
        blockers, _ = runner._prior_attempt_blockers(
            [], failed_attempt, normalized_source_key=key
        )
        self.assertIn("normalized_source_already_claimed", blockers)

        blockers, _ = runner._prior_attempt_blockers(
            [], failed_attempt, normalized_source_key=key, retry_failed=True
        )
        self.assertEqual(blockers, [])

        for status, attempts in (("processing", 1), ("completed", 1), ("failed", 2)):
            blockers, _ = runner._prior_attempt_blockers(
                [],
                [{"source_sha256": key, "status": status, "attempts": attempts}],
                normalized_source_key=key,
                retry_failed=True,
            )
            self.assertTrue(blockers, (status, attempts))

        blockers, _ = runner._prior_attempt_blockers(
            [], [], normalized_source_key=key, retry_failed=True
        )
        self.assertIn("retry_failed_requires_single_failed_ledger_attempt", blockers)

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

    def _create_failed_gemini_checkpoint(self):
        job_id = runner.AUTHORIZED_GEMINI_RESUME_JOB_ID
        ledger_key = hashlib.sha256(f"{self.digest}:full".encode()).hexdigest()
        ledger = ProcessingLedger(self.ledger)
        ledger.claim(
            ledger_key,
            str(self.source),
            max_attempts=1,
            edit_options=runner.EDIT_OPTIONS,
        )
        ledger.mark_failed(ledger_key, "AuthenticationError: first attempt failed")
        ledger.claim(
            ledger_key,
            str(self.source),
            retry_failed=True,
            max_attempts=2,
            preserve_failure_history=True,
            edit_options=runner.EDIT_OPTIONS,
        )
        ledger.mark_failed(ledger_key, "GeminiAnalysisError: ejecución detenida")

        job_work = self.work_root / job_id
        job_work.mkdir(parents=True)
        transcript = {
            "text": "Hola mundo",
            "words": [
                {"word": "Hola", "start": 0.0, "end": 0.4},
                {"word": "mundo", "start": 0.4, "end": 0.9},
            ],
        }
        transcript_path = job_work / "transcript.json"
        transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
        events_path = job_work / "events.jsonl"
        for event in (
            {"phase": "claimed", "source_sha256": self.digest, "output_profile": "2160p30"},
            {"phase": "transcribing"},
            {
                "phase": "transcript_persisted",
                "transcript_sha256": hashlib.sha256(transcript_path.read_bytes()).hexdigest(),
            },
            {"phase": "detecting_silences"},
            {"phase": "analyzing_with_gemini"},
            {
                "phase": "failed",
                "error_type": "GeminiAnalysisError",
                "failure_phase": "analyzing_with_gemini",
            },
        ):
            runner._append_event(events_path, event)
        return job_id, ledger_key, job_work

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

    def test_gemini_resume_preflight_reuses_checkpoint_and_excludes_whisper_cost(self):
        job_id, _, _ = self._create_failed_gemini_checkpoint()
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-only"}, clear=True),
            patch.object(runner, "transcribe_video") as transcribe,
            patch.object(runner, "analyze_video") as analyze,
            patch.object(runner, "process_local_video_modal") as render,
        ):
            result = runner.run_normalized_render_once(
                resume_job_id=job_id,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
            )
        self.assertEqual(result["status"], "preflight_ready")
        self.assertTrue(result["transcription_reused"])
        self.assertEqual(result["resume_job_id"], job_id)
        self.assertEqual(result["pending_operations"], ["gemini_analysis", "modal_render", "r2_upload"])
        self.assertEqual(result["pending_cost_breakdown_usd"]["whisper_usd"], 0.0)
        self.assertEqual(result["pending_cost_breakdown_usd"]["normalization_usd"], 0.0)
        self.assertFalse(result["silence_candidates_reused"])
        self.assertEqual(result["preflight_blockers"], [])
        transcribe.assert_not_called()
        analyze.assert_not_called()
        render.assert_not_called()

    def test_gemini_checkpoint_resume_skips_whisper_and_uses_one_atomic_recovery(self):
        job_id, ledger_key, job_work = self._create_failed_gemini_checkpoint()
        output = self.output_dir / "recovered.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"MP4 placeholder")
        fake_modal = SimpleNamespace(ensure_worker_available=lambda: None)
        analysis = {
            "summary": "summary",
            "suggested_zooms": [],
            "suggested_cuts": [],
            "copy_and_seo": {"hook": "hook"},
        }
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-only"}, clear=True),
            patch.object(runner, "ModalRenderClient", return_value=fake_modal) as modal_client,
            patch.object(runner, "transcribe_video") as transcribe,
            patch.object(runner, "detect_silence_cut_suggestions") as silence_detection,
            patch.object(runner, "analyze_video", return_value=analysis) as analyze,
            patch.object(runner, "_prepare_modal_render_plan", return_value=({}, [], [], [], [])),
            patch.object(
                runner,
                "process_local_video_modal",
                return_value={"output": str(output), "modal_output_sha256": "f" * 64},
            ) as render,
            patch.object(
                runner,
                "upload_mp4_to_r2",
                return_value={"object_key": "renders/recovered.mp4", "verified": True},
            ),
        ):
            result = runner.run_normalized_render_once(
                execute=True,
                resume_job_id=job_id,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
                authorizations={
                    "gemini": True,
                    "modal": True,
                    "r2": True,
                    "estimated_cost": True,
                },
            )
            self.assertEqual(result["status"], "completed")
            transcribe.assert_not_called()
            silence_detection.assert_not_called()
            analyze.assert_called_once()
            render.assert_called_once()
            modal_client.assert_called_once()

        record = ProcessingLedger(self.ledger).get(ledger_key)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["attempts"], 2)
        with sqlite3.connect(self.ledger) as connection:
            snapshots = connection.execute(
                """SELECT attempt_number, status, error
                   FROM processing_job_attempt_history
                   WHERE source_sha256 = ? ORDER BY attempt_number""",
                (ledger_key,),
            ).fetchall()
        self.assertEqual([row[0] for row in snapshots], [1, 2])
        self.assertEqual([row[1] for row in snapshots], ["failed", "failed"])
        self.assertIn("first attempt failed", snapshots[0][2])
        self.assertIn("GeminiAnalysisError", snapshots[1][2])
        appended_phases = [
            json.loads(line)["phase"]
            for line in (job_work / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("resumed_from_checkpoint", appended_phases)
        self.assertIn("cuts_disabled_no_persisted_silence_candidates", appended_phases)
        self.assertNotIn("transcribing", appended_phases[appended_phases.index("resumed_from_checkpoint"):])

        with patch.object(runner, "_validate_source", return_value=(self.source, self.video, {"sha256": self.digest})):
            blocked = runner.run_normalized_render_once(
                resume_job_id=job_id,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
            )
        self.assertEqual(blocked["status"], "preflight_blocked")
        self.assertTrue(any("normalized_source_completed" in item for item in blocked["preflight_blockers"]))

    def test_gemini_error_diagnostic_is_persisted_without_message_or_secret(self):
        job_id, ledger_key, job_work = self._create_failed_gemini_checkpoint()
        diagnostic_error = runner.GeminiAnalysisError(
            "provider error must not be persisted verbatim",
            category="quota_or_rate_limit",
            phase="generate_content",
            http_status=429,
            provider_status="RESOURCE_EXHAUSTED",
        )
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-only"}, clear=True),
            patch.object(runner, "ModalRenderClient", return_value=SimpleNamespace(ensure_worker_available=lambda: None)),
            patch.object(runner, "analyze_video", side_effect=diagnostic_error),
            patch.object(runner, "transcribe_video") as transcribe,
            patch.object(runner, "process_local_video_modal") as render,
            self.assertRaises(runner.GeminiAnalysisError),
        ):
            runner.run_normalized_render_once(
                execute=True,
                resume_job_id=job_id,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
                authorizations={
                    "gemini": True,
                    "modal": True,
                    "r2": True,
                    "estimated_cost": True,
                },
            )
        transcribe.assert_not_called()
        render.assert_not_called()
        ledger_record = ProcessingLedger(self.ledger).get(ledger_key)
        self.assertEqual(ledger_record["status"], "failed")
        self.assertNotIn("provider error must not be persisted", ledger_record["error"])
        self.assertNotIn("test-only", ledger_record["error"])
        events = [
            json.loads(line)
            for line in (job_work / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        failed = events[-1]
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["failure_phase"], "analyzing_with_gemini")
        self.assertEqual(failed["category"], "quota_or_rate_limit")
        self.assertEqual(failed["http_status"], 429)
        self.assertNotIn("provider error must not be persisted", json.dumps(failed))
        self.assertNotIn("test-only", json.dumps(events))
        with patch.object(
            runner,
            "_validate_source",
            return_value=(self.source, self.video, {"sha256": self.digest}),
        ):
            blocked = runner.run_normalized_render_once(
                resume_job_id=job_id,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
            )
        self.assertEqual(blocked["status"], "preflight_blocked")
        self.assertIn("resume_already_claimed", blocked["preflight_blockers"])

    def test_preflight_reports_existing_dashboard_jobs_without_mutating_them(self):
        panel_database = self.root / "panel.sqlite3"
        with sqlite3.connect(panel_database) as connection:
            connection.executescript(
                """
                CREATE TABLE dashboard_uploads (
                        id TEXT PRIMARY KEY, original_filename TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL
                );
                CREATE TABLE dashboard_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO dashboard_uploads VALUES (?, ?, ?)",
                ("source-1", "IMG_1254.MOV", "a" * 64),
            )
            connection.execute(
                "INSERT INTO dashboard_jobs VALUES (?, ?, ?, ?, ?)",
                ("job-1", "source-1", "failed", "render", 1.0),
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
        self.assertEqual(result["status"], "preflight_ready")
        self.assertEqual(result["existing_panel_attempts"], 1)
        self.assertEqual(result["previous_failed_attempts"], 1)
        self.assertFalse(self.ledger.exists())
        with sqlite3.connect(panel_database) as connection:
            status = connection.execute(
                "SELECT status FROM dashboard_jobs WHERE id = 'job-1'"
            ).fetchone()[0]
        self.assertEqual(status, "failed")

    def test_three_failed_history_rows_allow_one_new_preflight_attempt(self):
        panel_database = self.root / "panel.sqlite3"
        ledger_database = self.root / "modal_processing.sqlite3"
        source_hashes = ["a" * 64, "b" * 64, "c" * 64]
        with sqlite3.connect(panel_database) as connection:
            connection.executescript(
                """
                CREATE TABLE dashboard_uploads (
                    id TEXT PRIMARY KEY, original_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL
                );
                CREATE TABLE dashboard_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )
            for index, source_hash in enumerate(source_hashes):
                connection.execute(
                    "INSERT INTO dashboard_uploads VALUES (?, ?, ?)",
                    (f"source-{index}", "IMG_1254.MOV", source_hash),
                )
                connection.execute(
                    "INSERT INTO dashboard_jobs VALUES (?, ?, 'failed', 'failed', ?)",
                    (f"job-{index}", f"source-{index}", float(index)),
                )
        with sqlite3.connect(ledger_database) as connection:
            connection.execute(
                """CREATE TABLE processing_jobs (
                    source_sha256 TEXT PRIMARY KEY, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL
                )"""
            )
            for source_hash in source_hashes:
                ledger_key = hashlib.sha256(f"{source_hash}:full".encode()).hexdigest()
                connection.execute(
                    "INSERT INTO processing_jobs VALUES (?, 'failed', 1)",
                    (ledger_key,),
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
                ledger_path=ledger_database,
                panel_database_path=panel_database,
            )
        self.assertEqual(result["status"], "preflight_ready")
        self.assertEqual(result["previous_failed_attempts"], 3)
        self.assertEqual(result["preflight_blockers"], [])
        with sqlite3.connect(panel_database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM dashboard_jobs WHERE status='failed'"
                ).fetchone()[0],
                3,
            )

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
        old_source_hashes = ["a" * 64, "b" * 64, "c" * 64]
        old_panel_attempts = [
            {
                "job_id": f"old-job-{index}",
                "status": "failed",
                "phase": "failed",
                "source_sha256": source_hash,
            }
            for index, source_hash in enumerate(old_source_hashes)
        ]
        old_ledger_attempts = [
            {
                "source_sha256": hashlib.sha256(f"{source_hash}:full".encode()).hexdigest(),
                "status": "failed",
                "attempts": 1,
            }
            for source_hash in old_source_hashes
        ]
        preflight_reports = []

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
            patch.object(
                runner,
                "_existing_panel_attempts",
                return_value=old_panel_attempts,
            ),
            patch.object(
                runner,
                "_existing_modal_ledger_attempts",
                return_value=old_ledger_attempts,
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
                preflight_callback=preflight_reports.append,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["result"]["r2_storage"]["verified"], True)
            self.assertEqual(preflight_reports[0]["previous_failed_attempts"], 3)
            self.assertEqual(preflight_reports[0]["status"], "preflight_ready")
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

    def test_explicit_retry_preserves_failed_attempt_and_blocks_a_second_retry(self):
        ledger_key = hashlib.sha256(f"{self.digest}:full".encode()).hexdigest()
        ledger = ProcessingLedger(self.ledger)
        ledger.claim(
            ledger_key,
            str(self.source),
            max_attempts=1,
            edit_options=runner.EDIT_OPTIONS,
        )
        ledger.mark_failed(ledger_key, "AuthenticationError: invalid_api_key")
        failure_before_retry = ledger.get(ledger_key)

        output = self.output_dir / "retry-render.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"rendered MP4 placeholder")
        fake_modal = SimpleNamespace(ensure_worker_available=lambda: None)
        analysis = {
            "summary": "summary",
            "suggested_zooms": [],
            "suggested_cuts": [],
            "copy_and_seo": {"hook": "hook"},
        }
        words = [WordTiming("Hola", 0.0, 0.5), WordTiming("mundo", 0.5, 0.9)]
        patches = self._patch_preflight()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.dict("os.environ", {"OPENAI_API_KEY": "test", "GEMINI_API_KEY": "test"}),
            patch.object(runner, "ModalRenderClient", return_value=fake_modal),
            patch.object(runner, "transcribe_video", return_value=words),
            patch.object(runner, "detect_silence_cut_suggestions", return_value=[]),
            patch.object(runner, "analyze_video", return_value=analysis),
            patch.object(runner, "_prepare_modal_render_plan", return_value=({}, [], [], [], [])),
            patch.object(
                runner,
                "process_local_video_modal",
                return_value={"output": str(output), "modal_output_sha256": "f" * 64},
            ) as render,
            patch.object(
                runner, "upload_mp4_to_r2",
                return_value={"object_key": "renders/retry.mp4", "verified": True},
            ),
        ):
            result = runner.run_normalized_render_once(
                execute=True,
                retry_failed=True,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
                authorizations=self.authorizations,
            )
            self.assertEqual(result["status"], "completed")
            render.assert_called_once()

        current = ProcessingLedger(self.ledger).get(ledger_key)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["attempts"], 2)
        with sqlite3.connect(self.ledger) as connection:
            row = connection.execute(
                """SELECT status, error, snapshot_json
                   FROM processing_job_attempt_history
                   WHERE source_sha256 = ? AND attempt_number = 1""",
                (ledger_key,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "failed")
        self.assertEqual(row[1], failure_before_retry["error"])
        self.assertEqual(json.loads(row[2])["error"], "AuthenticationError: invalid_api_key")

        with (
            patch.object(
                runner,
                "_validate_source",
                return_value=(self.source.resolve(), self.video, {"sha256": self.digest}),
            ),
            self.assertRaisesRegex(RuntimeError, "Preflight bloqueado"),
        ):
            runner.run_normalized_render_once(
                execute=True,
                retry_failed=True,
                source_path=self.source,
                expected_sha256=self.digest,
                ledger_path=self.ledger,
                panel_database_path=self.root / "panel.sqlite3",
                output_dir=self.output_dir,
                work_root=self.work_root,
                authorizations=self.authorizations,
            )
        render.assert_called_once()

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
                    id TEXT PRIMARY KEY, original_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL
                );
                CREATE TABLE dashboard_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO dashboard_uploads VALUES (?, ?, ?)",
                ("source-1", "IMG_1254.MOV", "a" * 64),
            )
            connection.execute(
                "INSERT INTO dashboard_jobs VALUES (?, ?, ?, ?, ?)",
                ("job-1", "source-1", "completed", "completed", 1.0),
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
        self.assertTrue(any("panel_job_completed" in item for item in result["preflight_blockers"]))
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
