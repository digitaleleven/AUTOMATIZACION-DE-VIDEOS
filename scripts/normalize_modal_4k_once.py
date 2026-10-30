#!/usr/bin/env python3
"""Run one budget-preflighted 4K/30 normalization in Modal; never renders or calls AI APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from video_pipeline.cost_controls import enforce_4k_normalization_budget
from video_pipeline.media import probe_video
from video_pipeline.modal_renderer import (
    MASTER_4K_NORMALIZATION_PROFILE_ID,
    ModalRenderClient,
)
from video_pipeline.state import ProcessingLedger


DEFAULT_SOURCE = Path(
    "data/incoming/44e426722ccc4f01aaaf8662298b638f.mov"
)
DEFAULT_OUTPUT = Path(
    "generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4"
)
LEDGER_PATH = Path("data/modal-normalization-4k-once.sqlite3")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
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
    ledger.claim(
        source_hash,
        str(source),
        max_attempts=1,
        edit_options={
            "mode": "normalize_only",
            "profile_id": MASTER_4K_NORMALIZATION_PROFILE_ID,
        },
    )
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
        ledger.mark_failed(source_hash, f"{type(exc).__name__}: {str(exc)[:1200]}")
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
