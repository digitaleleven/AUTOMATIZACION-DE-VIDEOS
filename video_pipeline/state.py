"""SQLite processing ledger for local idempotency and resumable renders."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator


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
                    error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(processing_jobs)")
            }
            for column in ("subtitle_url", "logo_url"):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE processing_jobs ADD COLUMN {column} TEXT")

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
        stale_after_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Claim a source, allowing at most one explicit retry after failure."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM processing_jobs WHERE source_sha256 = ?",
                (source_sha256,),
            ).fetchone()
            now = self._now()

            if row is None:
                connection.execute(
                    """INSERT INTO processing_jobs
                    (source_sha256, source_path, status, attempts, updated_at)
                    VALUES (?, ?, 'processing', 1, ?)""",
                    (source_sha256, source_path, now),
                )
                return self.get(source_sha256, connection=connection)

            job = dict(row)
            if job["status"] == "completed":
                raise DuplicateProcessingError(
                    f"This video was already processed. Existing output: {job.get('output_path')}"
                )
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
                    error = NULL, updated_at = ?
                   WHERE source_sha256 = ?""",
                (source_path, now, source_sha256),
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
        allowed = {
            "output_path", "captions_json", "shotstack_source_url", "subtitle_url",
            "logo_url", "render_id", "error", "status"
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
        self.update(source_sha256, status="failed", error=error[:500])

    def mark_completed(self, source_sha256: str, output_path: str) -> None:
        self.update(source_sha256, status="completed", output_path=output_path, error=None)
