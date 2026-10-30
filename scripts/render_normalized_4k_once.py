#!/usr/bin/env python3
"""One-shot MATRIX render using the already-normalized 4K source on DigitalOcean."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any, Callable

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"


def load_project_environment(env_path: str | Path = DOTENV_PATH) -> bool:
    """Load project .env without logging values or overriding service environment."""
    return load_dotenv(dotenv_path=Path(env_path), override=False, verbose=False)


# Load secrets before importing provider clients. Existing process/systemd values win.
load_project_environment()

from video_pipeline.cost_controls import MAX_JOB_BUDGET_USD, enforce_job_budget
from video_pipeline.gemini_video import analyze_video
from video_pipeline.media import detect_silence_cut_suggestions, probe_video
from video_pipeline.modal_pipeline import process_local_video_modal
from video_pipeline.modal_renderer import (
    MAX_SOURCE_BYTES,
    ModalRenderClient,
    modal_configuration_status,
)
from video_pipeline.r2_storage import configuration_status as r2_configuration_status
from video_pipeline.r2_storage import upload_mp4 as upload_mp4_to_r2
from video_pipeline.state import DuplicateProcessingError, ProcessingLedger
from video_pipeline.style import VideoStyle, load_video_style, validate_output_profile
from video_pipeline.subtitles import build_subtitle_segments
from video_pipeline.transcription import transcribe_video
from video_pipeline.webapp import _prepare_modal_render_plan

logger = logging.getLogger("matrix.render_normalized_4k_once")

SOURCE_PATH = Path("generated-artifacts/normalization-4k/IMG_1254-2160x3840-30.mp4")
SOURCE_SHA256 = "ca31123c1dd1b72cfddfec558a3af9f3b7e1b8fdc7d02dcc271feafe7868f4c6"
LEDGER_PATH = Path("data/modal_processing.sqlite3")
PANEL_DATABASE_PATH = Path("data/processing.sqlite3")
OUTPUT_DIR = Path("outputs")
WORK_ROOT = Path("data/normalized-render-once")
EXPECTED_DURATION_SECONDS = 59.98
DURATION_TOLERANCE_SECONDS = 0.5

EDIT_OPTIONS: dict[str, Any] = {
    "renderer_backend": "modal",
    "render_mode": "full",
    "subtitle_mode": "dynamic",
    "output_profile": "2160p",
    "output_width": 2160,
    "output_height": 3840,
    "output_fps": 30,
    "output_quality": "high",
    "render_settings": {
        "auto_zoom": "smooth",
        "audio": {"original_volume": 1.0, "noise_reduction": False},
    },
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_audio_profile(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout)
    return next(
        (
            stream for stream in payload.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        {},
    )


def _verify_full_decode(path: Path, duration: float) -> None:
    """Read every video and audio packet locally without writing or re-encoding."""
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=max(300, min(1800, int(duration * 20))),
    )


def _validate_source(
    source_path: str | Path = SOURCE_PATH,
    *,
    expected_sha256: str = SOURCE_SHA256,
) -> tuple[Path, Any, dict[str, Any]]:
    source = Path(source_path).expanduser().resolve()
    if source.suffix.lower() != ".mp4" or not source.is_file():
        raise FileNotFoundError("No se encontró el MP4 normalizado esperado.")
    size = source.stat().st_size
    if size <= 0 or size > min(MAX_SOURCE_BYTES, 2 * 1024**3):
        raise ValueError("El MP4 supera el tamaño admitido para análisis/render en Modal.")

    source_hash = _sha256_file(source)
    if source_hash != expected_sha256:
        raise ValueError("El SHA-256 no coincide con el MP4 4K autorizado; no se procesará.")

    video = probe_video(source)
    try:
        validate_output_profile(
            2160,
            3840,
            30,
            source_width=video.width,
            source_height=video.height,
        )
    except ValueError as exc:
        raise ValueError(f"La fuente no cumple el perfil máster 4K: {exc}") from None
    if (
        video.width != 2160
        or video.height != 3840
        or abs(video.fps - 30) > 0.05
        or abs(video.duration - EXPECTED_DURATION_SECONDS) > DURATION_TOLERANCE_SECONDS
        or video.video_codec != "h264"
        or video.pixel_format != "yuv420p"
        or video.color_range != "tv"
        or video.color_primaries != "bt709"
        or video.color_transfer != "bt709"
        or video.color_space != "bt709"
        or not video.has_audio
        or video.audio_codec != "aac"
    ):
        raise ValueError("FFprobe rechazó el perfil H.264/AAC 4K/30 SDR BT.709 esperado.")
    audio = _probe_audio_profile(source)
    if (
        audio.get("codec_name") != "aac"
        or str(audio.get("sample_rate")) != "48000"
        or int(audio.get("channels") or 0) != 2
    ):
        raise ValueError("La pista de audio debe ser AAC estéreo a 48 kHz.")
    _verify_full_decode(source, video.duration)
    return source, video, {
        "sha256": source_hash,
        "size_bytes": size,
        "duration_seconds": video.duration,
        "width": video.width,
        "height": video.height,
        "fps": video.fps,
        "video_codec": video.video_codec,
        "audio_codec": video.audio_codec,
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "pixel_format": video.pixel_format,
        "color_range": video.color_range,
        "color_primaries": video.color_primaries,
        "color_transfer": video.color_transfer,
        "color_space": video.color_space,
        "full_decode_verified": True,
    }


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_new_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _existing_panel_attempts(
    database_path: str | Path = PANEL_DATABASE_PATH,
) -> list[dict[str, str]]:
    """Read the panel history without opening its mutating DashboardStore constructor."""
    database = Path(database_path).expanduser().resolve()
    if not database.is_file():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"dashboard_uploads", "dashboard_jobs"}.issubset(tables):
            return []
        rows = connection.execute(
            """
            SELECT jobs.id, jobs.status, jobs.phase, uploads.source_sha256
            FROM dashboard_jobs AS jobs
            JOIN dashboard_uploads AS uploads ON uploads.id = jobs.source_id
            WHERE lower(uploads.original_filename) = lower(?)
            ORDER BY jobs.created_at
            """,
            ("IMG_1254.MOV",),
        ).fetchall()
        return [
            {
                "job_id": str(row["id"]),
                "status": str(row["status"]).lower(),
                "phase": str(row["phase"]).lower(),
                "source_sha256": str(row["source_sha256"]),
            }
            for row in rows
        ]
    finally:
        connection.close()


def _existing_modal_ledger_attempts(
    database_path: str | Path,
    source_keys: set[str],
) -> list[dict[str, Any]]:
    """Read only the relevant idempotency rows without migrating or creating the ledger."""
    database = Path(database_path).expanduser().resolve()
    if not database.is_file() or not source_keys:
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'processing_jobs'"
        ).fetchone()
        if not table:
            return []
        placeholders = ",".join("?" for _ in source_keys)
        rows = connection.execute(
            f"""SELECT source_sha256, status, attempts
                FROM processing_jobs
                WHERE source_sha256 IN ({placeholders})""",
            tuple(sorted(source_keys)),
        ).fetchall()
        return [
            {
                "source_sha256": str(row["source_sha256"]),
                "status": str(row["status"]).lower(),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]
    finally:
        connection.close()


def _prior_attempt_blockers(
    panel_attempts: list[dict[str, str]],
    ledger_attempts: list[dict[str, Any]],
    *,
    normalized_source_key: str,
    retry_failed: bool = False,
) -> tuple[list[str], int]:
    """Allow one explicit retry of a single failed normalized-source attempt."""
    blockers: list[str] = []
    failed_panel_attempts = 0
    retry_failure_found = False
    for attempt in panel_attempts:
        status = attempt["status"]
        phase = attempt["phase"]
        uncertain_phases = {
            "interrupted",
            "submission_unknown",
            "cancelled",
            "cancelled_unknown",
        }
        if status == "failed" and phase not in uncertain_phases and "unknown" not in phase:
            failed_panel_attempts += 1
        elif status == "failed":
            blockers.append("panel_job_failed_unreconciled")
        else:
            blockers.append(f"panel_job_{status or 'unknown'}")

    for attempt in ledger_attempts:
        status = attempt["status"]
        if attempt["source_sha256"] == normalized_source_key:
            attempts = int(attempt.get("attempts") or 0)
            if status != "failed":
                blockers.append(f"normalized_source_{status or 'unknown'}")
            elif retry_failed and attempts == 1:
                retry_failure_found = True
            elif retry_failed:
                blockers.append("normalized_retry_limit_reached")
            else:
                blockers.append("normalized_source_already_claimed")
        elif status != "failed":
            blockers.append(f"prior_modal_ledger_{status or 'unknown'}")

    if retry_failed and not retry_failure_found:
        blockers.append("retry_failed_requires_single_failed_ledger_attempt")
    return sorted(set(blockers)), failed_panel_attempts


def run_normalized_render_once(
    *,
    execute: bool = False,
    retry_failed: bool = False,
    source_path: str | Path = SOURCE_PATH,
    expected_sha256: str = SOURCE_SHA256,
    ledger_path: str | Path = LEDGER_PATH,
    panel_database_path: str | Path = PANEL_DATABASE_PATH,
    output_dir: str | Path = OUTPUT_DIR,
    work_root: str | Path = WORK_ROOT,
    authorizations: dict[str, bool] | None = None,
    preflight_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Preflight by default; submit a single full render only when explicitly authorized."""
    source, video, source_report = _validate_source(
        source_path,
        expected_sha256=expected_sha256,
    )
    normalized_source_key = hashlib.sha256(
        f"{source_report['sha256']}:full".encode("utf-8")
    ).hexdigest()
    previous_attempts = _existing_panel_attempts(panel_database_path)
    source_hashes = {attempt["source_sha256"] for attempt in previous_attempts}
    ledger_keys = {
        hashlib.sha256(f"{source_hash}:full".encode("utf-8")).hexdigest()
        for source_hash in source_hashes
    }
    ledger_keys.add(normalized_source_key)
    previous_ledger_attempts = _existing_modal_ledger_attempts(ledger_path, ledger_keys)
    duplicate_blockers, failed_panel_attempts = _prior_attempt_blockers(
        previous_attempts,
        previous_ledger_attempts,
        normalized_source_key=normalized_source_key,
        retry_failed=retry_failed,
    )
    budget = enforce_job_budget(video.duration)
    if budget.maximum_usd > MAX_JOB_BUDGET_USD:
        raise RuntimeError("La estimación conservadora supera el límite interno.")

    modal_status = modal_configuration_status()
    r2_status = r2_configuration_status()
    blockers = []
    if not os.getenv("OPENAI_API_KEY"):
        blockers.append("OPENAI_API_KEY no configurada")
    if not os.getenv("GEMINI_API_KEY"):
        blockers.append("GEMINI_API_KEY no configurada")
    if not modal_status["sdk_installed"] or not modal_status["credentials_configured"]:
        blockers.append("Modal no está configurado")
    if not r2_status.get("ready"):
        blockers.append("R2 no está configurado")
    blockers.extend(duplicate_blockers)
    report = {
        "status": "preflight_blocked" if blockers else "preflight_ready",
        "source": source_report,
        "output_profile": {"width": 2160, "height": 3840, "fps": 30},
        "estimated_cost_usd": budget.maximum_usd,
        "cost_limit_usd": MAX_JOB_BUDGET_USD,
        "cost_is_provider_guaranteed_cap": False,
        "modal_configured": bool(
            modal_status["sdk_installed"] and modal_status["credentials_configured"]
        ),
        "r2_configured": bool(r2_status.get("ready")),
        "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
        "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "preflight_blockers": blockers,
        "previous_failed_attempts": failed_panel_attempts,
        "external_services_called": [],
        "normalization_reused": True,
        "existing_panel_attempts": len(previous_attempts),
        "retry_failed_requested": retry_failed,
    }
    if preflight_callback:
        preflight_callback(report)
    if not execute:
        return report
    if "normalized_source_already_claimed" in duplicate_blockers:
        raise DuplicateProcessingError(
            "El máster normalizado ya tuvo su única ejecución; no se reenviará automáticamente."
        )
    if blockers:
        raise RuntimeError("Preflight bloqueado: " + "; ".join(blockers))

    auth = authorizations or {}
    required_authorizations = {
        "openai": "authorize_openai",
        "gemini": "authorize_gemini",
        "modal": "authorize_modal",
        "r2": "authorize_r2",
        "estimated_cost": "acknowledge_estimated_cost",
    }
    missing = [flag for key, flag in required_authorizations.items() if auth.get(key) is not True]
    if missing:
        raise PermissionError("Falta autorización explícita: " + ", ".join(missing))
    # Check the deployed function before consuming transcription or video-analysis APIs.
    modal_client = ModalRenderClient()
    modal_client.ensure_worker_available()

    source_ledger_key = normalized_source_key
    edit_options = dict(EDIT_OPTIONS)
    ledger = ProcessingLedger(ledger_path)
    ledger.claim(
        source_ledger_key,
        str(source),
        retry_failed=retry_failed,
        max_attempts=2 if retry_failed else 1,
        preserve_failure_history=retry_failed,
        edit_options=edit_options,
    )
    job_id = uuid.uuid4().hex
    job_work = Path(work_root).expanduser().resolve() / job_id
    events_path = job_work / "events.jsonl"
    output_directory = Path(output_dir).expanduser().resolve()
    source_hash_before = source_report["sha256"]

    def phase(name: str, **details: Any) -> None:
        _append_event(events_path, {"phase": name, **details})
        logger.info("normalized_render_phase=%s job_id=%s", name, job_id)

    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        phase("claimed", source_sha256=source_hash_before, output_profile="2160p30")
        phase("transcribing")
        words = transcribe_video(video, job_work / "whisper")
        transcript_payload = {
            "text": " ".join(word.text for word in words),
            "words": [
                {"word": word.text, "start": word.start, "end": word.end}
                for word in words
            ],
        }
        _write_new_json(job_work / "transcript.json", transcript_payload)

        phase("detecting_silences")
        silence_candidates = detect_silence_cut_suggestions(
            source,
            video.duration,
            words,
        )
        phase("analyzing_with_gemini")
        analysis = analyze_video(
            source,
            duration_seconds=video.duration,
            transcript_text=transcript_payload["text"],
        )
        _write_new_json(job_work / "gemini-analysis.json", analysis)
        (
            creative_plan,
            _suggested_zooms,
            applied_zooms,
            _suggested_cuts,
            applied_ai_cuts,
        ) = _prepare_modal_render_plan(
            {"cuts": [], "zooms": [], "stickers": []},
            video.duration,
            analysis["suggested_zooms"],
            analysis["suggested_cuts"],
            silence_candidates,
        )
        analysis["applied_zooms"] = applied_zooms
        analysis["applied_ai_cuts"] = applied_ai_cuts
        _write_new_json(job_work / "render-plan.json", creative_plan)

        base_style = load_video_style()
        style = replace(
            base_style,
            output_width=2160,
            output_height=3840,
            output_fps=30,
            output_quality="high",
            show_subtitles=True,
            highlight_color="#FFC0CB",
            max_words_per_caption=2,
        )
        phase(
            "rendering_on_modal",
            transcription_words=len(words),
            subtitle_segments=len(build_subtitle_segments(words, video_duration=video.duration)),
            confirmed_cuts=len(applied_ai_cuts),
            applied_zooms=len(applied_zooms),
        )
        result = process_local_video_modal(
            source,
            output_directory,
            job_id=job_id,
            paid_api_authorized=True,
            style=style,
            subtitle_mode="dynamic",
            render_mode="full",
            render_settings=EDIT_OPTIONS["render_settings"],
            creative_plan=creative_plan,
            subtitle_words=words,
            briefing="Edición profesional vertical 4K: subtítulos dinámicos, zoom suave y cortes solo en pausas corroboradas.",
            modal_client=modal_client,
            progress_callback=lambda value: phase(value),
        )
        if _sha256_file(source) != source_hash_before:
            raise RuntimeError("El SHA-256 del archivo fuente cambió durante el procesamiento.")
        phase("uploading_to_r2")
        r2_result = upload_mp4_to_r2(result["output"], job_id)
        result["r2_storage"] = r2_result
        result["gemini_analysis"] = analysis
        result["normalization_reused"] = True
        result["normalization_repeated"] = False
        result["cost_preflight_usd"] = budget.maximum_usd
        result["cost_is_provider_guaranteed_cap"] = False
        ledger.mark_completed(source_ledger_key, str(result["output"]))
        phase(
            "completed",
            output_name=Path(result["output"]).name,
            output_sha256=result.get("modal_output_sha256"),
            output_size_bytes=result.get("modal_remote_size_bytes"),
            r2_object_key=r2_result.get("object_key"),
        )
        return {
            "status": "completed",
            "job_id": job_id,
            "result": result,
            "work_directory": str(job_work),
            "ledger_path": str(Path(ledger_path)),
        }
    except BaseException as exc:
        try:
            ledger.mark_failed(source_ledger_key, f"{type(exc).__name__}: ejecución detenida")
            phase("failed", error_type=type(exc).__name__)
        except BaseException:
            logger.error("Could not persist normalized render failure; job_id=%s", job_id)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Enviar transcripción, análisis y un único render completo tras el preflight.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Autoriza una sola repetición si el ledger del máster 4K está en failed con un intento.",
    )
    for flag, help_text in (
        ("--authorize-openai", "Autoriza Whisper para transcribir el audio."),
        ("--authorize-gemini", "Autoriza Gemini para analizar el vídeo."),
        ("--authorize-modal", "Autoriza un render completo en Modal."),
        ("--authorize-r2", "Autoriza subir el MP4 privado a R2."),
        ("--acknowledge-estimated-cost", "Acepta la estimación, que no es un tope facturable."),
    ):
        parser.add_argument(flag, action="store_true", help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_normalized_render_once(
            execute=args.execute,
            retry_failed=args.retry_failed,
            authorizations={
                "openai": args.authorize_openai,
                "gemini": args.authorize_gemini,
                "modal": args.authorize_modal,
                "r2": args.authorize_r2,
                "estimated_cost": args.acknowledge_estimated_cost,
            },
            preflight_callback=lambda report: print(
                "PREFLIGHT " + json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                flush=True,
            ),
        )
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "status": "blocked_or_failed",
                    "error_type": type(exc).__name__,
                    "message": "No se reintentó automáticamente. Consulte el log persistente del trabajo.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
