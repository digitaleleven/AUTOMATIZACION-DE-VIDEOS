"""Offline-safe pipeline planning and local input discovery.

This module deliberately does not call Google Drive, OpenAI, or Shotstack.
Live provider adapters should be added separately and must never mutate the
source Drive file: outputs are new files in the configured destination folder.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import PipelineConfig


@dataclass(frozen=True)
class LocalVideo:
    name: str
    path: str
    size_bytes: int


def find_local_mp4s(directory: str | Path) -> list[LocalVideo]:
    """List MP4 files without opening, copying, or changing their contents."""
    root = Path(directory)
    if not root.exists():
        return []
    if not root.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {root}")

    return [
        LocalVideo(name=file.name, path=str(file), size_bytes=file.stat().st_size)
        for file in sorted(root.iterdir(), key=lambda item: item.name.lower())
        if file.is_file() and file.suffix.lower() == ".mp4"
    ]


def build_dry_run_plan(config: PipelineConfig, directory: str | Path) -> dict[str, Any]:
    """Build a serializable plan; no external requests or filesystem writes."""
    videos = find_local_mp4s(directory)
    return {
        "mode": "dry-run",
        "external_services_called": [],
        "source_files_modified": False,
        "input_folder_id": config.input_folder_id,
        "processed_folder_id": config.processed_folder_id,
        "logos_folder_id": config.logos_folder_id,
        "discovered_local_mp4_count": len(videos),
        "videos": [asdict(video) for video in videos],
        "next_stages": [
            "download source MP4 from Drive (read-only)",
            "transcribe with OpenAI",
            "prepare timed subtitle clips",
            "render a new 9:16 video with Shotstack",
            "upload new output to the processed Drive folder",
        ],
        "live_integrations_ready": {
            "google_drive": config.google_oauth_token_present,
            "openai": config.openai_api_key_present,
            "shotstack": config.shotstack_api_key_present,
        },
    }

