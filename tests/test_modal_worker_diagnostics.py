import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from video_pipeline.modal_render_app import (
    _persist_worker_failure,
    _worker_failure_diagnostic,
)


class ModalWorkerDiagnosticTests(unittest.TestCase):
    def test_preserves_process_exit_and_stderr_without_command_or_credentials(self):
        exc = subprocess.CalledProcessError(
            1, ["node", "--briefing", "private customer notes"],
            output="phase=preflight",
            stderr=(
                "Uso: render-master.mjs\n"
                "Authorization: Bearer credential-value\n"
                "x-goog-api-key=another-value\n"
                "https://example.test/path?token=hidden\n"
                '{"token":"json-sensitive","password":"json-password"}\n'
            ),
        )
        report = _worker_failure_diagnostic("a" * 32, exc)
        self.assertEqual(report["returncode"], 1)
        self.assertIn("Uso: render-master.mjs", report["stderr"])
        self.assertEqual(report["stdout"], "phase=preflight")
        text = json.dumps(report)
        for secret in (
            "private customer notes", "credential-value", "another-value",
            "token=hidden", "json-sensitive", "json-password",
        ):
            self.assertNotIn(secret, text)

    def test_timeout_bytes_and_large_logs_are_bounded(self):
        exc = subprocess.TimeoutExpired(
            ["node"], 120, output=b"x" * 20000, stderr=b"decoder timeout"
        )
        report = _worker_failure_diagnostic("a" * 32, exc)
        self.assertEqual(report["timeout_seconds"], 120)
        self.assertEqual(report["stderr"], "decoder timeout")
        self.assertLessEqual(len(report["stdout"]), 8000)

    def test_persists_failure_before_cleanup_and_never_overwrites_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = subprocess.CalledProcessError(1, ["node"], stderr="first error")
            _persist_worker_failure(root, "a" * 32, first)
            with self.assertRaises(FileExistsError):
                _persist_worker_failure(root, "a" * 32, RuntimeError("second error"))
            self.assertEqual(json.loads((root / "failure.json").read_text())["stderr"], "first error")
