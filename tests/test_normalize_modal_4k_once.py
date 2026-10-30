import json
import contextlib
import io
import hashlib
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.normalize_modal_4k_once import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    NormalizationInterrupted,
    append_history,
    build_parser,
    claim_normalization_attempt,
    main,
    output_capacity_preflight,
    record_termination_signals,
    recover_interrupted_attempt,
    record_terminal_outcome,
    verify_third_attempt_authorization,
)
from video_pipeline.modal_execution_store import ModalExecutionStore
from video_pipeline.modal_renderer import (
    ModalNormalizationSubmission,
    ModalSubmissionUnknownError,
)
from video_pipeline.state import ProcessingLedger, RetryNotAllowedError


class NormalizeModal4KRetryTests(unittest.TestCase):
    def test_termination_handler_tracks_ssh_sighup(self):
        registrations = []
        with (
            patch("scripts.normalize_modal_4k_once.signal.getsignal", return_value=None),
            patch(
                "scripts.normalize_modal_4k_once.signal.signal",
                side_effect=lambda signum, _handler: registrations.append(signum),
            ),
        ):
            with record_termination_signals():
                pass
        self.assertIn(signal.SIGTERM, registrations)
        self.assertIn(signal.SIGINT, registrations)
        self.assertIn(signal.SIGHUP, registrations)

    def test_cli_exposes_manual_retry_flag_only(self):
        self.assertFalse(build_parser().parse_args([]).retry_failed)
        self.assertTrue(build_parser().parse_args(["--retry-failed"]).retry_failed)
        self.assertTrue(build_parser().parse_args(["--recover-only"]).recover_only)
        self.assertTrue(build_parser().parse_args(["--allow-third-attempt"]).allow_third_attempt)
        self.assertTrue(build_parser().parse_args(["--preflight-only"]).preflight_only)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--retry-failed", "--recover-only"])

    def test_main_persists_detached_call_id_and_original_without_waiting_for_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.MOV"
            output = root / "output.mp4"
            source.write_bytes(b"immutable source fixture")
            store_path = root / "modal-executions.sqlite3"
            submitted = []

            class FakeClient:
                def recover_cached_normalization(self, **_kwargs):
                    return None

                def submit_normalization_detached(self, **kwargs):
                    kwargs["on_phase"]("uploading_fixture")
                    kwargs["on_submitted"]("fc-simulated-id")
                    self.expected_worker_code_version = kwargs["expected_worker_code_version"]
                    submitted.append(kwargs["job_id"])
                    return ModalNormalizationSubmission(
                        job_id=kwargs["job_id"],
                        function_call_id="fc-simulated-id",
                        source_sha256="",
                        profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                        submitted_at=time.time(),
                    )

            fake_client = FakeClient()
            profile = SimpleNamespace(
                width=2160, height=3840, duration=59.98, rotation=0,
                video_codec="hevc",
                color_range="tv", color_primaries="bt2020",
                color_transfer="arib-std-b67", color_space="bt2020nc",
            )
            with (
                patch("scripts.normalize_modal_4k_once.probe_video", return_value=profile),
                patch(
                    "scripts.normalize_modal_4k_once.output_capacity_preflight",
                    return_value={
                        "checked_directory": str(root),
                        "available_bytes": 16 * 1024**3,
                        "required_bytes": 9 * 1024**3,
                    },
                ),
                patch(
                    "scripts.normalize_modal_4k_once.enforce_4k_normalization_budget",
                    return_value=SimpleNamespace(maximum_usd=1.3),
                ),
                patch("scripts.normalize_modal_4k_once.LEDGER_PATH", root / "legacy.sqlite3"),
                patch("scripts.normalize_modal_4k_once.HISTORY_DIR", root / "history"),
                patch("scripts.normalize_modal_4k_once.ModalRenderClient", return_value=fake_client),
            ):
                arguments = [
                    "--source", str(source),
                    "--output", str(output),
                    "--execution-store", str(store_path),
                ]
                code = main(arguments)
                with self.assertRaises(SystemExit):
                    main(arguments)

            self.assertEqual(code, 0)
            self.assertEqual(source.read_bytes(), b"immutable source fixture")
            self.assertFalse(output.exists())
            self.assertEqual(len(submitted), 1)
            self.assertIsNone(fake_client.expected_worker_code_version)
            store = ModalExecutionStore(store_path)
            record = store.latest_for_source(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                MASTER_4K_NORMALIZATION_PROFILE_ID,
            )
            self.assertEqual(record.status, "submitted")
            self.assertEqual(record.function_call_id, "fc-simulated-id")
            self.assertEqual(ProcessingLedger(root / "legacy.sqlite3").get(record.source_sha256)["attempts"], 1)

    def test_uncertain_submission_stores_call_id_and_blocks_automatic_resubmission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.MOV"
            output = root / "output.mp4"
            source.write_bytes(b"immutable source fixture")
            store_path = root / "modal-executions.sqlite3"

            class FakeClient:
                def recover_cached_normalization(self, **_kwargs):
                    return None

                def submit_normalization_detached(self, **kwargs):
                    kwargs["on_submitted"]("fc-uncertain-id")
                    raise ModalSubmissionUnknownError(
                        "lost reply", function_call_id="fc-uncertain-id"
                    )

            profile = SimpleNamespace(
                width=2160, height=3840, duration=59.98, rotation=0,
                video_codec="hevc",
                color_range="tv", color_primaries="bt2020",
                color_transfer="arib-std-b67", color_space="bt2020nc",
            )
            with (
                patch("scripts.normalize_modal_4k_once.probe_video", return_value=profile),
                patch(
                    "scripts.normalize_modal_4k_once.output_capacity_preflight",
                    return_value={
                        "checked_directory": str(root),
                        "available_bytes": 16 * 1024**3,
                        "required_bytes": 9 * 1024**3,
                    },
                ),
                patch(
                    "scripts.normalize_modal_4k_once.enforce_4k_normalization_budget",
                    return_value=SimpleNamespace(maximum_usd=1.3),
                ),
                patch("scripts.normalize_modal_4k_once.LEDGER_PATH", root / "legacy.sqlite3"),
                patch("scripts.normalize_modal_4k_once.HISTORY_DIR", root / "history"),
                patch("scripts.normalize_modal_4k_once.ModalRenderClient", return_value=FakeClient()),
            ):
                self.assertEqual(
                    main([
                        "--source", str(source),
                        "--output", str(output),
                        "--execution-store", str(store_path),
                    ]),
                    1,
                )

            record = ModalExecutionStore(store_path).latest_for_source(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                MASTER_4K_NORMALIZATION_PROFILE_ID,
            )
            self.assertEqual(record.status, "submission_unknown")
            self.assertEqual(record.function_call_id, "fc-uncertain-id")
            self.assertEqual(ProcessingLedger(root / "legacy.sqlite3").get(record.source_sha256)["attempts"], 1)
            self.assertEqual(source.read_bytes(), b"immutable source fixture")
            self.assertFalse(output.exists())

    def test_one_manual_retry_preserves_failure_and_blocks_a_third_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = ProcessingLedger(root / "state.sqlite3")
            history = root / "history" / "source.jsonl"
            source_hash = "a" * 64
            source_path = root / "IMG_1254.MOV"
            source_path.write_bytes(b"source fixture")

            first = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=False,
                history_path=history,
            )
            self.assertEqual((first["attempts"], first["status"]), (1, "processing"))
            ledger.mark_failed(source_hash, "first conversion failed")
            append_history(history, {
                "attempt": 1,
                "status": "failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "error": "first conversion failed",
            })

            second = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=True,
                history_path=history,
            )
            self.assertEqual((second["attempts"], second["status"]), (2, "processing"))
            ledger.mark_failed(source_hash, "manual retry failed")
            append_history(history, {
                "attempt": 2,
                "status": "failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "error": "manual retry failed",
            })

            with self.assertRaises(RetryNotAllowedError):
                claim_normalization_attempt(
                    ledger,
                    source_hash=source_hash,
                    source_path=source_path,
                    retry_failed=True,
                    history_path=history,
                )

            events = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertEqual(
                [(event["attempt"], event["status"]) for event in events],
                [(1, "started"), (1, "failed"), (2, "started"), (2, "failed")],
            )
            self.assertEqual(events[1]["error"], "first conversion failed")
            self.assertEqual(ledger.get(source_hash)["attempts"], 2)

    def test_retry_requires_an_existing_failed_first_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = ProcessingLedger(root / "state.sqlite3")
            with self.assertRaisesRegex(
                RetryNotAllowedError,
                "solo permite un segundo intento",
            ):
                claim_normalization_attempt(
                    ledger,
                    source_hash="b" * 64,
                    source_path=root / "IMG_1254.MOV",
                    retry_failed=True,
                    history_path=root / "history.jsonl",
                )
            self.assertEqual(ledger.get("b" * 64), {})

    def test_cancellation_records_interrupted_state_without_resetting_attempt_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = ProcessingLedger(root / "state.sqlite3")
            history = root / "history.jsonl"
            source_hash = "c" * 64
            source_path = root / "source.MOV"
            source_path.write_bytes(b"source")
            first = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=False,
                history_path=history,
            )
            ledger.mark_failed(source_hash, "first attempt failed")
            append_history(history, {
                "attempt": 1,
                "status": "failed",
                "source_sha256": source_hash,
                "error": "first attempt failed",
            })
            second = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=True,
                history_path=history,
            )

            event = record_terminal_outcome(
                ledger,
                source_hash=source_hash,
                history_path=history,
                attempt_number=int(second["attempts"]),
                job_id="d" * 32,
                phase="normalizing_on_modal",
                started=time.monotonic() - 2,
                exc=NormalizationInterrupted(signal.SIGTERM),
            )

            current = ledger.get(source_hash)
            self.assertEqual(current["status"], "interrupted")
            self.assertEqual(current["attempts"], 2)
            self.assertEqual(event["status"], "interrupted")
            self.assertEqual(event["phase"], "normalizing_on_modal")
            self.assertGreaterEqual(event["total_seconds"], 2)
            self.assertEqual(first["attempts"], 1)
            events = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertEqual(events[1]["error"], "first attempt failed")
            self.assertEqual(events[-1]["status"], "interrupted")
            with self.assertRaises(RetryNotAllowedError):
                claim_normalization_attempt(
                    ledger,
                    source_hash=source_hash,
                    source_path=source_path,
                    retry_failed=True,
                    history_path=history,
                )
            self.assertEqual(ledger.get(source_hash)["attempts"], 2)

    def test_recovery_check_does_not_claim_or_increment_an_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = ProcessingLedger(root / "state.sqlite3")
            history = root / "history.jsonl"
            source_hash = "e" * 64
            source_path = root / "source.MOV"
            source_path.write_bytes(b"source")
            first = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=False,
                history_path=history,
            )
            ledger.mark_failed(source_hash, "first attempt failed")
            append_history(history, {
                "attempt": 1,
                "status": "failed",
                "source_sha256": source_hash,
                "error": "first attempt failed",
            })
            second = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=True,
                history_path=history,
            )
            ledger.update(source_hash, status="interrupted", error="Modal call cancelled")

            class ReadOnlyRecoveryClient:
                calls = []

                def recover_cached_normalization(self, **_kwargs):
                    self.calls.append("cache_read_only")
                    return None

            client = ReadOnlyRecoveryClient()
            result = recover_interrupted_attempt(
                ledger,
                client,
                source_hash=source_hash,
                source_path=source_path,
                output_path=root / "output.mp4",
                history_path=history,
            )

            current = ledger.get(source_hash)
            self.assertIsNone(result)
            self.assertEqual(client.calls, ["cache_read_only"])
            self.assertEqual(current["attempts"], 2)
            self.assertEqual(current["status"], "interrupted")
            self.assertEqual(second["attempts"], 2)
            self.assertEqual(first["attempts"], 1)
            last_event = json.loads(history.read_text().splitlines()[-1])
            self.assertEqual(last_event["status"], "recovery_not_found")
            self.assertFalse(last_event["worker_invoked"])

    def test_recovered_cache_completes_without_resetting_attempt_counter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = ProcessingLedger(root / "state.sqlite3")
            history = root / "history.jsonl"
            source_hash = "f" * 64
            source_path = root / "source.MOV"
            output_path = root / "recovered.mp4"
            source_path.write_bytes(b"source")
            ledger.claim(source_hash, str(source_path), max_attempts=2)
            ledger.mark_failed(source_hash, "attempt one failed")
            ledger.claim(source_hash, str(source_path), retry_failed=True, max_attempts=2)
            ledger.update(source_hash, status="interrupted", error="cancelled")

            class CachedResultClient:
                def recover_cached_normalization(self, **_kwargs):
                    return SimpleNamespace(
                        output_path=output_path,
                        sha256="a" * 64,
                        size_bytes=123,
                        profile={"width": 2160, "height": 3840, "fps": 30},
                        remote_processing_seconds=60.0,
                    )

            result = recover_interrupted_attempt(
                ledger,
                CachedResultClient(),
                source_hash=source_hash,
                source_path=source_path,
                output_path=output_path,
                history_path=history,
            )
            current = ledger.get(source_hash)
            self.assertEqual(result.output_path, output_path)
            self.assertEqual(current["status"], "completed")
            self.assertEqual(current["attempts"], 2)
            self.assertEqual(json.loads(history.read_text())["status"], "recovered")

    def test_third_attempt_requires_exact_hash_and_terminal_cancelled_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.MOV"
            source_path.write_bytes(b"immutable original")
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            ledger = ProcessingLedger(root / "ledger.sqlite3")
            history = root / "history.jsonl"
            first = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=False,
                history_path=history,
            )
            ledger.mark_failed(source_hash, "attempt 1 failed")
            append_history(history, {
                "attempt": 1,
                "status": "failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            })
            second = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=True,
                history_path=history,
            )
            ledger.update(source_hash, status="interrupted", error="attempt 2 cancelled")
            append_history(history, {
                "attempt": 2,
                "status": "recovery_not_found",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            })

            store = ModalExecutionStore(root / "supervision.sqlite3")
            execution, _ = store.reserve(
                execution_id="attempt-2-execution",
                source_sha256=source_hash,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                job_id="2" * 32,
                attempt_number=2,
                source_path=source_path,
                output_path=root / "normalized.mp4",
                budget_estimate_usd=1.35,
                budget_limit_usd=1.50,
            )
            store.update(
                execution.execution_id,
                status="submitted",
                phase="call_id_persisted",
                function_call_id="fc-confirmed-cancelled",
            )
            store.update(
                execution.execution_id,
                status="cancelled",
                phase="modal_call_cancelled",
                details={"automatic_resubmit": False},
            )

            with self.assertRaises(RetryNotAllowedError):
                verify_third_attempt_authorization(
                    ledger,
                    store,
                    source_hash=source_hash,
                    confirmed_source_hash="0" * 64,
                    confirmed_cancelled_call_id="fc-confirmed-cancelled",
                    history_path=history,
                )
            with self.assertRaises(RetryNotAllowedError):
                verify_third_attempt_authorization(
                    ledger,
                    store,
                    source_hash=source_hash,
                    confirmed_source_hash=source_hash,
                    confirmed_cancelled_call_id="fc-wrong-id",
                    history_path=history,
                )
            confirmed = verify_third_attempt_authorization(
                ledger,
                store,
                source_hash=source_hash,
                confirmed_source_hash=source_hash,
                confirmed_cancelled_call_id="fc-confirmed-cancelled",
                history_path=history,
            )
            self.assertEqual(confirmed.attempt_number, 2)

            third = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source_path,
                retry_failed=True,
                history_path=history,
                allow_third_attempt=True,
            )
            self.assertEqual((third["attempts"], third["status"]), (3, "processing"))
            self.assertEqual(source_path.read_bytes(), b"immutable original")
            with self.assertRaises(RetryNotAllowedError):
                claim_normalization_attempt(
                    ledger,
                    source_hash=source_hash,
                    source_path=source_path,
                    retry_failed=True,
                    history_path=history,
                    allow_third_attempt=True,
                )
            self.assertEqual(ledger.get(source_hash)["attempts"], 3)
            events = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertEqual(
                [(event["attempt"], event["status"]) for event in events],
                [(1, "started"), (1, "failed"), (2, "started"),
                 (2, "recovery_not_found"), (3, "started")],
            )

    def test_output_capacity_preflight_is_read_only_and_requires_nine_gib(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "scripts.normalize_modal_4k_once.shutil.disk_usage",
                return_value=SimpleNamespace(free=9 * 1024**3),
            ) as disk_usage:
                result = output_capacity_preflight(root / "not-created" / "output.mp4")
            disk_usage.assert_called_once_with(root)
            self.assertEqual(result["available_bytes"], 9 * 1024**3)
            self.assertFalse((root / "not-created").exists())

            with patch(
                "scripts.normalize_modal_4k_once.shutil.disk_usage",
                return_value=SimpleNamespace(free=9 * 1024**3 - 1),
            ):
                with self.assertRaisesRegex(RuntimeError, "Espacio insuficiente"):
                    output_capacity_preflight(root / "output.mp4")

    def test_authorized_third_attempt_submits_once_and_persists_attempt_three(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.MOV"
            source.write_bytes(b"immutable original fixture")
            output = root / "output.mp4"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            ledger_path = root / "ledger.sqlite3"
            history_dir = root / "history"
            history_path = history_dir / f"{source_hash}.jsonl"
            ledger = ProcessingLedger(ledger_path)

            first = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source,
                retry_failed=False,
                history_path=history_path,
            )
            ledger.mark_failed(source_hash, "attempt 1 failed")
            append_history(history_path, {
                "attempt": 1,
                "status": "failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            })
            claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source,
                retry_failed=True,
                history_path=history_path,
            )
            ledger.update(source_hash, status="interrupted", error="attempt 2 cancelled")
            append_history(history_path, {
                "attempt": 2,
                "status": "recovery_not_found",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            })
            store_path = root / "supervision.sqlite3"
            store = ModalExecutionStore(store_path)
            prior, _ = store.reserve(
                execution_id="prior-attempt-2",
                source_sha256=source_hash,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                job_id="2" * 32,
                attempt_number=2,
                source_path=source,
                output_path=output,
                budget_estimate_usd=1.3,
                budget_limit_usd=1.5,
            )
            store.update(
                prior.execution_id,
                status="submitted",
                phase="call_id_persisted",
                function_call_id="fc-attempt-two",
            )
            store.update(
                prior.execution_id,
                status="cancelled",
                phase="modal_call_cancelled",
            )
            submissions = []

            class FakeClient:
                def recover_cached_normalization(self, **_kwargs):
                    return None

                def submit_normalization_detached(self, **kwargs):
                    call_id = "fc-attempt-three"
                    kwargs["on_submitted"](call_id)
                    submissions.append(call_id)
                    return ModalNormalizationSubmission(
                        job_id=kwargs["job_id"],
                        function_call_id=call_id,
                        source_sha256=source_hash,
                        profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                        submitted_at=time.time(),
                    )

            profile = SimpleNamespace(
                width=2160, height=3840, duration=59.98, rotation=0,
                video_codec="hevc", color_range="tv", color_primaries="bt2020",
                color_transfer="arib-std-b67", color_space="bt2020nc",
            )
            args = [
                "--source", str(source),
                "--output", str(output),
                "--execution-store", str(store_path),
                "--allow-third-attempt",
                "--confirm-source-sha256", source_hash,
                "--confirm-cancelled-call-id", "fc-attempt-two",
                "--confirm-worker-code-version", "release-4k-03",
            ]
            with (
                patch("scripts.normalize_modal_4k_once.probe_video", return_value=profile),
                patch(
                    "scripts.normalize_modal_4k_once.output_capacity_preflight",
                    return_value={
                        "checked_directory": str(root),
                        "available_bytes": 16 * 1024**3,
                        "required_bytes": 9 * 1024**3,
                    },
                ),
                patch(
                    "scripts.normalize_modal_4k_once.enforce_4k_normalization_budget",
                    return_value=SimpleNamespace(maximum_usd=1.3),
                ),
                patch("scripts.normalize_modal_4k_once.LEDGER_PATH", ledger_path),
                patch("scripts.normalize_modal_4k_once.HISTORY_DIR", history_dir),
                patch("scripts.normalize_modal_4k_once.ModalRenderClient", return_value=FakeClient()),
            ):
                self.assertEqual(main(args), 0)
                with self.assertRaises(SystemExit):
                    main(args)

            self.assertEqual(submissions, ["fc-attempt-three"])
            self.assertEqual(ledger.get(source_hash)["attempts"], 3)
            self.assertEqual(ledger.get(source_hash)["status"], "processing")
            latest = ModalExecutionStore(store_path).latest_for_source(
                source_hash, MASTER_4K_NORMALIZATION_PROFILE_ID
            )
            self.assertEqual(latest.attempt_number, 3)
            self.assertEqual(latest.status, "submitted")
            self.assertEqual(latest.function_call_id, "fc-attempt-three")
            self.assertEqual(source.read_bytes(), b"immutable original fixture")
            self.assertFalse(output.exists())

    def test_preflight_only_checks_profile_without_modal_or_database_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.MOV"
            source.write_bytes(b"source fixture")
            ledger_path = root / "ledger.sqlite3"
            history_dir = root / "history"
            profile = SimpleNamespace(
                width=2160, height=3840, duration=59.98, rotation=0,
                video_codec="hevc", color_range="tv", color_primaries="bt2020",
                color_transfer="arib-std-b67", color_space="bt2020nc",
            )
            output = io.StringIO()
            with (
                patch("scripts.normalize_modal_4k_once.probe_video", return_value=profile),
                patch(
                    "scripts.normalize_modal_4k_once.output_capacity_preflight",
                    return_value={
                        "checked_directory": str(root),
                        "available_bytes": 10 * 1024**3,
                        "required_bytes": 9 * 1024**3,
                    },
                ),
                patch(
                    "scripts.normalize_modal_4k_once.enforce_4k_normalization_budget",
                    return_value=SimpleNamespace(maximum_usd=1.3455),
                ),
                patch("scripts.normalize_modal_4k_once.LEDGER_PATH", ledger_path),
                patch("scripts.normalize_modal_4k_once.HISTORY_DIR", history_dir),
                patch("scripts.normalize_modal_4k_once.ModalRenderClient") as modal_client,
                contextlib.redirect_stdout(output),
            ):
                code = main([
                    "--source", str(source),
                    "--output", str(root / "output.mp4"),
                    "--preflight-only",
                ])
            self.assertEqual(code, 0)
            modal_client.assert_not_called()
            self.assertFalse(ledger_path.exists())
            self.assertFalse(history_dir.exists())
            result = json.loads(output.getvalue())
            self.assertEqual(result["source_video_codec"], "hevc")
            self.assertEqual(result["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(result["normalization_profile"], MASTER_4K_NORMALIZATION_PROFILE_ID)


if __name__ == "__main__":
    unittest.main()
