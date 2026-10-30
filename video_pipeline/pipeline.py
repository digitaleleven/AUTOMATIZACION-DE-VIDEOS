"""Offline-safe planning and Google Drive media synchronization.

Rendering is owned by the authenticated panel's Modal/Remotion flow. The
former provider-specific CLI renderer has been retired rather than replaced
with a local or simulated renderer.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import math
import os
import re
from uuid import uuid4

from .config import PipelineConfig
from .drive import (
    DriveAccessError,
    download_drive_file,
    get_drive_file,
    load_file_selections,
    load_folder_selections,
)
from .media import probe_video
from .style import VideoStyle, load_video_style


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
    """Return supported local MP4/MOV files."""
    return find_local_mp4s(directory)


def build_dry_run_plan(
    config: PipelineConfig,
    directory: str | Path,
    style: VideoStyle | None = None,
    subtitle_mode: str = "none",
) -> dict[str, Any]:
    """Inspect local inputs and estimate OpenAI call counts without API calls."""
    if subtitle_mode not in {"dynamic", "simple", "none"}:
        raise ValueError("subtitle_mode must be dynamic, simple, or none.")
    appearance = style or load_video_style()
    transcription_required = subtitle_mode != "none"
    videos = find_local_videos(directory)
    inspections = []
    for item in videos:
        try:
            metadata = probe_video(item.path)
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
                    # The cloud-render price is variable and is not quoted here.
                    "estimated_cost_usd": None,
                    "fits_limits": (
                        metadata.duration <= appearance.max_video_duration_seconds
                        and (metadata.has_audio or not transcription_required)
                        and (
                            not transcription_required
                            or math.ceil(metadata.duration / 900)
                            <= appearance.max_transcription_chunks
                        )
                    ),
                }
            )
        except Exception as exc:
            inspections.append(
                {**asdict(item), "valid_video": False, "validation_error": str(exc)}
            )
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
        "discovered_local_mov_count": sum(
            Path(item.path).suffix.lower() == ".mov" for item in videos
        ),
        "videos": inspections,
        "cost_estimate_note": _cost_estimate_note(
            appearance, include_transcription=transcription_required
        ),
        "operation_limits": {
            "max_files_per_run": 1,
            "max_input_size_mb": appearance.max_input_size_mb,
            "max_video_duration_seconds": appearance.max_video_duration_seconds,
            "max_transcription_chunks": appearance.max_transcription_chunks,
            "max_renders_per_attempt": 1,
        },
        "next_stages": [
            "validate and inspect the local MP4/MOV",
            *(
                [
                    "transcribe with OpenAI after explicit authorization",
                    "prepare timed subtitle segments",
                ]
                if transcription_required
                else ["skip transcription and subtitles"]
            ),
            "prepare and review the edit in the authenticated panel",
            "render through the Modal/Remotion worker after explicit authorization",
            "optionally save a new file in the configured processed Drive folder",
        ],
        "live_integrations_ready": {
            "google_drive": bool(
                config.google_oauth_configured
                and config.input_folder_id
                and config.processed_folder_id
            ),
            "openai": config.openai_api_key_present,
            "modal": _modal_sdk_available(),
        },
    }


def _modal_sdk_available() -> bool:
    try:
        from importlib.util import find_spec

        return find_spec("modal") is not None
    except (ImportError, ValueError):
        return False


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
        raise DriveAccessError(
            "Selecciona y valida las carpetas Entrada y Logos con Google Picker."
        )
    if not access_token:
        raise DriveAccessError(
            "Autoriza Google Drive en esta sesión para descargar archivos."
        )

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
                selected["id"], access_token=access_token, session=session
            )
            if folder_id not in item.get("parents", []):
                raise DriveAccessError(
                    f"El archivo {item.get('name', selected['id'])} ya no pertenece a la carpeta seleccionada."
                )
            suffix = Path(item["name"]).suffix.lower()
            mime = item.get("mimeType", "")
            if kind == "video" and suffix not in {".mp4", ".mov"} and mime not in {
                "video/mp4",
                "video/quicktime",
            }:
                raise DriveAccessError(
                    "La selección de Entrada contiene un archivo que no es MP4/MOV."
                )
            if (
                kind == "logo"
                and suffix not in {".png", ".jpg", ".jpeg", ".webp"}
                and mime not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise DriveAccessError(
                    "La selección de Logos contiene una imagen no compatible."
                )
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
                char
                for char in Path(item["name"]).name
                if char.isalnum() or char in {"-", "_", ".", " "}
            ).strip(" .") or f"{label}-{file_id}"
            suffix = Path(safe_name).suffix.lower()
            if kind == "video" and suffix not in {".mp4", ".mov"}:
                safe_name = f"{Path(safe_name).stem}.mp4"
            version = hashlib.sha256(
                f"{item.get('modifiedTime', '')}:{item.get('size', '')}".encode("utf-8")
            ).hexdigest()[:8]
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "", file_id)[:10] or "drive-file"
            name = f"{Path(safe_name).stem}_{safe_id}_{version}{Path(safe_name).suffix}"
            destination = directory / name
            downloaded = download_drive_file(
                file_id, destination, access_token=access_token, session=session
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
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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


def process_local_video(*args: Any, paid_api_authorized: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Retired CLI renderer; actual rendering is available via the authenticated panel."""
    if not paid_api_authorized:
        raise PermissionError(
            "El render requiere autorización humana explícita desde el panel autenticado."
        )
    raise RuntimeError(
        "El render por CLI está deshabilitado. No se inició ningún trabajo; "
        "usa el flujo del panel, que envía el render al worker Modal cuando está disponible."
    )


def _estimate_cost(
    duration_seconds: float,
    style: VideoStyle,
    *,
    include_transcription: bool = True,
) -> float | None:
    """Return no total estimate because Modal compute and storage are unquoted."""
    return None


def _cost_estimate_note(
    style: VideoStyle,
    *,
    include_transcription: bool = True,
) -> str:
    return (
        "No hay un coste total cotizado: el cómputo Modal y el almacenamiento/egress "
        "de destino dependen del uso. Revisa y autoriza el coste desconocido en el panel "
        "antes de iniciar cualquier trabajo."
    )
