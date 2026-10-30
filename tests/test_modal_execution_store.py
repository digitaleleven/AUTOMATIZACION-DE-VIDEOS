import tempfile
from pathlib import Path
import unittest

from video_pipeline.modal_execution_store import (
    ModalExecutionBudgetExceeded,
    ModalExecutionConflict,
    ModalExecutionStore,
    safe_error_message,
)


SOURCE_HASH = "a" * 64
PROFILE = "matrix-sdr-bt709-2160x3840-30-crf17-v1"


def _reserve(store, *, attempt=1, job_id=None, estimate=1.2, limit=1.5):
    return store.reserve(
        execution_id=f"execution-{attempt}",
        source_sha256=SOURCE_HASH,
        profile_id=PROFILE,
        job_id=job_id or f"{attempt:032x}",
        attempt_number=attempt,
        source_path="/tmp/original.mov",
        output_path=f"/tmp/output-{attempt}.mp4",
        budget_estimate_usd=estimate,
        budget_limit_usd=limit,
    )


class ModalExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "supervision.sqlite3"
        self.store = ModalExecutionStore(self.path)

    def test_call_id_and_events_survive_reopen_and_duplicate_is_blocked(self):
        record, created = _reserve(self.store)
        self.assertTrue(created)
        self.store.update(
            record.execution_id,
            status="submitted",
            phase="call_id_persisted",
            function_call_id="fc-existing-call",
        )
        reopened = ModalExecutionStore(self.path)
        persisted = reopened.get(record.execution_id)
        self.assertEqual(persisted.function_call_id, "fc-existing-call")
        self.assertEqual(persisted.status, "submitted")
        duplicate, created = _reserve(reopened)
        self.assertFalse(created)
        self.assertEqual(duplicate.execution_id, record.execution_id)
        self.assertGreaterEqual(len(reopened.events(record.execution_id)), 2)

    def test_budget_rejection_creates_no_execution(self):
        with self.assertRaises(ModalExecutionBudgetExceeded):
            _reserve(self.store, estimate=1.51, limit=1.50)
        self.assertEqual(self.store.list_active(), [])

    def test_terminal_failure_requires_explicit_new_attempt_number(self):
        first, _ = _reserve(self.store, attempt=1)
        self.store.update(first.execution_id, status="failed", phase="failed", error="test")
        with self.assertRaises(ModalExecutionConflict):
            _reserve(self.store, attempt=1)
        second, created = self.store.reserve(
            execution_id="execution-2",
            source_sha256=SOURCE_HASH,
            profile_id=PROFILE,
            job_id="2" * 32,
            attempt_number=2,
            source_path="/tmp/original.mov",
            output_path="/tmp/output-2.mp4",
            budget_estimate_usd=1.2,
            budget_limit_usd=1.5,
            allow_manual_retry=True,
        )
        self.assertTrue(created)
        self.assertEqual(second.attempt_number, 2)
        self.assertEqual(self.store.get(first.execution_id).status, "failed")

    def test_existing_call_can_be_attached_without_new_submission(self):
        attached, created = self.store.attach_existing_call(
            execution_id="attached-execution",
            source_sha256=SOURCE_HASH,
            profile_id=PROFILE,
            attempt_number=2,
            function_call_id="fc-historical-call",
            source_path="/tmp/original.mov",
            output_path="/tmp/recovered.mp4",
            budget_estimate_usd=1.2,
            budget_limit_usd=1.5,
        )
        same, created_again = self.store.attach_existing_call(
            execution_id="another-id-is-ignored",
            source_sha256=SOURCE_HASH,
            profile_id=PROFILE,
            attempt_number=2,
            function_call_id="fc-historical-call",
            source_path="/tmp/original.mov",
            output_path="/tmp/recovered.mp4",
            budget_estimate_usd=1.2,
            budget_limit_usd=1.5,
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(same.execution_id, attached.execution_id)
        self.assertEqual(self.store.list_active()[0].function_call_id, "fc-historical-call")

    def test_unknown_submission_can_be_attached_to_known_call_id(self):
        record, _ = _reserve(self.store)
        self.store.update(
            record.execution_id,
            status="submission_unknown",
            phase="lost_reply",
        )
        attached, created = self.store.attach_existing_call(
            execution_id="unused-new-id",
            source_sha256=SOURCE_HASH,
            profile_id=PROFILE,
            attempt_number=1,
            function_call_id="fc-found-in-modal",
            source_path="/tmp/original.mov",
            output_path="/tmp/output-1.mp4",
            budget_estimate_usd=1.2,
            budget_limit_usd=1.5,
        )
        self.assertFalse(created)
        self.assertEqual(attached.execution_id, record.execution_id)
        self.assertEqual(attached.function_call_id, "fc-found-in-modal")
        self.assertEqual(attached.status, "submitted")

    def test_recovery_requires_verified_matching_source_profile_and_checksum(self):
        record, _ = _reserve(self.store)
        bad_payload = {
            "cache_verified": False,
            "source_sha256": SOURCE_HASH,
            "profile_id": PROFILE,
            "output_sha256": "b" * 64,
            "output_path": "/tmp/result.mp4",
        }
        with self.assertRaises(ValueError):
            self.store.mark_recovered(record.execution_id, result=bad_payload)
        good_payload = {
            "cache_verified": True,
            "source_sha256": SOURCE_HASH,
            "profile_id": PROFILE,
            "output_sha256": "b" * 64,
            "output_path": "/tmp/result.mp4",
        }
        recovered = self.store.mark_recovered(record.execution_id, result=good_payload)
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(recovered.result_json is not None, True)
        self.assertEqual(self.store.list_terminal_unreconciled()[0].execution_id, record.execution_id)

    def test_error_messages_redact_urls_and_credentials(self):
        safe = safe_error_message(
            "failed https://storage.example/object?X-Amz-Signature=private "
            "Authorization=Bearer abcdefghijklmnop sk-abcdefghijklmnopqrstuvwxyz"
        )
        self.assertNotIn("storage.example", safe)
        self.assertNotIn("abcdefghijklmnop", safe)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", safe)


if __name__ == "__main__":
    unittest.main()
