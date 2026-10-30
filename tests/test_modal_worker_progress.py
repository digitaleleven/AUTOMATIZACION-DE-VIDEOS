import subprocess
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from video_pipeline import modal_render_app


class FakeProcess:
    pid = 43210

    def __init__(self, first_communicate=None):
        self.returncode = None
        self.calls = 0
        self.first_communicate = first_communicate

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1 and self.first_communicate:
            raise self.first_communicate
        self.returncode = 0
        return "stdout", "stderr"

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode


class ModalWorkerProgressTests(unittest.TestCase):
    def test_worker_code_version_is_recordable_and_nonempty(self):
        version = modal_render_app.WORKER_CODE_VERSION
        self.assertTrue(version)
        self.assertLessEqual(len(version), 80)
        self.assertRegex(version, r"^(?:sha256:[0-9a-f]{64}|[A-Za-z0-9._-]{1,80})$")

    def test_persisted_worker_events_include_code_version(self):
        class FakeVolume:
            def commit(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "jobs" / ("a" * 32)).mkdir(parents=True)
            with (
                patch.object(modal_render_app, "VOLUME_MOUNT", root),
                patch.object(modal_render_app, "staging_volume", FakeVolume()),
            ):
                event = modal_render_app._persist_worker_state(
                    "a" * 32,
                    status="running",
                    phase="preflight",
                )
            self.assertEqual(event["worker_code_version"], modal_render_app.WORKER_CODE_VERSION)
            saved = json.loads((root / "jobs" / ("a" * 32) / "worker-state.json").read_text())
            self.assertEqual(saved["worker_code_version"], modal_render_app.WORKER_CODE_VERSION)

    def test_long_subprocess_emits_heartbeat_and_returns_captured_output(self):
        process = FakeProcess(subprocess.TimeoutExpired(["fake"], 0.01))
        progress = []
        with patch.object(modal_render_app.subprocess, "Popen", return_value=process):
            result = modal_render_app._run_checked(
                ["fake"],
                timeout=5,
                progress_interval_seconds=0.01,
                progress_callback=progress.append,
            )
        self.assertGreaterEqual(progress[0], 0)
        self.assertEqual(result.stdout, "stdout")
        self.assertEqual(result.stderr, "stderr")

    def test_baseexception_terminates_child_process_group_and_propagates(self):
        process = FakeProcess(KeyboardInterrupt())
        with (
            patch.object(modal_render_app.subprocess, "Popen", return_value=process),
            patch.object(modal_render_app.os, "killpg") as killpg,
        ):
            with self.assertRaises(KeyboardInterrupt):
                modal_render_app._run_checked(
                    ["fake"],
                    timeout=5,
                    progress_interval_seconds=0.01,
                    progress_callback=lambda _elapsed: None,
                )
        killpg.assert_called_once_with(process.pid, modal_render_app.signal.SIGTERM)
        self.assertEqual(process.returncode, -15)


if __name__ == "__main__":
    unittest.main()
