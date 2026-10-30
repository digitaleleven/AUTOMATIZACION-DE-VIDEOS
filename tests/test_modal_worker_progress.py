import subprocess
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
