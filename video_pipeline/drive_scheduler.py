"""Conservative periodic Drive discovery; never starts a render."""

import logging
import os
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)
DEFAULT_SCAN_INTERVAL_SECONDS = 15 * 60
MIN_SCAN_INTERVAL_SECONDS = 5 * 60
MAX_SCAN_INTERVAL_SECONDS = 24 * 60 * 60


def scan_interval_from_environment() -> int:
    raw = os.getenv("GOOGLE_DRIVE_SCAN_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SCAN_INTERVAL_SECONDS
    try:
        interval = int(raw)
    except ValueError:
        logger.warning(
            "GOOGLE_DRIVE_SCAN_INTERVAL_SECONDS is invalid; using the 15-minute default."
        )
        return DEFAULT_SCAN_INTERVAL_SECONDS
    if not MIN_SCAN_INTERVAL_SECONDS <= interval <= MAX_SCAN_INTERVAL_SECONDS:
        logger.warning(
            "GOOGLE_DRIVE_SCAN_INTERVAL_SECONDS is outside the supported range; using the 15-minute default."
        )
        return DEFAULT_SCAN_INTERVAL_SECONDS
    return interval


class DriveScanScheduler:
    """Poll Drive only after explicit OAuth setup and account authorization."""

    def __init__(self, panel: Any, interval_seconds: int | None = None):
        self.panel = panel
        self.interval_seconds = (
            scan_interval_from_environment()
            if interval_seconds is None
            else int(interval_seconds)
        )
        if not MIN_SCAN_INTERVAL_SECONDS <= self.interval_seconds <= MAX_SCAN_INTERVAL_SECONDS:
            raise ValueError("Drive scan interval must be between 5 minutes and 24 hours.")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_scan_at: float | None = None
        self._last_summary: dict[str, int] | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="drive-periodic-scan",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "enabled": True,
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_seconds": self.interval_seconds,
                "last_scan_at": self._last_scan_at,
                "last_summary": dict(self._last_summary) if self._last_summary else None,
                "last_error": self._last_error,
            }

    def run_once_if_authorized(self) -> bool:
        oauth = getattr(self.panel, "drive_oauth", None)
        if oauth is None:
            return False
        try:
            status = oauth.status()
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            return False
        if not status.get("ready") or not status.get("authorized"):
            return False

        try:
            result = self.panel.scan_drive()
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            return False

        imported = result.get("imported", []) if isinstance(result, dict) else []
        failures = result.get("failed", []) if isinstance(result, dict) else []
        summary = {
            "imported": len(imported) if isinstance(imported, list) else 0,
            "skipped": int(result.get("skipped", 0) or 0) if isinstance(result, dict) else 0,
            "failed": len(failures) if isinstance(failures, list) else 0,
        }
        with self._state_lock:
            self._last_scan_at = time.time()
            self._last_summary = summary
            self._last_error = (
                "Uno o más archivos no se pudieron importar."
                if summary["failed"]
                else None
            )
        if summary["failed"]:
            logger.warning(
                "Periodic Drive scan completed with failures: imported=%d skipped=%d failed=%d",
                summary["imported"],
                summary["skipped"],
                summary["failed"],
            )
        else:
            logger.info(
                "Periodic Drive scan completed: imported=%d skipped=%d failed=0",
                summary["imported"],
                summary["skipped"],
            )
        return summary["failed"] == 0

    def _record_failure(self, reason: str) -> None:
        with self._state_lock:
            self._last_scan_at = time.time()
            self._last_error = "No se pudo completar el escaneo automático."
            self._last_summary = None
        logger.warning("Periodic Drive scan failed (%s).", reason)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.run_once_if_authorized()
