#!/usr/bin/env python3
"""Attach to and supervise detached Modal normalizations without submitting work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from video_pipeline.cost_controls import (
    MAX_JOB_BUDGET_USD,
    estimate_4k_normalization_budget,
)
from video_pipeline.modal_execution_store import (
    ModalExecution,
    ModalExecutionConflict,
    ModalExecutionStore,
)
from video_pipeline.modal_renderer import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    ModalRenderClient,
)
from video_pipeline.state import ProcessingLedger


STORE_PATH = Path("data/modal-execution-supervision.sqlite3")
LEGACY_LEDGER_PATH = Path("data/modal-normalization-4k-once.sqlite3")
POLL_INTERVAL_SECONDS = 20
HISTORY_DIR = Path("data/normalization-4k-history")
MAX_MODAL_EVIDENCE_BYTES = 10 * 1024 * 1024
MODAL_EVIDENCE_SUFFIXES = {".jpg", ".jpeg", ".json", ".log", ".png", ".txt", ".webp"}


def _source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_legacy_history_once(path: Path, event: dict[str, Any], execution_id: str) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                previous = json.loads(line)
            except json.JSONDecodeError:
                continue
            if previous.get("execution_id") == execution_id:
                return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def reconcile_legacy_terminal_records(
    store: ModalExecutionStore,
    *,
    legacy_ledger: Path = LEGACY_LEDGER_PATH,
    history_dir: Path = HISTORY_DIR,
) -> None:
    """Append terminal outcomes without changing or replacing prior attempt events."""
    for record in store.list_terminal_unreconciled():
        if not legacy_ledger.is_file():
            continue
        legacy = ProcessingLedger(legacy_ledger)
        current = legacy.get(record.source_sha256)
        if not current or int(current.get("attempts") or 0) != record.attempt_number:
            store.mark_legacy_reconciled(
                record.execution_id,
                reason="legacy_attempt_missing_or_superseded; prior rows preserved",
            )
            continue
        event_status = record.status
        if record.status in {"completed", "recovered"}:
            result = json.loads(record.result_json or "{}")
            output_path = result.get("output_path")
            if not output_path:
                continue
            legacy.mark_completed(record.source_sha256, output_path)
            event_status = "completed"
        elif record.status == "cancelled":
            legacy.update(
                record.source_sha256,
                status="interrupted",
                error=f"Modal cancelled; supervision record {record.execution_id}",
            )
            event_status = "interrupted"
        elif record.status == "failed":
            legacy.mark_failed(
                record.source_sha256,
                f"Modal failed; supervision record {record.execution_id}",
            )
        history_path = history_dir / f"{record.source_sha256}.jsonl"
        _append_legacy_history_once(
            history_path,
            {
                "attempt": record.attempt_number,
                "status": event_status,
                "source_sha256": record.source_sha256,
                "profile_id": record.profile_id,
                "execution_id": record.execution_id,
                "job_id": record.job_id,
                "function_call_id": record.function_call_id,
                "output": (
                    json.loads(record.result_json or "{}").get("output_path")
                    if record.result_json else None
                ),
                "error_type": record.error_type,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "supervisor_reconciled": True,
            },
            record.execution_id,
        )
        store.mark_legacy_reconciled(record.execution_id, reason=event_status)


def _read_legacy_attempts(database: Path, source_hash: str) -> tuple[int, str]:
    if not database.is_file():
        raise FileNotFoundError("No existe el ledger previo; no se modificó.")
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT attempts, status FROM processing_jobs WHERE source_sha256 = ?",
            (source_hash,),
        ).fetchone()
    if row is None:
        raise LookupError("No existe un registro de intento para este origen.")
    return int(row[0]), str(row[1])


def attach_existing_call(
    store: ModalExecutionStore,
    *,
    call_id: str,
    source_path: Path,
    output_path: Path,
    attempt_number: int,
    legacy_ledger: Path = LEGACY_LEDGER_PATH,
) -> tuple[ModalExecution, bool]:
    """Register a known call ID in the new store; never touches legacy attempts."""
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise FileNotFoundError("El vídeo fuente no existe o está vacío.")
    source_hash = _source_sha256(source_path)
    attempts, legacy_status = _read_legacy_attempts(legacy_ledger, source_hash)
    if (
        attempt_number < 1
        or attempt_number != attempts
        or attempt_number > 2
        or legacy_status not in {"processing", "interrupted", "failed"}
    ):
        raise ModalExecutionConflict("El intento indicado no coincide con el historial conservado.")
    estimate = estimate_4k_normalization_budget()
    if estimate.maximum_usd > MAX_JOB_BUDGET_USD:
        raise RuntimeError("El presupuesto interno bloquea la supervisión/recuperación.")
    execution_id = hashlib.sha256(
        f"{source_hash}\0{MASTER_4K_NORMALIZATION_PROFILE_ID}\0{attempt_number}\0{call_id}".encode()
    ).hexdigest()[:32]
    record, created = store.attach_existing_call(
        execution_id=execution_id,
        source_sha256=source_hash,
        profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
        attempt_number=attempt_number,
        function_call_id=call_id,
        source_path=source_path,
        output_path=output_path,
        budget_estimate_usd=estimate.maximum_usd,
        budget_limit_usd=MAX_JOB_BUDGET_USD,
    )
    return record, created


def _is_cancelled(exc: BaseException) -> bool:
    # Fail closed: AuthError and message text can contain "cancel" without
    # proving that the provider call itself reached a cancelled terminal state.
    return type(exc).__name__.lower() in {
        "inputcancellation",
        "cancelederror",
        "cancellederror",
        "generatorexit",
    }


def record_manual_modal_cancellation_evidence(
    store: ModalExecutionStore,
    *,
    call_id: str,
    evidence_file: Path,
    confirmed_status: str,
) -> tuple[ModalExecution, str, bool]:
    """Append a hashed, operator-reviewed dashboard artifact; never rewrites AuthError."""
    if confirmed_status != "Cancelled":
        raise ValueError("Confirme explícitamente el estado Cancelled mostrado por Modal.")
    if not call_id.startswith("fc-") or len(call_id) > 128:
        raise ValueError("ID de llamada Modal no válido.")
    path = evidence_file.expanduser().resolve()
    if (
        path.suffix.lower() not in MODAL_EVIDENCE_SUFFIXES
        or not path.is_file()
        or path.stat().st_size <= 0
        or path.stat().st_size > MAX_MODAL_EVIDENCE_BYTES
    ):
        raise ValueError("Adjunte una captura o registro de Modal válido (máximo 10 MiB).")
    record = store.get_by_call_id(call_id)
    if record is None:
        raise KeyError("No hay una ejecución persistida con ese ID de Modal.")
    evidence_sha256 = _source_sha256(path)
    reconciled, created = store.record_manual_modal_cancellation_evidence(
        record.execution_id,
        function_call_id=call_id,
        evidence_file_name=path.name,
        evidence_sha256=evidence_sha256,
    )
    return reconciled, evidence_sha256, created


def _is_transient_poll_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    return (
        isinstance(exc, (OSError, ConnectionError, TimeoutError))
        or "grpc" in module
        or "connection" in name
        or "temporar" in name
        or "unavailable" in name
    )


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "cache_verified": True,
        "source_sha256": result.source_sha256,
        "profile_id": result.profile_id,
        "output_path": str(result.output_path),
        "output_sha256": result.sha256,
        "output_size_bytes": result.size_bytes,
        "duration_seconds": result.duration_seconds,
        "profile": result.profile,
        "remote_processing_seconds": result.remote_processing_seconds,
        "cache_hit": result.cache_hit,
    }


def _recover_verified_cache(
    store: ModalExecutionStore,
    client: ModalRenderClient,
    record: ModalExecution,
    *,
    recovery_reason: str,
) -> ModalExecution | None:
    result = client.recover_cached_normalization(
        input_path=record.source_path,
        output_path=record.output_path,
        profile_id=record.profile_id,
    )
    if result is None:
        return None
    payload = _result_payload(result)
    if record.status in {"cancelled", "failed", "submission_unknown"}:
        return store.mark_recovered(
            record.execution_id,
            result=payload,
            details={"reason": recovery_reason, "worker_invoked": False},
        )
    return store.update(
        record.execution_id,
        status="completed",
        phase="verified_cache_result",
        result=payload,
        details={"reason": recovery_reason, "worker_invoked": False},
    )


def poll_execution(
    store: ModalExecutionStore,
    client: ModalRenderClient,
    execution_id: str,
) -> ModalExecution:
    """Poll one stored call ID; never creates, retries, or cancels a Modal input."""
    record = store.get(execution_id)
    if record is None:
        raise KeyError("No existe la ejecución solicitada.")
    if record.status in {"completed", "recovered"}:
        return record
    if record.status in {"failed", "cancelled"}:
        recovered = _recover_verified_cache(
            store,
            client,
            record,
            recovery_reason="reconcile_after_terminal_call_state",
        )
        return recovered or record
    if not record.function_call_id:
        try:
            recovered = _recover_verified_cache(
                store,
                client,
                record,
                recovery_reason="cache_check_without_persisted_call_id",
            )
            if recovered:
                return recovered
        except Exception:
            # Unknown submission remains blocked; never infer that it is safe to resubmit.
            pass
        if record.status == "submission_unknown":
            return record
        return store.update(
            execution_id,
            status="submission_unknown",
            phase="missing_function_call_id",
            details={"automatic_resubmit": False},
        )

    now = datetime.now(timezone.utc).isoformat()
    remote_phase: str | None = None
    remote_worker_code_version: str | None = None
    if record.job_id:
        try:
            progress = client.read_normalization_progress(record.job_id)
            if progress:
                remote_phase = str(progress.get("phase") or "")[:100] or None
                candidate_version = progress.get("worker_code_version")
                if isinstance(candidate_version, str) and len(candidate_version) <= 80:
                    remote_worker_code_version = candidate_version
        except Exception as exc:
            # A missing progress snapshot must not be treated as a failed conversion.
            store.update(
                execution_id,
                status="running",
                phase="progress_read_unavailable",
                details={"error_type": type(exc).__name__, "polled_at": now},
            )

    try:
        remote_result = client.poll_normalization_call(record.function_call_id)
    except BaseException as exc:
        if _is_cancelled(exc):
            try:
                recovered = _recover_verified_cache(
                    store,
                    client,
                    record,
                    recovery_reason="cache_check_after_modal_cancellation",
                )
                if recovered:
                    return recovered
            except Exception as cache_error:
                return store.update(
                    execution_id,
                    status="running",
                    phase="cancelled_cache_check_unavailable",
                    details={
                        "error_type": type(exc).__name__,
                        "cache_check_error_type": type(cache_error).__name__,
                        "remote_worker_code_version": remote_worker_code_version,
                        "automatic_resubmit": False,
                    },
                )
            return store.update(
                execution_id,
                status="cancelled",
                phase="modal_call_cancelled",
                error=exc,
                details={
                    "automatic_resubmit": False,
                    "remote_worker_code_version": remote_worker_code_version,
                },
            )
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            store.update(
                execution_id,
                status="running",
                phase="supervisor_interrupted",
                details={"error_type": type(exc).__name__, "remote_call_cancelled": False},
            )
            raise
        if _is_transient_poll_error(exc):
            return store.update(
                execution_id,
                status="running",
                phase="poll_connection_unavailable",
                details={"error_type": type(exc).__name__, "polled_at": now},
            )
        return store.update(
            execution_id,
            status="failed",
            phase="modal_call_failed",
            error=exc,
            details={"error_type": type(exc).__name__, "automatic_resubmit": False},
        )

    if remote_result is None:
        return store.update(
            execution_id,
            status="running",
            phase=remote_phase or "awaiting_modal_result",
            details={"polled_at": now, "remote_progress_phase": remote_phase},
        )
    if (
        remote_result.get("source_sha256") != record.source_sha256
        or remote_result.get("profile_id") != record.profile_id
        or (
            record.job_id is not None
            and remote_result.get("job_id") != record.job_id
        )
    ):
        return store.update(
            execution_id,
            status="failed",
            phase="remote_result_identity_mismatch",
            error=RuntimeError("El resultado Modal no coincide con la ejecución persistida."),
        )

    candidate_version = remote_result.get("worker_code_version")
    if isinstance(candidate_version, str) and len(candidate_version) <= 80:
        remote_worker_code_version = candidate_version
    store.update(
        execution_id,
        status="running",
        phase="remote_result_received",
        details={"remote_worker_code_version": remote_worker_code_version},
    )
    try:
        recovered = _recover_verified_cache(
            store,
            client,
            record,
            recovery_reason="completed_modal_call",
        )
    except Exception as exc:
        return store.update(
            execution_id,
            status="failed",
            phase="completed_call_result_verification_failed",
            error=exc,
            details={"automatic_resubmit": False},
        )
    if recovered is None:
        return store.update(
            execution_id,
            status="failed",
            phase="modal_result_without_verified_cache",
            error=RuntimeError("Modal terminó sin un manifiesto/proxy verificable."),
            details={"automatic_resubmit": False},
        )
    return recovered


def poll_once(store: ModalExecutionStore, client: ModalRenderClient) -> list[ModalExecution]:
    records = store.list_active()
    updated: list[ModalExecution] = []
    for record in records:
        updated.append(poll_execution(store, client, record.execution_id))
    reconcile_legacy_terminal_records(store)
    return updated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--attach-call-id", help="Registra un ID existente; no envía procesamiento.")
    modes.add_argument(
        "--record-modal-cancellation",
        metavar="FC_ID",
        help="Registra evidencia revisada del estado Cancelled; no consulta ni invoca Modal.",
    )
    modes.add_argument("--once", action="store_true", help="Consulta llamadas persistidas una vez.")
    modes.add_argument("--watch", action="store_true", help="Supervisa llamadas desde un servicio persistente.")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--attempt-number", type=int)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--confirm-modal-status", choices=["Cancelled"])
    parser.add_argument("--store", type=Path, default=STORE_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.record_modal_cancellation and not args.store.is_file():
        raise SystemExit(
            "No existe el almacén de supervisión indicado; se rechaza crear uno nuevo."
        )
    store = ModalExecutionStore(args.store)
    if args.record_modal_cancellation:
        if not args.evidence_file or args.confirm_modal_status != "Cancelled":
            raise SystemExit(
                "--record-modal-cancellation requiere --evidence-file y "
                "--confirm-modal-status Cancelled."
            )
        record, evidence_sha256, created = record_manual_modal_cancellation_evidence(
            store,
            call_id=args.record_modal_cancellation,
            evidence_file=args.evidence_file,
            confirmed_status=args.confirm_modal_status,
        )
        print(json.dumps({
            "recorded": created,
            "status_unchanged": record.status,
            "error_type_unchanged": record.error_type,
            "function_call_id": record.function_call_id,
            "evidence_sha256": evidence_sha256,
        }, ensure_ascii=False))
        return 0
    if args.attach_call_id:
        if not args.source or not args.output or args.attempt_number is None:
            raise SystemExit("--attach-call-id requiere --source, --output y --attempt-number.")
        record, created = attach_existing_call(
            store,
            call_id=args.attach_call_id,
            source_path=args.source,
            output_path=args.output,
            attempt_number=args.attempt_number,
        )
        print(json.dumps({"attached": created, "execution": record.as_dict()}, ensure_ascii=False))
        return 0

    client = ModalRenderClient()
    try:
        while True:
            records = poll_once(store, client)
            for record in records:
                print(json.dumps(record.as_dict(), ensure_ascii=False), flush=True)
            if not args.watch:
                return 0
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        # The Modal input is not cancelled; a later supervisor process can reattach.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
