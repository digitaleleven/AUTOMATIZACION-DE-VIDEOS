import json
from pathlib import Path
import tempfile
import unittest

from scripts.normalize_modal_4k_once import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    append_history,
    build_parser,
    claim_normalization_attempt,
)
from video_pipeline.state import ProcessingLedger, RetryNotAllowedError


class NormalizeModal4KRetryTests(unittest.TestCase):
    def test_cli_exposes_manual_retry_flag_only(self):
        self.assertFalse(build_parser().parse_args([]).retry_failed)
        self.assertTrue(build_parser().parse_args(["--retry-failed"]).retry_failed)

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


if __name__ == "__main__":
    unittest.main()
