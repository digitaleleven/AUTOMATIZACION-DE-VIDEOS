#!/usr/bin/env python3
"""Run one budget-preflighted 4K/30 normalization in Modal; never renders or calls AI APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone

from video_pipeline.cost_controls import enforce_4k_normalization_budget
from video_pipeline.media import probe_video
from video_pipeline.modal_renderer import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    ModalRenderClient,
)
from video_pipeline.state import ProcessingLedger, RetryNotAllowedError


DEFAULT_SOURCE = Path(
    "data/incoming/44e426722ccc4f01aaaf8662298b638f.mov"
)
DEFAULT_OUTPUT = Path(
    "generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4"
)
LEDGER_PATH = Path("data/modal-normalization-4k-once.sqlite3")
HISTORY_DIR = Path("data/normalization-4k-history")


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
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="autoriza un único segundo intento manual, solo si el primero quedó failed.",
    )
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


def claim_normalization_attempt(
    ledger: ProcessingLedger,
    *,
    source_hash: str,
    source_path: Path,
    retry_failed: bool,
    history_path: Path,
) -> dict[str, object]:
    previous = ledger.get(source_hash)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()

    if not source.is_file() or source.stat().st_size <= 0:
        raise SystemExit(f"Bloqueado: no existe el vídeo fuente: {source}")
    if output.exists():
        raise SystemExit(f"Bloqueado: la salida ya existe y no se sobrescribirá: {output}")

    video = probe_video(source)
    display_width, display_height = video.width, video.height
    if abs(int(round(video.rotation or 0))) % 180 == 90:
        display_width, display_height = display_height, display_width
    if (
        display_width < 2160
        or display_height < 3840
        or abs(video.duration - 59.98) > 0.5
    ):
        raise SystemExit(
            "Bloqueado: la fuente debe ser el original vertical 2160×3840 de 59,98 s."
        )

    budget = enforce_4k_normalization_budget()
    print(
        json.dumps(
            {
                "preflight": "pass",
                "static_maximum_usd": budget.maximum_usd,
                "billing_cap_guaranteed": False,
                "source_sha256": sha256_file(source),
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
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    source_hash = sha256_file(source)
    ledger = ProcessingLedger(LEDGER_PATH)
    history_path = HISTORY_DIR / f"{source_hash}.jsonl"
    try:
        attempt = claim_normalization_attempt(
            ledger,
            source_hash=source_hash,
            source_path=source,
            retry_failed=args.retry_failed,
            history_path=history_path,
        )
    except RetryNotAllowedError as exc:
        raise SystemExit(f"Bloqueado: {exc}") from None
    job_id = uuid.uuid4().hex
    started = time.monotonic()
    client = ModalRenderClient()
    try:
        result = client.normalize_input(
            job_id=job_id,
            input_path=source,
            render_source_path=source,
            output_path=output,
            profile_id=MASTER_4K_NORMALIZATION_PROFILE_ID,
        )
        expected = {
            "width": 2160,
            "height": 3840,
            "fps": 30,
            "codec": "h264",
            "pixel_format": "yuv420p",
            "color_range": "tv",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
        }
        if any(result.profile.get(key) != value for key, value in expected.items()):
            raise RuntimeError("FFprobe no confirmó el perfil 4K/30 SDR BT.709 esperado.")
        ledger.mark_completed(source_hash, str(output))
        append_history(
            history_path,
            {
                "attempt": int(attempt["attempts"]),
                "status": "completed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "job_id": job_id,
                "output": str(output),
                "output_sha256": result.sha256,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        client.cleanup_staged_job(job_id)
        print(
            json.dumps(
                {
                    "status": "verified",
                    "job_id": job_id,
                    "cache_hit": result.cache_hit,
                    "total_seconds": round(time.monotonic() - started, 3),
                    "remote_processing_seconds": result.remote_processing_seconds,
                    "output": str(output),
                    "output_size_bytes": result.size_bytes,
                    "output_sha256": result.sha256,
                    "profile": result.profile,
                    "capabilities": result.capabilities,
                    "cost_estimate_usd": budget.maximum_usd,
                    "billing_cap_guaranteed": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:1200]}"
        ledger.mark_failed(source_hash, error)
        append_history(
            history_path,
            {
                "attempt": int(attempt["attempts"]),
                "status": "failed",
                "source_sha256": source_hash,
                "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
                "job_id": job_id,
                "error": error,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "job_id": job_id,
                    "total_seconds": round(time.monotonic() - started, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1200],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
