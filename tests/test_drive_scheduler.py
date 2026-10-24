import unittest
from types import SimpleNamespace

from video_pipeline.drive_scheduler import DriveScanScheduler


class DriveScanSchedulerTests(unittest.TestCase):
    def test_does_not_scan_until_oauth_is_ready_and_identity_is_authorized(self):
        class Panel:
            drive_oauth = SimpleNamespace(
                status=lambda: {
                    "ready": True,
                    "authorized": False,
                    "identity_verified": False,
                }
            )

            def __init__(self):
                self.scan_calls = 0

            def scan_drive(self):
                self.scan_calls += 1

        panel = Panel()
        scheduler = DriveScanScheduler(panel, interval_seconds=300)
        self.assertFalse(scheduler.run_once_if_authorized())
        self.assertEqual(panel.scan_calls, 0)
        self.assertIsNone(scheduler.status()["last_scan_at"])

    def test_periodic_scan_only_imports_and_reports_counts_without_rendering(self):
        class Panel:
            drive_oauth = SimpleNamespace(
                status=lambda: {
                    "ready": True,
                    "authorized": True,
                    "identity_verified": True,
                }
            )

            def __init__(self):
                self.scan_calls = 0

            def scan_drive(self):
                self.scan_calls += 1
                return {
                    "imported": [{"id": "upload-id"}],
                    "skipped": 3,
                    "failed": [],
                }

        panel = Panel()
        scheduler = DriveScanScheduler(panel, interval_seconds=300)
        self.assertTrue(scheduler.run_once_if_authorized())
        self.assertEqual(panel.scan_calls, 1)
        self.assertEqual(
            scheduler.status()["last_summary"],
            {"imported": 1, "skipped": 3, "failed": 0},
        )
        self.assertIsNotNone(scheduler.status()["last_scan_at"])
        self.assertFalse(scheduler.status()["running"])

    def test_scan_errors_are_redacted_in_status(self):
        class Panel:
            drive_oauth = SimpleNamespace(
                status=lambda: {"ready": True, "authorized": True}
            )

            def scan_drive(self):
                raise RuntimeError("private response body")

        scheduler = DriveScanScheduler(Panel(), interval_seconds=300)
        self.assertFalse(scheduler.run_once_if_authorized())
        status = scheduler.status()
        self.assertEqual(status["last_error"], "No se pudo completar el escaneo automático.")
        self.assertNotIn("private response body", str(status))

    def test_rejects_unreasonably_short_scan_interval(self):
        with self.assertRaises(ValueError):
            DriveScanScheduler(SimpleNamespace(), interval_seconds=30)


if __name__ == "__main__":
    unittest.main()
