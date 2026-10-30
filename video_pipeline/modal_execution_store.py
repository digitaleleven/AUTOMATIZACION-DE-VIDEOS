"""Durable supervision records for detached Modal executions.

This store is deliberately separate from the legacy attempt ledger: it records
Modal call identity and lifecycle without rewriting prior attempt history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import re
import sqlite3
from typing import Any


class ModalExecutionConflict(RuntimeError):
    """A duplicate or ambiguous Modal submission must be reconciled, not retried."""


class ModalExecutionBudgetExceeded(RuntimeError):
    """A recorded estimate exceeds the configured internal budget."""


ACTIVE_STATES = {"reserved", "submitting", "submitted", "running", "submission_unknown"}
TERMINAL_STATES = {"completed", "failed", "cancelled", "recovered"}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error_message(error: BaseException | str | None, *, limit: int = 1200) -> str | None:
    """Bound diagnostics and redact common credential, bearer, and signed-URL forms."""
    if error is None:
        return None
    text = str(error)
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization|x-api-key|api[_-]?key|token(?:[_-](?:id|secret))?|"
        r"password|secret)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?[^\s,;\"']+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"\b(?:sk-|fc-)[A-Za-z0-9_-]{10,}", "[IDENTIFIER REDACTED]", text)
    return text[:limit]


@dataclass(frozen=True)
class ModalExecution:
    execution_id: str
    source_sha256: str
    profile_id: str
    job_id: str | None
    attempt_number: int
    status: str
    phase: str
    function_call_id: str | None
    source_path: str
    output_path: str
    budget_estimate_usd: float
    budget_limit_usd: float
    result_json: str | None
    error_type: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    legacy_reconciled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


class ModalExecutionStore:
    """SQLite job state with append-only lifecycle events and duplicate guards."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS modal_executions (
                    execution_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    job_id TEXT UNIQUE,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    function_call_id TEXT UNIQUE,
                    source_path TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    budget_estimate_usd REAL NOT NULL,
                    budget_limit_usd REAL NOT NULL,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    legacy_reconciled INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_sha256, profile_id, attempt_number)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS modal_execution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(execution_id) REFERENCES modal_executions(execution_id)
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(modal_executions)")
            }
            if "legacy_reconciled" not in columns:
                connection.execute(
                    "ALTER TABLE modal_executions ADD COLUMN legacy_reconciled INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_modal_execution_active
                ON modal_executions(status, updated_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> ModalExecution | None:
        if row is None:
            return None
        values = dict(row)
        values["legacy_reconciled"] = bool(values.get("legacy_reconciled", 0))
        return ModalExecution(**values)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        execution_id: str,
        status: str,
        phase: str,
        details: dict[str, Any] | None,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO modal_execution_events
                (execution_id, status, phase, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                status,
                phase,
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )

    def reserve(
        self,
        *,
        execution_id: str,
        source_sha256: str,
        profile_id: str,
        job_id: str,
        attempt_number: int,
        source_path: str | Path,
        output_path: str | Path,
        budget_estimate_usd: float,
        budget_limit_usd: float,
        allow_manual_retry: bool = False,
    ) -> tuple[ModalExecution, bool]:
        """Atomically reserve one execution; returns (record, created)."""
        if (
            len(source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in source_sha256)
            or not profile_id
            or not execution_id
            or not job_id
            or attempt_number < 1
            or not math.isfinite(budget_estimate_usd)
            or not math.isfinite(budget_limit_usd)
            or budget_estimate_usd < 0
            or budget_limit_usd <= 0
        ):
            raise ValueError("La identidad o el presupuesto de ejecución Modal no es válido.")
        if budget_estimate_usd > budget_limit_usd:
            raise ModalExecutionBudgetExceeded(
                "El coste estimado supera el límite interno; no se reservó ni envió el trabajo."
            )

        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM modal_executions
                WHERE source_sha256 = ? AND profile_id = ?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (source_sha256, profile_id),
            ).fetchone()
            if existing:
                current = self._row(existing)
                assert current is not None
                if current.status in ACTIVE_STATES or current.status in {"completed", "recovered"}:
                    return current, False
                if not (
                    allow_manual_retry
                    and current.status in {"failed", "cancelled"}
                    and attempt_number == current.attempt_number + 1
                ):
                    raise ModalExecutionConflict(
                        "La ejecución anterior terminó; un nuevo intento requiere autorización manual explícita."
                    )

            connection.execute(
                """
                INSERT INTO modal_executions (
                    execution_id, source_sha256, profile_id, job_id, attempt_number,
                    status, phase, function_call_id, source_path, output_path,
                    budget_estimate_usd, budget_limit_usd, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', 'reserved', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    source_sha256,
                    profile_id,
                    job_id,
                    attempt_number,
                    str(source_path),
                    str(output_path),
                    float(budget_estimate_usd),
                    float(budget_limit_usd),
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                execution_id,
                "reserved",
                "reserved",
                {
                    "attempt_number": attempt_number,
                    "budget_estimate_usd": budget_estimate_usd,
                    "budget_limit_usd": budget_limit_usd,
                },
                now,
            )
            row = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            record = self._row(row)
            assert record is not None
            return record, True

    def attach_existing_call(
        self,
        *,
        execution_id: str,
        source_sha256: str,
        profile_id: str,
        attempt_number: int,
        function_call_id: str,
        source_path: str | Path,
        output_path: str | Path,
        budget_estimate_usd: float,
        budget_limit_usd: float,
    ) -> tuple[ModalExecution, bool]:
        """Register a known Modal call ID without submitting work or changing attempts."""
        if (
            not function_call_id.startswith("fc-")
            or len(function_call_id) > 128
            or len(source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in source_sha256)
            or not profile_id
            or not 1 <= attempt_number <= 2
            or not math.isfinite(budget_estimate_usd)
            or not math.isfinite(budget_limit_usd)
            or budget_estimate_usd < 0
            or budget_limit_usd <= 0
        ):
            raise ValueError("ID de ejecución Modal no válido.")
        if budget_estimate_usd > budget_limit_usd:
            raise ModalExecutionBudgetExceeded(
                "El presupuesto interno impide registrar esta recuperación."
            )
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM modal_executions
                WHERE source_sha256 = ? AND profile_id = ? AND attempt_number = ?
                """,
                (source_sha256, profile_id, attempt_number),
            ).fetchone()
            if existing:
                record = self._row(existing)
                assert record is not None
                if record.function_call_id == function_call_id:
                    return record, False
                if (
                    record.function_call_id is None
                    and record.status in ACTIVE_STATES
                    and record.status in {"reserved", "submitting", "submission_unknown"}
                ):
                    connection.execute(
                        """
                        UPDATE modal_executions
                        SET status = 'submitted', phase = 'attached_existing_call',
                            function_call_id = ?, updated_at = ?
                        WHERE execution_id = ?
                        """,
                        (function_call_id, now, record.execution_id),
                    )
                    self._append_event(
                        connection,
                        record.execution_id,
                        "submitted",
                        "attached_existing_call",
                        {"existing_call_attached": True, "attempt_number": attempt_number},
                        now,
                    )
                    updated = connection.execute(
                        "SELECT * FROM modal_executions WHERE execution_id = ?",
                        (record.execution_id,),
                    ).fetchone()
                    attached = self._row(updated)
                    assert attached is not None
                    return attached, False
                raise ModalExecutionConflict(
                    "Ya existe un registro distinto para este intento; no se reemplazará."
                )
            connection.execute(
                """
                INSERT INTO modal_executions (
                    execution_id, source_sha256, profile_id, job_id, attempt_number,
                    status, phase, function_call_id, source_path, output_path,
                    budget_estimate_usd, budget_limit_usd, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, 'submitted', 'attached_existing_call', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    source_sha256,
                    profile_id,
                    attempt_number,
                    function_call_id,
                    str(source_path),
                    str(output_path),
                    float(budget_estimate_usd),
                    float(budget_limit_usd),
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                execution_id,
                "submitted",
                "attached_existing_call",
                {"existing_call_attached": True, "attempt_number": attempt_number},
                now,
            )
            row = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            record = self._row(row)
            assert record is not None
            return record, True

    def record_manual_modal_cancellation_evidence(
        self,
        execution_id: str,
        *,
        function_call_id: str,
        evidence_file_name: str,
        evidence_sha256: str,
    ) -> tuple[ModalExecution, bool]:
        """Append reviewed provider evidence without rewriting the AuthError state."""
        if (
            not function_call_id.startswith("fc-")
            or len(function_call_id) > 128
            or not evidence_file_name
            or len(evidence_file_name) > 255
            or Path(evidence_file_name).name != evidence_file_name
            or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256)
        ):
            raise ValueError("La evidencia de cancelación Modal no tiene un formato válido.")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            record = self._row(row)
            if record is None:
                raise KeyError("No existe la ejecución Modal que se quiere conciliar.")
            if (
                record.attempt_number != 2
                or record.status != "failed"
                or record.error_type != "AuthError"
                or record.function_call_id != function_call_id
            ):
                raise ModalExecutionConflict(
                    "La evidencia solo puede conciliar el intento 2 con AuthError y el mismo ID."
                )
            prior_events = connection.execute(
                """
                SELECT details_json FROM modal_execution_events
                WHERE execution_id = ? AND phase = 'manual_modal_cancellation_evidence'
                ORDER BY event_id
                """,
                (execution_id,),
            ).fetchall()
            for event in prior_events:
                details = json.loads(event["details_json"])
                if (
                    details.get("function_call_id") == function_call_id
                    and details.get("evidence_sha256") == evidence_sha256
                ):
                    return record, False
                raise ModalExecutionConflict(
                    "Ya existe evidencia de cancelación manual distinta para esta ejecución."
                )
            self._append_event(
                connection,
                execution_id,
                record.status,
                "manual_modal_cancellation_evidence",
                {
                    "provider": "modal",
                    "provider_status": "cancelled",
                    "function_call_id": function_call_id,
                    "evidence_file_name": evidence_file_name,
                    "evidence_sha256": evidence_sha256,
                    "operator_confirmed": True,
                },
                now,
            )
            updated = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            confirmed = self._row(updated)
            assert confirmed is not None
            return confirmed, True

    def record_modal_status_observation(
        self,
        execution_id: str,
        *,
        function_call_id: str,
        provider_status: str,
        observation_method: str,
        observed_at_utc: str,
        exception_type: str | None,
        sdk_version: str | None,
        response_sha256: str | None = None,
        error_message: str | None = None,
    ) -> tuple[ModalExecution, str, bool]:
        """Append a direct read-only Modal lookup without changing the stored attempt."""
        if (
            not function_call_id.startswith("fc-")
            or len(function_call_id) > 128
            or provider_status not in {"cancelled", "running", "completed", "auth_error", "query_error"}
            or observation_method != "FunctionCall.from_id.get(timeout=0)"
            or not observed_at_utc
            or len(observed_at_utc) > 80
            or (sdk_version is not None and len(sdk_version) > 80)
            or (
                response_sha256 is not None
                and not re.fullmatch(r"[a-f0-9]{64}", response_sha256)
            )
        ):
            raise ValueError("La observación de estado de Modal no tiene un formato válido.")
        if provider_status == "cancelled" and (
            exception_type != "modal.exception.InputCancellation"
        ):
            raise ValueError("La cancelación requiere una excepción terminal tipada del SDK.")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            record = self._row(row)
            if record is None:
                raise KeyError("No existe la ejecución Modal que se quiere consultar.")
            if (
                record.attempt_number != 2
                or record.status != "failed"
                or record.error_type != "AuthError"
                or record.function_call_id != function_call_id
            ):
                raise ModalExecutionConflict(
                    "La consulta solo puede observar el intento 2 fallido con AuthError y el mismo ID."
                )
            details = {
                "provider": "modal",
                "provider_status": provider_status,
                "function_call_id": function_call_id,
                "observation_method": observation_method,
                "observed_at_utc": observed_at_utc,
                "exception_type": exception_type,
                "sdk_version": sdk_version,
                "response_sha256": response_sha256,
                "error_message": safe_error_message(error_message),
            }
            canonical = json.dumps(
                details,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            evidence_sha256 = hashlib.sha256(canonical).hexdigest()
            details["evidence_sha256"] = evidence_sha256
            self._append_event(
                connection,
                execution_id,
                record.status,
                "modal_status_observation",
                details,
                now,
            )
            updated = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            observed = self._row(updated)
            assert observed is not None
            return observed, evidence_sha256, True

    def update(
        self,
        execution_id: str,
        *,
        status: str,
        phase: str,
        details: dict[str, Any] | None = None,
        function_call_id: str | None = None,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> ModalExecution:
        if status not in ALL_STATES:
            raise ValueError("Estado Modal no reconocido.")
        now = _utc_now()
        result_json = (
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if result is not None else None
        )
        error_type = type(error).__name__ if error is not None else None
        error_message = safe_error_message(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if current is None:
                raise KeyError("No existe el registro de supervisión Modal.")
            if current["status"] in TERMINAL_STATES and status != current["status"]:
                raise ModalExecutionConflict(
                    "No se permite cambiar un estado terminal ni reiniciar automáticamente."
                )
            if current["status"] == "completed" and status == "completed":
                raise ModalExecutionConflict("El resultado Modal ya quedó registrado.")
            saved_call_id = function_call_id or current["function_call_id"]
            if current["function_call_id"] and function_call_id:
                if current["function_call_id"] != function_call_id:
                    raise ModalExecutionConflict("El ID de ejecución Modal no puede reemplazarse.")
            connection.execute(
                """
                UPDATE modal_executions
                SET status = ?, phase = ?, function_call_id = ?,
                    result_json = COALESCE(?, result_json),
                    error_type = ?, error_message = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    status,
                    phase,
                    saved_call_id,
                    result_json,
                    error_type,
                    error_message,
                    now,
                    execution_id,
                ),
            )
            self._append_event(connection, execution_id, status, phase, details, now)
            return self.get(execution_id, connection=connection)

    def get(
        self,
        execution_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ModalExecution | None:
        if connection is not None:
            return self._row(
                connection.execute(
                    "SELECT * FROM modal_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
            )
        with self._connect() as owned:
            return self.get(execution_id, connection=owned)

    def get_by_call_id(self, function_call_id: str) -> ModalExecution | None:
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM modal_executions WHERE function_call_id = ?",
                    (function_call_id,),
                ).fetchone()
            )

    def latest_for_source(self, source_sha256: str, profile_id: str) -> ModalExecution | None:
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    """
                    SELECT * FROM modal_executions
                    WHERE source_sha256 = ? AND profile_id = ?
                    ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (source_sha256, profile_id),
                ).fetchone()
            )

    def mark_recovered(
        self,
        execution_id: str,
        *,
        result: dict[str, Any],
        details: dict[str, Any] | None = None,
    ) -> ModalExecution:
        """Record a verified existing cache even if the Modal call was cancelled."""
        if (
            result.get("cache_verified") is not True
            or result.get("source_sha256") is None
            or result.get("profile_id") is None
            or result.get("output_sha256") is None
        ):
            raise ValueError("La recuperación exige un manifiesto y checksum verificados.")
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if current is None:
                raise KeyError("No existe el registro de supervisión Modal.")
            if (
                result["source_sha256"] != current["source_sha256"]
                or result["profile_id"] != current["profile_id"]
            ):
                raise ModalExecutionConflict(
                    "La caché verificada no corresponde al origen y perfil reservados."
                )
            if current["status"] == "recovered":
                raise ModalExecutionConflict("La ejecución ya quedó recuperada.")
            if current["status"] == "completed":
                raise ModalExecutionConflict("El resultado ya quedó completado.")
            result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                """
                UPDATE modal_executions
                SET status = 'recovered', phase = 'verified_cache_recovery',
                    result_json = ?, error_type = NULL, error_message = NULL, updated_at = ?
                WHERE execution_id = ?
                """,
                (result_json, now, execution_id),
            )
            self._append_event(
                connection,
                execution_id,
                "recovered",
                "verified_cache_recovery",
                details,
                now,
            )
            record = self.get(execution_id, connection=connection)
            assert record is not None
            return record

    def list_active(self) -> list[ModalExecution]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM modal_executions WHERE status IN ({placeholders}) ORDER BY created_at",
                tuple(sorted(ACTIVE_STATES)),
            ).fetchall()
            return [record for row in rows if (record := self._row(row)) is not None]

    def list_terminal_unreconciled(self) -> list[ModalExecution]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM modal_executions
                WHERE status IN ({placeholders}) AND legacy_reconciled = 0
                ORDER BY updated_at
                """,
                tuple(sorted(TERMINAL_STATES)),
            ).fetchall()
            return [record for row in rows if (record := self._row(row)) is not None]

    def mark_legacy_reconciled(self, execution_id: str, *, reason: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM modal_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError("No existe el registro de supervisión Modal.")
            if row["status"] not in TERMINAL_STATES:
                raise ModalExecutionConflict("Solo se reconcilian ejecuciones terminales.")
            connection.execute(
                """
                UPDATE modal_executions SET legacy_reconciled = 1, updated_at = ?
                WHERE execution_id = ?
                """,
                (now, execution_id),
            )
            self._append_event(
                connection,
                execution_id,
                row["status"],
                "legacy_ledger_reconciled",
                {"reason": reason},
                now,
            )

    def events(self, execution_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, status, phase, details_json, created_at
                FROM modal_execution_events WHERE execution_id = ? ORDER BY event_id
                """,
                (execution_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "status": row["status"],
                "phase": row["phase"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
