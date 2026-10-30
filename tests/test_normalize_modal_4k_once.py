import json
import contextlib
import io
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest

from scripts.normalize_modal_4k_once import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    NormalizationInterrupted,
    append_history,
    build_parser,
    claim_normalization_attempt,
    recover_interrupted_attempt,
    record_terminal_outcome,
)
from video_pipeline.state import ProcessingLedger, RetryNotAllowedError


class NormalizeModal4KRetryTests(unittest.TestCase):
    def test_cli_exposes_manual_retry_flag_only(self):
        self.assertFalse(build_parser().parse_args([]).retry_failed)
        self.assertTrue(build_parser().parse_args(["--retry-failed"]).retry_failed)
        self.assertTrue(build_parser().parse_args(["--recover-only"]).recover_only)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--retry-failed", "--recover-only"])

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


if __name__ == "__main__":
    unittest.main()
