import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts.modal_normalization_supervisor import (
    attach_existing_call,
    poll_execution,
    reconcile_legacy_terminal_records,
)
from video_pipeline.modal_execution_store import ModalExecutionStore
from video_pipeline.modal_renderer import MASTER_4K_NORMALIZATION_PROFILE_ID
from video_pipeline.state import ProcessingLedger


class InputCancellation(BaseException):
    pass


class FakeClient:
    def __init__(self, poll_result=None, poll_error=None, cached_result=None):
        self.poll_result = poll_result
        self.poll_error = poll_error
        self.cached_result = cached_result
        self.call_ids = []
        self.cache_reads = []
        self.progress_reads = []
        self.cancel_calls = []

    def poll_normalization_call(self, call_id):
        self.call_ids.append(call_id)
        if self.poll_error:
            raise self.poll_error
        return self.poll_result

    def read_normalization_progress(self, job_id):
        self.progress_reads.append(job_id)
        return None

    def recover_cached_normalization(self, **kwargs):
        self.cache_reads.append(kwargs)
        return self.cached_result


class ModalNormalizationSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "IMG_1254.MOV"
        self.source.write_bytes(b"source-original")
        self.output = self.root / "normalized.mp4"
        self.store = ModalExecutionStore(self.root / "supervisor.sqlite3")
        self.ledger_path = self.root / "legacy.sqlite3"
        self.history_dir = self.root / "history"
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        ledger = ProcessingLedger(self.ledger_path)
        ledger.claim(self.source_hash, str(self.source), max_attempts=2)
        ledger.mark_failed(self.source_hash, "first attempt failed")
        ledger.claim(
            self.source_hash,
            str(self.source),
            retry_failed=True,
            max_attempts=2,
        )
        ledger.update(self.source_hash, status="interrupted", error="historic cancellation")
        self.legacy = ledger
        self.prior_history = self.history_dir / f"{self.source_hash}.jsonl"
        self.prior_history.parent.mkdir(parents=True)
        self.prior_history.write_text(
            json.dumps({"attempt": 1, "status": "failed", "error": "first attempt failed"}) + "\n"
            + json.dumps({"attempt": 2, "status": "started"}) + "\n",
            encoding="utf-8",
        )

    def attach(self):
        return attach_existing_call(
            self.store,
            call_id="fc-existing-123",
            source_path=self.source,
            output_path=self.output,
            attempt_number=2,
            legacy_ledger=self.ledger_path,
        )[0]

    def result(self):
        return SimpleNamespace(
            output_path=self.output,
            sha256="b" * 64,
            size_bytes=1024,
            duration_seconds=59.98,
            source_sha256=self.source_hash,
            profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
            profile={"width": 2160, "height": 3840, "fps": 30},
            remote_processing_seconds=540,
            cache_hit=False,
        )

    def test_attach_and_pending_poll_reuse_call_id_without_spawn_or_cancel(self):
        record = self.attach()
        original_history = self.prior_history.read_text(encoding="utf-8")
        client = FakeClient()
        updated = poll_execution(self.store, client, record.execution_id)
        self.assertEqual(updated.status, "running")
        self.assertEqual(client.call_ids, ["fc-existing-123"])
        self.assertEqual(client.cancel_calls, [])
        self.assertEqual(self.prior_history.read_text(encoding="utf-8"), original_history)
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)

    def test_completed_call_recovers_verified_cache_and_appends_legacy_outcome(self):
        record = self.attach()
        client = FakeClient(
            poll_result={
                "job_id": "remote-job-id",
                "source_sha256": self.source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            },
            cached_result=self.result(),
        )
        completed = poll_execution(self.store, client, record.execution_id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(client.call_ids, ["fc-existing-123"])
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)
        reconcile_legacy_terminal_records(
            self.store,
            legacy_ledger=self.ledger_path,
            history_dir=self.history_dir,
        )
        self.assertEqual(self.legacy.get(self.source_hash)["status"], "completed")
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)
        history = [json.loads(line) for line in self.prior_history.read_text().splitlines()]
        self.assertEqual([(item["attempt"], item["status"]) for item in history[:2]], [
            (1, "failed"), (2, "started")
        ])
        self.assertEqual(history[-1]["status"], "completed")
        self.assertEqual(history[-1]["execution_id"], record.execution_id)

    def test_modal_cancellation_checks_cache_then_records_interruption_without_retry(self):
        record = self.attach()
        client = FakeClient(poll_error=InputCancellation("cancelled"))
        cancelled = poll_execution(self.store, client, record.execution_id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(len(client.cache_reads), 1)
        self.assertEqual(client.cancel_calls, [])
        reconcile_legacy_terminal_records(
            self.store,
            legacy_ledger=self.ledger_path,
            history_dir=self.history_dir,
        )
        self.assertEqual(self.legacy.get(self.source_hash)["status"], "interrupted")
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)
        history = [json.loads(line) for line in self.prior_history.read_text().splitlines()]
        self.assertEqual(history[0]["error"], "first attempt failed")
        self.assertEqual(history[-1]["status"], "interrupted")

    def test_cached_output_after_cancellation_is_recovered_without_new_call(self):
        record = self.attach()
        self.store.update(record.execution_id, status="cancelled", phase="historic_cancel")
        client = FakeClient(cached_result=self.result())
        recovered = poll_execution(self.store, client, record.execution_id)
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(client.call_ids, [])
        self.assertEqual(client.cache_reads[0]["profile_id"], MASTER_4K_NORMALIZATION_PROFILE_ID)
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)

    def test_supervisor_interrupt_is_persisted_without_cancelling_modal_call(self):
        record = self.attach()
        client = FakeClient(poll_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            poll_execution(self.store, client, record.execution_id)
        self.assertEqual(self.store.get(record.execution_id).status, "running")
        self.assertEqual(client.cancel_calls, [])

    def test_attach_reads_legacy_history_without_mutating_attempts(self):
        history_before = self.prior_history.read_bytes()
        self.attach()
        self.assertEqual(self.prior_history.read_bytes(), history_before)
        self.assertEqual(self.legacy.get(self.source_hash)["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
