"""SQLite processing ledger for local idempotency and resumable renders."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import hashlib
import sqlite3
from typing import Any, Iterator

from .config import RENDER_IDENTITY_VERSION


class DuplicateProcessingError(RuntimeError):
    """Raised when a source is already complete or being processed."""


class RetryNotAllowedError(RuntimeError):
    """Raised when a failed/stale job needs explicit manual retry approval."""


class ProcessingLedger:
    def __init__(self, database_path: str | Path = "data/processing.sqlite3"):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processing_jobs (
                    source_sha256 TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    output_path TEXT,
                    captions_json TEXT,
                    shotstack_source_url TEXT,
                    subtitle_url TEXT,
                    logo_url TEXT,
                    render_id TEXT,
                    execution_key TEXT,
                    edit_options_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(processing_jobs)")
            }
            if "subtitle_url" not in existing_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN subtitle_url TEXT")
            if "logo_url" not in existing_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN logo_url TEXT")
            if "edit_options_json" not in existing_columns:
                connection.execute(
                    "ALTER TABLE processing_jobs ADD COLUMN edit_options_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "execution_key" not in existing_columns:
                connection.execute("ALTER TABLE processing_jobs ADD COLUMN execution_key TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def claim(
        self,
        source_sha256: str,
        source_path: str,
        *,
        retry_failed: bool = False,
        max_attempts: int = 2,
        allow_third_attempt: bool = False,
        stale_after_seconds: int = 3600,
        edit_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Claim a source, allowing a third attempt only through an explicit scoped gate."""
        if allow_third_attempt:
            if not retry_failed or max_attempts != 3:
                raise RetryNotAllowedError("A third attempt requires the explicit controlled retry gate.")
        elif max_attempts > 2:
            raise RetryNotAllowedError("Only the controlled RENDER-001 path may reserve a third attempt.")
        requested_options = edit_options or {}
        requested_options_json = json.dumps(
            requested_options, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        execution_key = stable_execution_key(source_sha256, requested_options)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM processing_jobs WHERE source_sha256 = ?",
                (source_sha256,),
            ).fetchone()
            now = self._now()

            if row is None:
                if allow_third_attempt:
                    raise RetryNotAllowedError("A third attempt cannot start without the two recorded failures.")
                connection.execute(
                    """INSERT INTO processing_jobs
                    (source_sha256, source_path, status, attempts, execution_key,
                     edit_options_json, updated_at)
                    VALUES (?, ?, 'processing', 1, ?, ?, ?)""",
                    (source_sha256, source_path, execution_key, requested_options_json, now),
                )
                return self.get(source_sha256, connection=connection)

            job = dict(row)
            if job["status"] == "completed":
                raise DuplicateProcessingError(
                    f"This video was already processed. Existing output: {job.get('output_path')}"
                )
            if job["status"] == "submission_unknown":
                raise RetryNotAllowedError(
                        "A remote render may have accepted the previous request. "
                        "Reconcile its status before any retry."
                )
            if allow_third_attempt and job["status"] not in {"failed", "interrupted"}:
                raise RetryNotAllowedError(
                    "The controlled third attempt requires a failed or reconciled interrupted prior attempt."
                )
            try:
                previous_options = json.loads(job.get("edit_options_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                previous_options = None
            if previous_options != requested_options:
                raise RetryNotAllowedError(
                    "The requested edit settings differ from the previous attempt. "
                    "Use an unprocessed source copy rather than reusing cached provider assets."
                )
            saved_execution_key = job.get("execution_key")
            if saved_execution_key and saved_execution_key != execution_key:
                raise RetryNotAllowedError(
                    "The requested execution identity differs from the previous attempt."
                )
            if not saved_execution_key:
                connection.execute(
                    "UPDATE processing_jobs SET execution_key = ? WHERE source_sha256 = ?",
                    (execution_key, source_sha256),
                )
            if allow_third_attempt and int(job["attempts"]) != 2:
                raise RetryNotAllowedError("The controlled third attempt requires exactly two prior attempts.")
            if job["status"] == "processing":
                updated = datetime.fromisoformat(job["updated_at"])
                if datetime.now(timezone.utc) - updated <= timedelta(seconds=stale_after_seconds):
                    raise DuplicateProcessingError("This video is already marked as processing.")
                if not retry_failed:
                    raise RetryNotAllowedError(
                        "A previous run stopped before recording completion. Inspect the job, then use --retry-failed."
                    )
            elif not retry_failed:
                raise RetryNotAllowedError(
                    "A previous run failed. Inspect its logs, then use --retry-failed for one controlled retry."
                )

            if int(job["attempts"]) >= max_attempts:
                raise RetryNotAllowedError("The one permitted retry has already been used.")
            connection.execute(
                """UPDATE processing_jobs SET
                    source_path = ?, status = 'processing', attempts = attempts + 1,
                    execution_key = ?,
                    error = NULL, updated_at = ?
                   WHERE source_sha256 = ?""",
                (source_path, execution_key, now, source_sha256),
            )
            return self.get(source_sha256, connection=connection)

    def get(self, source_sha256: str, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM processing_jobs WHERE source_sha256 = ?", (source_sha256,)
            ).fetchone()
            return dict(row) if row else {}
        with self._connect() as owned:
            row = owned.execute(
                "SELECT * FROM processing_jobs WHERE source_sha256 = ?", (source_sha256,)
            ).fetchone()
            return dict(row) if row else {}

    def update(self, source_sha256: str, **fields: Any) -> None:
        # Legacy database columns remain intact for historical rows, but new code
        # no longer writes provider-specific fields.
        allowed = {
            "output_path", "captions_json", "subtitle_url", "logo_url",
            "render_id", "execution_key", "error", "status"
        }
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported or empty processing-ledger update.")
        assignments = [f"{key} = ?" for key in fields]
        values = list(fields.values())
        assignments.append("updated_at = ?")
        values.extend((self._now(), source_sha256))
        with self._connect() as connection:
            connection.execute(
                f"UPDATE processing_jobs SET {', '.join(assignments)} WHERE source_sha256 = ?",
                values,
            )

    def mark_failed(self, source_sha256: str, error: str) -> None:
        self.update(source_sha256, status="failed", error=error[:1600])

    def mark_completed(self, source_sha256: str, output_path: str) -> None:
        self.update(source_sha256, status="completed", output_path=output_path, error=None)


def stable_execution_key(source_sha256: str, edit_options: dict[str, Any]) -> str:
    """Identify one source/configuration pair across process restarts and retries."""
    if not isinstance(source_sha256, str) or not source_sha256:
        raise ValueError("A source identity is required.")
    canonical_options = json.dumps(
        edit_options, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    identity = (
        f"matrix-motion-render/{RENDER_IDENTITY_VERSION}\0"
        f"{source_sha256}\0{canonical_options}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
