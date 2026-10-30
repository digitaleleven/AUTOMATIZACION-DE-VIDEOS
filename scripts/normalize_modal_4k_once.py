#!/usr/bin/env python3
"""Run one budget-preflighted 4K/30 normalization in Modal; never renders or calls AI APIs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from video_pipeline.cost_controls import (
    MAX_4K_NORMALIZED_PROXY_GIB,
    MAX_JOB_BUDGET_USD,
    enforce_4k_normalization_budget,
)
from video_pipeline.media import probe_video
from video_pipeline.modal_execution_store import (
    ModalExecutionBudgetExceeded,
    ModalExecutionConflict,
    ModalExecutionStore,
    safe_error_message,
)
from video_pipeline.modal_renderer import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    ModalNormalizationResult,
    ModalNormalizationSubmission,
    ModalRenderClient,
    ModalSubmissionUnknownError,
)
from video_pipeline.state import (
    DuplicateProcessingError,
    ProcessingLedger,
    RetryNotAllowedError,
)


DEFAULT_SOURCE = Path(
    "data/incoming/44e426722ccc4f01aaaf8662298b638f.mov"
)
DEFAULT_OUTPUT = Path(
    "generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4"
)
LEDGER_PATH = Path("data/modal-normalization-4k-once.sqlite3")
HISTORY_DIR = Path("data/normalization-4k-history")
EXECUTION_STORE_PATH = Path("data/modal-execution-supervision.sqlite3")
THIRD_ATTEMPT_EXTRA_FREE_GIB = 1


class NormalizationInterrupted(BaseException):
    def __init__(self, signal_number: int):
        super().__init__(f"Recibida señal {signal_number}")
        self.signal_number = signal_number


@contextmanager
def record_termination_signals():
    previous_handlers = {}

    def interrupt(signum, _frame):
        raise NormalizationInterrupted(signum)

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    for signum in signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--execution-store", type=Path, default=EXECUTION_STORE_PATH)
    attempts = parser.add_mutually_exclusive_group()
    attempts.add_argument(
        "--retry-failed",
        action="store_true",
        help="autoriza un único segundo intento manual, solo si el primero quedó failed.",
    )
    attempts.add_argument(
        "--recover-only",
        action="store_true",
        help="consulta y recupera solo la caché existente de Modal; nunca llama al worker.",
    )
    attempts.add_argument(
        "--allow-third-attempt",
        action="store_true",
        help="puerta manual de una sola vez: exige hash confirmado e ID del intento 2 cancelado.",
    )
    attempts.add_argument(
        "--preflight-only",
        action="store_true",
        help="valida localmente archivo, perfil, espacio y coste; no conecta con Modal ni cambia estado.",
    )
    parser.add_argument("--confirm-source-sha256")
    parser.add_argument("--confirm-cancelled-call-id")
    parser.add_argument("--confirm-worker-code-version")
    return parser


def append_history(history_path: Path, event: dict[str, object]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def history_has_event(history_path: Path, *, attempt: int, status: str) -> bool:
    if not history_path.is_file():
        return False
    for line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("attempt") == attempt and event.get("status") == status:
            return True
    return False


def output_capacity_preflight(output_path: Path) -> dict[str, int | str]:
    """Check output location and conservative free-space reserve without creating files."""
    directory = output_path.parent
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(f"El directorio de salida no es accesible para escritura: {directory}")
    available = shutil.disk_usage(directory).free
    required = (MAX_4K_NORMALIZED_PROXY_GIB + THIRD_ATTEMPT_EXTRA_FREE_GIB) * 1024**3
    if available < required:
        raise RuntimeError(
            f"Espacio insuficiente en {directory}: disponibles {available} bytes; "
            f"se requieren al menos {required} bytes para salida y margen."
        )
    return {
        "checked_directory": str(directory),
        "available_bytes": available,
        "required_bytes": required,
    }


def _history_attempt_statuses(history_path: Path, attempt: int) -> set[str]:
    statuses: set[str] = set()
    if not history_path.is_file():
        return statuses
    for line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("attempt") == attempt and isinstance(event.get("status"), str):
            statuses.add(event["status"])
    return statuses


def verify_third_attempt_authorization(
    ledger: ProcessingLedger,
    execution_store: ModalExecutionStore,
    *,
    source_hash: str,
    confirmed_source_hash: str | None,
    confirmed_cancelled_call_id: str | None,
    history_path: Path,
) -> Any:
    """Require preserved attempts and either a polled cancellation or explicit evidence."""
    if confirmed_source_hash != source_hash:
        raise RetryNotAllowedError(
            "--allow-third-attempt requiere confirmar el SHA-256 exacto del original."
        )
    if not confirmed_cancelled_call_id or not confirmed_cancelled_call_id.startswith("fc-"):
        raise RetryNotAllowedError(
            "--allow-third-attempt requiere el ID fc- exacto del intento 2 cancelado."
        )
    previous = ledger.get(source_hash)
    if (
        int(previous.get("attempts") or 0) != 2
        or previous.get("status") not in {"failed", "interrupted"}
    ):
        raise RetryNotAllowedError(
            "El ledger debe confirmar dos intentos y estado failed/interrupted ya reconciliado."
        )
    if "failed" not in _history_attempt_statuses(history_path, 1):
        raise RetryNotAllowedError("El historial debe conservar el fallo registrado del intento 1.")
    attempt_two_statuses = _history_attempt_statuses(history_path, 2)
    if not attempt_two_statuses.intersection(
        {"started", "interrupted", "cancelled", "failed", "recovery_not_found"}
    ):
        raise RetryNotAllowedError("No se encontró el intento 2 en el historial JSONL.")
    record = execution_store.latest_for_source(
        source_hash,
        MASTER_4K_NORMALIZATION_PROFILE_ID,
    )
    call_matches = record is not None and record.function_call_id == confirmed_cancelled_call_id
    modal_cancellation_was_polled = (
        record is not None
        and record.attempt_number == 2
        and record.status == "cancelled"
        and call_matches
    )
    manual_evidence = False
    if (
        record is not None
        and record.attempt_number == 2
        and record.status == "failed"
        and record.error_type == "AuthError"
        and call_matches
    ):
        for event in execution_store.events(record.execution_id):
            details = event.get("details") or {}
            same_identity_and_hash = (
                event.get("status") == "failed"
                and details.get("provider") == "modal"
                and details.get("provider_status") == "cancelled"
                and details.get("function_call_id") == confirmed_cancelled_call_id
                and isinstance(details.get("evidence_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", details["evidence_sha256"])
            )
            manual_artifact_evidence = (
                event.get("phase") == "manual_modal_cancellation_evidence"
                and details.get("operator_confirmed") is True
                and isinstance(details.get("evidence_file_name"), str)
            )
            direct_modal_query_evidence = (
                event.get("phase") == "modal_status_observation"
                and details.get("observation_method")
                == "FunctionCall.from_id.get(timeout=0)"
                and details.get("exception_type") == "modal.exception.InputCancellation"
            )
            if (
                same_identity_and_hash
                and (manual_artifact_evidence or direct_modal_query_evidence)
            ):
                manual_evidence = True
                break
    if (
        record is None
        or record.attempt_number != 2
        or not (modal_cancellation_was_polled or manual_evidence)
    ):
        raise RetryNotAllowedError(
            "El intento 2 debe tener estado cancelado consultado a Modal o evidencia manual "
            "verificada para el mismo ID; AuthError por sí solo no basta."
        )
    return record


def claim_normalization_attempt(
    ledger: ProcessingLedger,
    *,
    source_hash: str,
    source_path: Path,
    retry_failed: bool,
    history_path: Path,
    allow_third_attempt: bool = False,
) -> dict[str, object]:
    previous = ledger.get(source_hash)
    if allow_third_attempt:
        if (
            not previous
            or int(previous.get("attempts") or 0) != 2
            or previous.get("status") not in {"failed", "interrupted"}
        ):
            raise RetryNotAllowedError(
                "El tercer intento controlado exige dos intentos previos y estado terminal reconciliado."
            )
        attempt = ledger.claim(
            source_hash,
            str(source_path),
            retry_failed=True,
            max_attempts=3,
            allow_third_attempt=True,
            edit_options={
                "mode": "normalize_only",
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            },
        )
        append_history(
            history_path,
            {
                "attempt": 3,
                "status": "started",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "manually_authorized": True,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return attempt

    if retry_failed:
        if (
            not previous
            or previous.get("status") != "failed"
            or int(previous.get("attempts") or 0) != 1
        ):
            raise RetryNotAllowedError(
                "--retry-failed solo permite un segundo intento cuando el primer intento está failed."
            )
        if not history_has_event(history_path, attempt=1, status="failed"):
            append_history(
                history_path,
                {
                    "attempt": 1,
                    "status": "failed",
                    "source_sha256": source_hash,
                    "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                    "recorded_from_ledger": True,
                    "recorded_at": previous.get("updated_at"),
                    "error": previous.get("error"),
                },
            )

    attempt = ledger.claim(
        source_hash,
        str(source_path),
        retry_failed=retry_failed,
        max_attempts=2,
        edit_options={
            "mode": "normalize_only",
            "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
        },
    )
    append_history(
        history_path,
        {
            "attempt": int(attempt["attempts"]),
            "status": "started",
            "source_sha256": source_hash,
            "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return attempt


def recover_interrupted_attempt(
    ledger: ProcessingLedger,
    client: ModalRenderClient,
    *,
    source_hash: str,
    source_path: Path,
    output_path: Path,
    history_path: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> Any | None:
    previous = ledger.get(source_hash)
    attempts = int(previous.get("attempts") or 0) if previous else 0
    if (
        not previous
        or previous.get("status") not in {"processing", "interrupted", "failed"}
        or attempts < 1
        or attempts > 2
    ):
        raise RetryNotAllowedError(
            "No hay un intento incompleto existente que se pueda reconciliar sin nuevo cómputo."
        )

    started = time.monotonic()
    result = client.recover_cached_normalization(
        input_path=source_path,
        output_path=output_path,
        profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
        progress_callback=progress_callback,
    )
    if result is None:
        append_history(
            history_path,
            {
                "attempt": attempts,
                "status": "recovery_not_found",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "total_seconds": round(time.monotonic() - started, 3),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "worker_invoked": False,
            },
        )
        return None

    ledger.mark_completed(source_hash, str(result.output_path))
    append_history(
        history_path,
        {
            "attempt": attempts,
            "status": "recovered",
            "source_sha256": source_hash,
            "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            "output": str(result.output_path),
            "output_sha256": result.sha256,
            "size_bytes": result.size_bytes,
            "profile": result.profile,
            "remote_processing_seconds": result.remote_processing_seconds,
            "total_seconds": round(time.monotonic() - started, 3),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "worker_invoked": False,
        },
    )
    return result


def _is_interruption(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return True
    return "cancel" in type(exc).__name__.lower() or "cancel" in str(exc).lower()


def record_terminal_outcome(
    ledger: ProcessingLedger,
    *,
    source_hash: str,
    history_path: Path,
    attempt_number: int,
    job_id: str,
    phase: str,
    started: float,
    exc: BaseException,
) -> dict[str, object]:
    error = f"{type(exc).__name__}: {safe_error_message(exc)}"
    interrupted = _is_interruption(exc)
    current = ledger.get(source_hash)
    if current.get("status") != "completed":
        if interrupted:
            ledger.update(source_hash, status="interrupted", error=error[:1600])
        else:
            ledger.mark_failed(source_hash, error)
    status = "interrupted" if interrupted else "failed"
    event = {
        "attempt": attempt_number,
        "status": status if current.get("status") != "completed" else "completed",
        "source_sha256": source_hash,
        "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
        "job_id": job_id,
        "phase": phase,
        "total_seconds": round(time.monotonic() - started, 3),
        "error": error,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    append_history(history_path, event)
    return event


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()

    if args.recover_only and not LEDGER_PATH.is_file():
        raise SystemExit("Bloqueado: no existe el ledger previo; no se creará ni modificará.")
    if not source.is_file() or source.stat().st_size <= 0:
        raise SystemExit(f"Bloqueado: no existe el vídeo fuente: {source}")
    if output.exists():
        raise SystemExit(f"Bloqueado: la salida ya existe y no se sobrescribirá: {output}")

    try:
        output_capacity = output_capacity_preflight(output)
    except RuntimeError as exc:
        raise SystemExit(f"Bloqueado: {exc}") from None

    video = probe_video(source)
    display_width, display_height = video.width, video.height
    if abs(int(round(video.rotation or 0))) % 180 == 90:
        display_width, display_height = display_height, display_width
    if (
        display_width < 2160
        or display_height < 3840
        or abs(video.duration - 59.98) > 0.5
        or video.video_codec.lower() not in {"hevc", "h265"}
    ):
        raise SystemExit(
            "Bloqueado: la fuente debe ser el original HEVC vertical 2160×3840 de 59,98 s."
        )
    is_hlg = (
        video.color_range == "tv"
        and video.color_primaries == "bt2020"
        and video.color_transfer == "arib-std-b67"
        and video.color_space == "bt2020nc"
    )
    is_sdr_bt709 = (
        video.color_range in {"tv", "pc"}
        and video.color_primaries == "bt709"
        and video.color_transfer == "bt709"
        and video.color_space == "bt709"
    )
    if not (is_hlg or is_sdr_bt709):
        raise SystemExit("Bloqueado: color de origen no reconocido como HLG BT.2020 o SDR BT.709.")

    budget = None if args.recover_only else enforce_4k_normalization_budget()
    source_hash = sha256_file(source)
    if args.allow_third_attempt and (
        args.confirm_source_sha256 != source_hash
        or not args.confirm_cancelled_call_id
        or not args.confirm_cancelled_call_id.startswith("fc-")
        or not args.confirm_worker_code_version
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", args.confirm_worker_code_version)
    ):
        raise SystemExit(
            "Bloqueado: el tercer intento requiere SHA-256, ID fc- cancelado y versión Modal confirmados."
        )
    print(
        json.dumps(
            {
                "preflight": "pass",
                "operation": (
                    "preflight_only"
                    if args.preflight_only
                    else "recover_only"
                    if args.recover_only
                    else "normalize"
                ),
                "static_maximum_usd": budget.maximum_usd if budget else None,
                "billing_cap_guaranteed": False,
                "source_sha256": source_hash,
                "source_video_codec": video.video_codec,
                "source_duration_seconds": video.duration,
                "source_dimensions": [video.width, video.height],
                "source_display_dimensions": [display_width, display_height],
                "source_color_profile": {
                    "range": video.color_range,
                    "primaries": video.color_primaries,
                    "transfer": video.color_transfer,
                    "matrix": video.color_space,
                },
                "normalization_profile": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "output_capacity": output_capacity,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.preflight_only:
        return 0

    if args.allow_third_attempt and (
        not LEDGER_PATH.is_file() or not args.execution_store.is_file()
    ):
        raise SystemExit(
            "Bloqueado: para autorizar el tercer intento deben existir ambos registros persistentes previos."
        )
    ledger = ProcessingLedger(LEDGER_PATH)
    history_path = HISTORY_DIR / f"{source_hash}.jsonl"
    started = time.monotonic()
    if args.recover_only:
        current = ledger.get(source_hash)
        attempt_number = int(current.get("attempts") or 0) if current else 0

        def recovery_progress(phase: str) -> None:
            event = {
                "attempt": attempt_number,
                "status": "recovery_progress",
                "phase": phase,
                "source_sha256": source_hash,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "worker_invoked": False,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            append_history(history_path, event)
            print(json.dumps(event, ensure_ascii=False), flush=True)

        try:
            with record_termination_signals():
                client = ModalRenderClient()
                result = recover_interrupted_attempt(
                    ledger,
                    client,
                    source_hash=source_hash,
                    source_path=source,
                    output_path=output,
                    history_path=history_path,
                    progress_callback=recovery_progress,
                )
        except RetryNotAllowedError as exc:
            raise SystemExit(f"Bloqueado: {exc}") from None
        except BaseException as exc:
            error = f"{type(exc).__name__}: {safe_error_message(exc)}"
            event = {
                "attempt": attempt_number,
                "status": "recovery_check_interrupted" if _is_interruption(exc) else "recovery_check_failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "total_seconds": round(time.monotonic() - started, 3),
                "error": error,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "worker_invoked": False,
            }
            append_history(history_path, event)
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if isinstance(exc, NormalizationInterrupted):
                return 128 + exc.signal_number
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        if result is None:
            print(
                json.dumps(
                    {
                        "status": "not_recovered",
                        "reason": "No se encontró un manifiesto de caché válido en Modal.",
                        "worker_invoked": False,
                        "attempts_unchanged": True,
                        "total_seconds": round(time.monotonic() - started, 3),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "recovered",
                    "output": str(result.output_path),
                    "output_sha256": result.sha256,
                    "profile": result.profile,
                    "remote_processing_seconds": result.remote_processing_seconds,
                    "worker_invoked": False,
                    "attempts_unchanged": True,
                    "total_seconds": round(time.monotonic() - started, 3),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    current = ledger.get(source_hash)
    if (
        current
        and int(current.get("attempts") or 0) >= 2
        and current.get("status") != "completed"
        and not args.allow_third_attempt
    ):
        raise SystemExit(
            "Bloqueado: ya se consumieron los dos intentos. Use --recover-only para consultar la caché; "
            "no se iniciará otro trabajo."
        )
    execution_store = ModalExecutionStore(args.execution_store)
    existing_execution = execution_store.latest_for_source(
        source_hash,
        MASTER_4K_NORMALIZATION_PROFILE_ID,
    )
    permitted_store_retry = bool(
        (args.retry_failed or args.allow_third_attempt)
        and existing_execution
        and existing_execution.status in {"failed", "cancelled"}
        and existing_execution.attempt_number + 1
        == int((current or {}).get("attempts") or 0) + 1
    )
    if existing_execution and not permitted_store_retry:
        raise SystemExit(
            "Bloqueado: ya existe un registro de supervisión para este origen/perfil. "
            "Consúltelo con scripts/modal_normalization_supervisor.py; no se reenviará."
        )

    client = ModalRenderClient()
    cached_result = client.recover_cached_normalization(
        input_path=source,
        output_path=output,
        profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
    )
    if cached_result is not None:
        prior_attempts = int(current.get("attempts") or 0) if current else 0
        record = existing_execution
        if record is None:
            record, created = execution_store.reserve(
                execution_id=uuid.uuid4().hex,
                source_sha256=source_hash,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                job_id=uuid.uuid4().hex,
                attempt_number=max(1, prior_attempts),
                source_path=source,
                output_path=output,
                budget_estimate_usd=budget.maximum_usd,
                budget_limit_usd=MAX_JOB_BUDGET_USD,
            )
            if not created:
                raise SystemExit("Bloqueado: la caché se detectó junto a una ejecución ya registrada.")
        recovered_payload = {
            "cache_verified": True,
            "source_sha256": cached_result.source_sha256,
            "profile_id": cached_result.profile_id,
            "output_path": str(cached_result.output_path),
            "output_sha256": cached_result.sha256,
            "output_size_bytes": cached_result.size_bytes,
            "duration_seconds": cached_result.duration_seconds,
            "profile": cached_result.profile,
            "remote_processing_seconds": cached_result.remote_processing_seconds,
            "cache_hit": True,
        }
        if record.status not in {"completed", "recovered"}:
            execution_store.mark_recovered(
                record.execution_id,
                result=recovered_payload,
                details={"worker_invoked": False, "attempts_unchanged": True},
            )
        if current:
            ledger.mark_completed(source_hash, str(output))
            append_history(
                history_path,
                {
                    "attempt": prior_attempts,
                    "status": "recovered",
                    "source_sha256": source_hash,
                    "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                    "execution_id": record.execution_id,
                    "output": str(output),
                    "output_sha256": cached_result.sha256,
                    "worker_invoked": False,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        print(
            json.dumps(
                {
                    "status": "recovered",
                    "execution_id": record.execution_id,
                    "output": str(output),
                    "output_sha256": cached_result.sha256,
                    "worker_invoked": False,
                    "attempts_unchanged": True,
                    "cost_estimate_usd": budget.maximum_usd,
                    "billing_cap_guaranteed": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    if args.allow_third_attempt:
        if args.retry_failed or args.recover_only or args.preflight_only:
            raise SystemExit("Bloqueado: el gate del tercer intento no se combina con otros modos.")
        try:
            verify_third_attempt_authorization(
                ledger,
                execution_store,
                source_hash=source_hash,
                confirmed_source_hash=args.confirm_source_sha256,
                confirmed_cancelled_call_id=args.confirm_cancelled_call_id,
                history_path=history_path,
            )
        except RetryNotAllowedError as exc:
            raise SystemExit(f"Bloqueado: {exc}") from None

    job_id = uuid.uuid4().hex
    execution_id = uuid.uuid4().hex
    if args.allow_third_attempt:
        try:
            execution_record, created = execution_store.reserve(
                execution_id=execution_id,
                source_sha256=source_hash,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                job_id=job_id,
                attempt_number=3,
                source_path=source,
                output_path=output,
                budget_estimate_usd=budget.maximum_usd,
                budget_limit_usd=MAX_JOB_BUDGET_USD,
                allow_manual_retry=True,
            )
            if not created:
                raise ModalExecutionConflict("El intento 3 ya estaba reservado; no se reenviará.")
        except (ModalExecutionConflict, ModalExecutionBudgetExceeded) as exc:
            raise SystemExit(f"Bloqueado: {exc}") from None
        try:
            attempt = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source,
                retry_failed=True,
                history_path=history_path,
                allow_third_attempt=True,
            )
        except (DuplicateProcessingError, RetryNotAllowedError) as exc:
            execution_store.update(
                execution_id,
                status="failed",
                phase="attempt_claim_rejected",
                error=exc,
                details={"worker_invoked": False, "automatic_retry": False},
            )
            raise SystemExit(f"Bloqueado: {exc}") from None
    else:
        try:
            attempt = claim_normalization_attempt(
                ledger,
                source_hash=source_hash,
                source_path=source,
                retry_failed=args.retry_failed,
                history_path=history_path,
            )
        except (DuplicateProcessingError, RetryNotAllowedError) as exc:
            raise SystemExit(f"Bloqueado: {exc}") from None
        execution_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        try:
            execution_record, created = execution_store.reserve(
                execution_id=execution_id,
                source_sha256=source_hash,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                job_id=job_id,
                attempt_number=int(attempt["attempts"]),
                source_path=source,
                output_path=output,
                budget_estimate_usd=budget.maximum_usd,
                budget_limit_usd=MAX_JOB_BUDGET_USD,
            )
            if not created:
                raise ModalExecutionConflict("La ejecución ya estaba reservada.")
        except (ModalExecutionConflict, ModalExecutionBudgetExceeded) as exc:
            raise SystemExit(f"Bloqueado: {exc}") from None

    started = time.monotonic()
    current_phase = "starting"

    def progress(phase: str) -> None:
        nonlocal current_phase
        current_phase = phase
        execution_store.update(
            execution_id,
            status="submitting",
            phase=phase,
            details={
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "attempt_number": int(attempt["attempts"]),
                "budget_estimate_usd": budget.maximum_usd,
            },
        )
        event = {
            "attempt": int(attempt["attempts"]),
            "status": "progress",
            "phase": phase,
            "source_sha256": source_hash,
            "job_id": job_id,
            "execution_id": execution_id,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        append_history(history_path, event)
        print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        with record_termination_signals():
            execution_store.update(
                execution_id,
                status="submitting",
                phase="reserved_for_detached_submission",
                details={"job_id": job_id},
            )
            progress("submitting_normalization")
            submission = client.submit_normalization_detached(
                job_id=job_id,
                input_path=source,
                output_path=output,
                profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
                check_cache=False,
                expected_worker_code_version=args.confirm_worker_code_version,
                on_phase=progress,
                on_submitted=lambda call_id: execution_store.update(
                    execution_id,
                    status="submitted",
                    phase="modal_call_id_persisted",
                    function_call_id=call_id,
                    details={"job_id": job_id},
                ),
            )
        if isinstance(submission, ModalNormalizationResult):
            raise RuntimeError(
                "La caché apareció tras la reserva. Use --recover-only; no se inició un nuevo worker."
            )
        if not isinstance(submission, ModalNormalizationSubmission):
            raise RuntimeError("Modal no devolvió un identificador de ejecución supervisable.")
        print(
            json.dumps(
                {
                    "status": "submitted",
                    "job_id": job_id,
                    "execution_id": execution_id,
                    "function_call_id": submission.function_call_id,
                    "total_seconds": round(time.monotonic() - started, 3),
                    "output": str(output),
                    "cost_estimate_usd": budget.maximum_usd,
                    "billing_cap_guaranteed": False,
                    "supervision": "independent_service_required",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except ModalSubmissionUnknownError as exc:
        event = {
            "attempt": int(attempt["attempts"]),
            "status": "submission_unknown",
            "source_sha256": source_hash,
            "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
            "job_id": job_id,
            "execution_id": execution_id,
            "function_call_id": exc.function_call_id,
            "phase": current_phase,
            "error_type": type(exc).__name__,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            execution_store.update(
                execution_id,
                status="submission_unknown",
                phase="modal_submission_reply_unknown",
                function_call_id=exc.function_call_id,
                error=exc,
                details={"automatic_resubmit": False},
            )
        finally:
            append_history(history_path, event)
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return 1
    except BaseException as exc:
        persisted_submission = execution_store.get(execution_id)
        if persisted_submission and persisted_submission.function_call_id:
            execution_store.update(
                execution_id,
                status="submitted",
                phase="caller_interrupted_after_call_id_persisted",
                function_call_id=persisted_submission.function_call_id,
                details={
                    "error_type": type(exc).__name__,
                    "remote_call_cancelled": False,
                    "automatic_resubmit": False,
                },
            )
            event = {
                "attempt": int(attempt["attempts"]),
                "status": "caller_interrupted_after_submission",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "job_id": job_id,
                "execution_id": execution_id,
                "function_call_id": persisted_submission.function_call_id,
                "error_type": type(exc).__name__,
                "remote_call_cancelled": False,
                "automatic_resubmit": False,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            append_history(history_path, event)
            print(json.dumps(event, ensure_ascii=False), flush=True)
            if isinstance(exc, NormalizationInterrupted):
                return 128 + exc.signal_number
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        try:
            execution_store.update(
                execution_id,
                status="cancelled" if _is_interruption(exc) else "failed",
                phase=current_phase,
                error=exc,
                details={"remote_call_cancelled": False},
            )
        except Exception:
            pass
        event = record_terminal_outcome(
            ledger,
            source_hash=source_hash,
            history_path=history_path,
            attempt_number=int(attempt["attempts"]),
            job_id=job_id,
            phase=current_phase,
            started=started,
            exc=exc,
        )
        event["execution_id"] = execution_id
        print(
            json.dumps(event, ensure_ascii=False),
            flush=True,
        )
        if isinstance(exc, NormalizationInterrupted):
            return 128 + exc.signal_number
        if isinstance(exc, KeyboardInterrupt):
            return 130
        if isinstance(exc, SystemExit):
            return 1
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
