"""Offline-safe planning and explicitly authorized local video processing."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import logging
import os
import hashlib
import json
import math
import mimetypes
import re
from uuid import uuid4
from collections.abc import Callable

from .config import PipelineConfig
from .media import (
    prepare_video_for_shotstack,
    probe_video,
    temporary_work_directory,
    validate_rendered_output,
)
from .models import SubtitleSegment, WordTiming
from .shotstack import ShotstackClient, build_edit_payload
from .state import ProcessingLedger
from .style import VideoStyle, load_video_style
from .subtitles import build_subtitle_segments, captions_to_srt
from .transcription import transcribe_video

logger = logging.getLogger(__name__)
from .drive import (
    DriveAccessError,
    download_drive_file,
    get_drive_file,
    load_file_selections,
    load_folder_selections,
    upload_new_file,
)

@dataclass(frozen=True)
class LocalVideo:
    name: str
    path: str
    size_bytes: int


def find_local_mp4s(directory: str | Path) -> list[LocalVideo]:
    """List supported MP4 and MOV files without changing their contents."""
    root = Path(directory)
    if not root.exists():
        return []
    if not root.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {root}")

    return [
        LocalVideo(name=file.name, path=str(file), size_bytes=file.stat().st_size)
        for file in sorted(root.iterdir(), key=lambda item: item.name.lower())
        if file.is_file() and file.suffix.lower() in {".mp4", ".mov"}
    ]


def find_local_videos(directory: str | Path) -> list[LocalVideo]:
    """Return every supported local video; retained as the primary scan API."""
    return find_local_mp4s(directory)


def build_dry_run_plan(
    config: PipelineConfig,
    directory: str | Path,
    style: VideoStyle | None = None,
    subtitle_mode: str = "none",
) -> dict[str, Any]:
    """Probe local inputs and estimate call counts without contacting providers."""
    if subtitle_mode not in {"dynamic", "simple", "none"}:
        raise ValueError("subtitle_mode must be dynamic, simple, or none.")
    appearance = style or load_video_style()
    transcription_required = subtitle_mode != "none"
    videos = find_local_videos(directory)
    inspections = []
    for item in videos:
        try:
            metadata = probe_video(item.path)
            estimated_usd = _estimate_cost(
                metadata.duration,
                appearance,
                include_transcription=transcription_required,
            )
            inspections.append(
                {
                    **asdict(item),
                    "valid_video": True,
                    "duration_seconds": metadata.duration,
                    "dimensions": {"width": metadata.width, "height": metadata.height},
                    "has_audio": metadata.has_audio,
                    "estimated_openai_transcription_chunks": (
                        math.ceil(metadata.duration / 900) if transcription_required else 0
                    ),
                    "estimated_cost_usd": estimated_usd,
                    "fits_limits": (
                        metadata.size_bytes <= appearance.max_input_size_mb * 1024 * 1024
                        and metadata.duration <= appearance.max_video_duration_seconds
                        and (metadata.has_audio or not transcription_required)
                        and (
                            not transcription_required
                            or math.ceil(metadata.duration / 900) <= appearance.max_transcription_chunks
                        )
                    ),
                }
            )
        except Exception as exc:
            inspections.append({**asdict(item), "valid_video": False, "validation_error": str(exc)})
    return {
        "mode": "dry-run",
        "subtitle_mode": subtitle_mode,
        "external_services_called": [],
        "source_files_modified": False,
        "input_folder_id": config.input_folder_id,
        "processed_folder_id": config.processed_folder_id,
        "logos_folder_id": config.logos_folder_id,
        "discovered_local_video_count": len(videos),
        "discovered_local_mp4_count": len(videos),
        "discovered_local_mov_count": sum(Path(item.path).suffix.lower() == ".mov" for item in videos),
        "videos": inspections,
        "shotstack_environment": "stage",
        "cost_estimate_note": _cost_estimate_note(
            appearance,
            include_transcription=transcription_required,
        ),
        "operation_limits": {
            "max_files_per_run": 1,
            "max_input_size_mb": appearance.max_input_size_mb,
            "max_video_duration_seconds": appearance.max_video_duration_seconds,
            "max_transcription_chunks": appearance.max_transcription_chunks,
            "max_renders_per_attempt": 1,
        },
        "next_stages": [
            "validate and inspect the local MP4",
            *(
                [
                    "transcribe with OpenAI (requires explicit paid-API authorization)",
                    "prepare timed 2–4-word subtitle clips",
                ]
                if transcription_required
                else ["skip transcription and subtitles"]
            ),
            "ingest and render 1080 × 1920 with Shotstack Stage (requires explicit authorization)",
            "download the verified render and create a new file in the processed Drive folder",
        ],
        "live_integrations_ready": {
            "google_drive": bool(
                config.google_oauth_configured
                and config.input_folder_id
                and config.processed_folder_id
            ),
            "openai": config.openai_api_key_present,
            "shotstack": config.shotstack_api_key_present,
        },
    }


def sync_drive_media(
    config: PipelineConfig,
    access_token: str,
    *,
    input_dir: str | Path | None = None,
    logo_dir: str | Path = "data/logos",
    manifest_path: str | Path = "data/drive_downloads.json",
    session: Any | None = None,
) -> dict[str, Any]:
    """Download selected MP4s and logos; skip known files and never overwrite."""
    selections = load_folder_selections()
    input_folder = config.input_folder_id or selections.get("entrada", {}).get("id")
    logos_folder = config.logos_folder_id or selections.get("logos", {}).get("id")
    if not input_folder or not logos_folder:
        raise DriveAccessError("Selecciona y valida las carpetas Entrada y Logos con Google Picker.")
    if not access_token:
        raise DriveAccessError("Autoriza Google Drive en esta sesión para descargar archivos.")

    incoming = Path(input_dir or config.local_input_dir).expanduser().resolve()
    logos = Path(logo_dir).expanduser().resolve()
    manifest_file = Path(manifest_path)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}

    report: dict[str, Any] = {"videos": [], "logos": []}
    for role, folder_id, kind, directory, label in (
        ("videos", input_folder, "video", incoming, "entrada"),
        ("logos", logos_folder, "logo", logos, "logos"),
    ):
        selection_role = "entrada" if role == "videos" else "logos"
        files = load_file_selections().get(selection_role, [])
        directory.mkdir(parents=True, exist_ok=True)
        for selected in files:
            item = get_drive_file(
                selected["id"],
                access_token=access_token,
                session=session,
            )
            if folder_id not in item.get("parents", []):
                raise DriveAccessError(
                    f"El archivo {item.get('name', selected['id'])} ya no pertenece a la carpeta seleccionada."
                )
            suffix = Path(item["name"]).suffix.lower()
            mime = item.get("mimeType", "")
            if kind == "video" and suffix != ".mp4" and mime != "video/mp4":
                raise DriveAccessError("La selección de Entrada contiene un archivo que no es MP4.")
            if kind == "logo" and suffix not in {".png", ".jpg", ".jpeg", ".webp"} and mime not in {
                "image/png", "image/jpeg", "image/webp"
            }:
                raise DriveAccessError("La selección de Logos contiene una imagen no compatible.")
            file_id = item["id"]
            previous = manifest.get(file_id)
            if (
                isinstance(previous, dict)
                and previous.get("modifiedTime") == item.get("modifiedTime")
                and previous.get("size") == item.get("size")
                and Path(str(previous.get("path", ""))).is_file()
            ):
                report[role].append(
                    {
                        "id": file_id,
                        "name": item["name"],
                        "status": "already_downloaded",
                        "local_path": str(previous["path"]),
                    }
                )
                continue
            safe_name = "".join(
                char for char in Path(item["name"]).name
                if char.isalnum() or char in {"-", "_", ".", " "}
            ).strip(" .")
            if not safe_name:
                safe_name = f"{label}-{file_id}"
            suffix = Path(safe_name).suffix.lower()
            if kind == "video" and suffix != ".mp4":
                safe_name = f"{Path(safe_name).stem}.mp4"
            version = hashlib.sha256(
                f"{item.get('modifiedTime', '')}:{item.get('size', '')}".encode("utf-8")
            ).hexdigest()[:8]
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "", file_id)[:10] or "drive-file"
            name = f"{Path(safe_name).stem}_{safe_id}_{version}{Path(safe_name).suffix}"
            destination = directory / name
            downloaded = download_drive_file(
                file_id,
                destination,
                access_token=access_token,
                session=session,
            )
            manifest[file_id] = {
                "name": item["name"],
                "modifiedTime": item.get("modifiedTime"),
                "size": item.get("size"),
                "path": str(downloaded),
            }
            report[role].append(
                {
                    "id": file_id,
                    "name": item["name"],
                    "status": "downloaded",
                    "local_path": str(downloaded),
                }
            )
            _write_drive_manifest(manifest_file, manifest)

    report["input_dir"] = str(incoming)
    report["logo_dir"] = str(logos)
    return report


def _write_drive_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _new_output_path(
    directory: Path,
    source: Path,
    *,
    subtitle_mode: str = "dynamic",
) -> tuple[Path, Path]:
    """Reserve a unique output name to prevent accidental overwrite/races."""
    directory.mkdir(parents=True, exist_ok=True)
    suffix_label = "edited" if subtitle_mode == "none" else "subtitled"
    base = f"{source.stem}_vertical_{suffix_label}"
    suffix = 1
    while True:
        stem = base if suffix == 1 else f"{base}_{suffix}"
        output = directory / f"{stem}.mp4"
        lock = directory / f".{stem}.lock"
        try:
            with lock.open("x", encoding="utf-8") as lock_file:
                lock_file.write("reserved by video_pipeline\n")
            if output.exists():
                lock.unlink(missing_ok=True)
                suffix += 1
                continue
            return output, lock
        except FileExistsError:
            suffix += 1


def process_local_video(
    input_path: str | Path,
    output_dir: str | Path = "outputs",
    *,
    paid_api_authorized: bool = False,
    openai_client: Any | None = None,
    shotstack_client: ShotstackClient | None = None,
    drive_uploader: Any | None = None,
    upload_to_drive: bool = True,
    drive_access_token: str | None = None,
    config: PipelineConfig | None = None,
    style: VideoStyle | None = None,
    subtitle_mode: str = "none",
    state_db_path: str | Path = "data/processing.sqlite3",
    retry_failed: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Transcribe and render one local MP4/MOV; never modifies the source.

    Paid provider calls are blocked unless the caller explicitly opts in.
    Required credentials are checked before the first API request. Drive
    receives a new file only when upload_to_drive is enabled and the render
    completes and validates.
    """
    if not paid_api_authorized:
        raise PermissionError(
            "Paid API calls are disabled. Re-run with --authorize-paid-apis only "
            "when you explicitly approve OpenAI transcription and Shotstack ingest/render."
        )

    if subtitle_mode not in {"dynamic", "simple", "none"}:
        raise ValueError("subtitle_mode must be dynamic, simple, or none.")
    transcription_required = subtitle_mode != "none"
    settings = config or PipelineConfig.from_environment()
    appearance = style or load_video_style()
    if settings.shotstack_environment != "stage":
        raise ValueError("Production is disabled; all Shotstack jobs must use Stage.")
    source = Path(input_path).expanduser().resolve()
    if progress_callback:
        progress_callback("validating")
    video = probe_video(source)
    if video.video_codec not in {"h264", "hevc"}:
        raise ValueError(
            f"Unsupported video codec {video.video_codec}; accepted codecs are H.264 and HEVC/H.265."
        )
    if video.size_bytes > appearance.max_input_size_mb * 1024 * 1024:
        raise ValueError(
            f"Input is {video.size_bytes / (1024 * 1024):.1f} MB; the configured maximum is "
            f"{appearance.max_input_size_mb} MB."
        )
    if video.duration > appearance.max_video_duration_seconds:
        raise ValueError(
            f"Input is {video.duration:.1f} seconds; the configured maximum is "
            f"{appearance.max_video_duration_seconds} seconds."
        )
    if transcription_required and not video.has_audio:
        raise ValueError("Input MP4 has no audio stream to transcribe.")
    transcription_chunks = math.ceil(video.duration / 900) if transcription_required else 0
    if transcription_required and transcription_chunks > appearance.max_transcription_chunks:
        raise ValueError(
            f"Input requires {transcription_chunks} transcription chunks; the configured maximum is "
            f"{appearance.max_transcription_chunks}."
        )
    estimated_cost = _estimate_cost(
        video.duration,
        appearance,
        include_transcription=transcription_required,
    )
    if appearance.max_estimated_cost_usd is not None:
        if estimated_cost is None:
            raise ValueError(
                "A maximum cost was configured, but account-specific API prices are missing from video_style.json."
            )
        if estimated_cost > appearance.max_estimated_cost_usd:
            raise ValueError(
                f"Estimated cost ${estimated_cost:.4f} exceeds the configured limit "
                f"${appearance.max_estimated_cost_usd:.4f}."
            )
    destination_dir = Path(output_dir).expanduser().resolve()
    if appearance.logo_path:
        logo_path = Path(appearance.logo_path).expanduser().resolve()
        if not logo_path.is_file():
            raise ValueError(f"Configured logo file does not exist: {logo_path}")
        if logo_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Logo must be a PNG, JPG, JPEG, or WebP image.")
        if logo_path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Configured logo exceeds the 20 MB safety limit.")
    else:
        logo_path = None

    if transcription_required and openai_client is None and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set in Replit Secrets.")
    if shotstack_client is None and not os.getenv("SHOTSTACK_API_KEY"):
        raise RuntimeError(
            "SHOTSTACK_API_KEY is not set in Replit Secrets. No Shotstack credential was created or assumed."
        )
    drive_token = drive_access_token or os.getenv("GOOGLE_OAUTH_TOKEN")
    if upload_to_drive and not settings.processed_folder_id:
        raise RuntimeError(
            "No hay una carpeta 02_PROCESADOS configurada para guardar el resultado."
        )
    if upload_to_drive and not drive_token and drive_uploader is None:
        raise RuntimeError(
            "Autoriza Google Drive en esta sesión con drive.file o configura GOOGLE_OAUTH_TOKEN en Replit Secrets. "
            "No se guardan tokens de acceso en el historial de trabajos."
        )
    renderer = shotstack_client or ShotstackClient(
        environment=settings.shotstack_environment,
        poll_seconds=settings.shotstack_poll_seconds,
        timeout_seconds=settings.shotstack_timeout_seconds,
    )
    source_hash = _sha256_file(source)
    ledger = ProcessingLedger(state_db_path)
    job = ledger.claim(
        source_hash,
        str(source),
        retry_failed=retry_failed,
        max_attempts=2,
        edit_options={
            "subtitle_mode": subtitle_mode,
            "style": asdict(appearance),
        },
    )
    output_path: Path | None = None
    lock_path: Path | None = None
    try:
        output_path, lock_path = _new_output_path(
            destination_dir,
            source,
            subtitle_mode=subtitle_mode,
        )
        ledger.update(source_hash, output_path=str(output_path))
        with temporary_work_directory() as work_dir:
            logger.info("Inspecting %s (%dx%d, %.2f seconds)", source.name, video.width, video.height, video.duration)
            if progress_callback:
                progress_callback("normalizing")
            editing_video, conversion = prepare_video_for_shotstack(video, work_dir)
            captions: list[SubtitleSegment] = []
            words_count = 0
            if transcription_required and job.get("captions_json"):
                captions = [SubtitleSegment(**item) for item in json.loads(job["captions_json"])]
                logger.info("Reusing previously stored caption timings after a failed attempt")
                words_count = sum(len(caption.text.split()) for caption in captions)
            elif transcription_required:
                if progress_callback:
                    progress_callback("transcribing")
                words: list[WordTiming] = transcribe_video(
                    video,
                    work_dir,
                    model=settings.openai_model,
                    client=openai_client,
                )
                captions = build_subtitle_segments(words, video_duration=video.duration)
                words_count = len(words)
                ledger.update(
                    source_hash,
                    captions_json=json.dumps([asdict(caption) for caption in captions], ensure_ascii=False),
                )

            subtitle_url = None
            if transcription_required:
                srt_path = Path(work_dir) / "captions.srt"
                if progress_callback:
                    progress_callback("preparing_captions")
                srt_path.write_text(
                    captions_to_srt(captions, appearance.emoji_by_keyword),
                    encoding="utf-8",
                )
                logger.info("Prepared %d timed subtitle segments", len(captions))

                if job.get("subtitle_url"):
                    subtitle_url = job["subtitle_url"]
                else:
                    if progress_callback:
                        progress_callback("uploading_captions")
                    subtitle_url = renderer.upload_local_asset(srt_path, "application/x-subrip")
                    ledger.update(source_hash, subtitle_url=subtitle_url)

            if job.get("shotstack_source_url"):
                shotstack_source_url = job["shotstack_source_url"]
            else:
                if progress_callback:
                    progress_callback("uploading")
                shotstack_source_url = renderer.upload_local_video(editing_video.path)
                ledger.update(source_hash, shotstack_source_url=shotstack_source_url)

            logo_url = job.get("logo_url")
            if logo_path and not logo_url:
                logo_mime, _ = mimetypes.guess_type(logo_path.name)
                logo_mime = logo_mime if logo_mime in {"image/png", "image/jpeg", "image/webp"} else "image/png"
                logo_url = renderer.upload_local_asset(logo_path, logo_mime)
                ledger.update(source_hash, logo_url=logo_url)

            edit_payload = build_edit_payload(
                shotstack_source_url,
                video.duration,
                captions,
                subtitle_url,
                style=appearance,
                logo_url=logo_url,
                source_width=editing_video.width,
                source_height=editing_video.height,
                subtitle_mode=subtitle_mode,
            )
            render_id = job.get("render_id")
            render_url = None
            if render_id:
                previous_status, previous_url = renderer.render_status(render_id)
                if previous_status == "done":
                    render_url = previous_url
                elif previous_status in {"failed", "cancelled"}:
                    if not retry_failed:
                        raise RuntimeError(
                            f"Previous Shotstack render {render_id} ended as {previous_status}; "
                            "use --retry-failed to authorize one new Stage render."
                        )
                    render_id = None
            if not render_url:
                if not render_id:
                    if progress_callback:
                        progress_callback("rendering")
                    render_id = renderer.submit_render(edit_payload)
                    ledger.update(source_hash, render_id=render_id)
                elif progress_callback:
                    progress_callback("rendering")
                render_url = renderer.wait_for_render(render_id)
            if progress_callback:
                progress_callback("downloading")
            output = renderer.download_render(render_url, output_path)
            if progress_callback:
                progress_callback("validating_output")
            validated_output = validate_rendered_output(video, output, appearance)
            if _sha256_file(source) != source_hash:
                raise RuntimeError("The source MP4 changed during processing; it remains unmodified by this pipeline.")

        drive_file = None
        if upload_to_drive:
            uploader = drive_uploader or upload_new_file
            drive_file = uploader(
                output,
                folder_id=settings.processed_folder_id,
                access_token=drive_token,
            )
        ledger.mark_completed(source_hash, str(output))
        if progress_callback:
            progress_callback("completed")
        return {
            "source": str(source),
            "output": str(output),
            "drive_output": drive_file,
            "drive_upload_skipped": not upload_to_drive,
            "duration_seconds": video.duration,
            "source_dimensions": {"width": video.width, "height": video.height},
            "source_video_codec": video.video_codec,
            "source_audio_codec": video.audio_codec,
            "source_fps": video.fps,
            "output_dimensions": {"width": validated_output.width, "height": validated_output.height},
            "output_video_codec": validated_output.video_codec,
            "output_audio_codec": validated_output.audio_codec,
            "output_fps": validated_output.fps,
            "transcribed_word_count": words_count,
            "subtitle_mode": subtitle_mode,
            "subtitle_segment_count": len(captions),
            "render_id": render_id,
            "render_status_verified": True,
            "shotstack_environment": "stage",
            "source_modified": False,
            "conversion": conversion,
            "estimated_cost_usd": _estimate_cost(video.duration, appearance),
            "cost_estimate_note": _cost_estimate_note(
                appearance,
                include_transcription=transcription_required,
            ),
            "paid_operations": {
                "openai_transcription_chunks": transcription_chunks,
                "shotstack_ingested_assets": 1 + int(transcription_required) + int(bool(logo_path)),
                "shotstack_render_jobs": 1,
                "max_renders_per_attempt": 1,
            },
        }
    except Exception as exc:
        ledger.mark_failed(source_hash, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if lock_path is not None:
            lock_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _estimate_cost(
    duration_seconds: float,
    style: VideoStyle,
    *,
    include_transcription: bool = True,
) -> float | None:
    if style.shotstack_price_per_render_minute_usd is None:
        return None
    if include_transcription and style.openai_price_per_audio_minute_usd is None:
        return None
    minutes = duration_seconds / 60
    transcription_cost = (
        minutes * float(style.openai_price_per_audio_minute_usd)
        if include_transcription
        else 0.0
    )
    return round(
        transcription_cost + minutes * float(style.shotstack_price_per_render_minute_usd),
        4,
    )


def _cost_estimate_note(
    style: VideoStyle,
    *,
    include_transcription: bool = True,
) -> str:
    missing = []
    if include_transcription and style.openai_price_per_audio_minute_usd is None:
        missing.append("OpenAI transcription")
    if style.shotstack_price_per_render_minute_usd is None:
        missing.append("Shotstack Stage render")
    if missing:
        return (
            "Cost estimate unavailable for "
            + " and ".join(missing)
            + "; add current account-specific prices to video_style.json. "
            "Explicit authorization remains required."
        )
    if not include_transcription:
        return (
            "No OpenAI transcription is included. Shotstack estimate uses the configured "
            "account-specific rate; provider billing may differ."
        )
    return "Estimate uses the per-minute prices configured in video_style.json; provider billing may differ."
