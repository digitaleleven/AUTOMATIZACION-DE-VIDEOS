"""Offline-safe planning and explicitly authorized local video processing."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import logging
import os

from .config import PipelineConfig
from .media import probe_video, temporary_work_directory
from .models import SubtitleSegment, WordTiming
from .shotstack import ShotstackClient, build_edit_payload
from .subtitles import build_subtitle_segments
from .transcription import transcribe_video

logger = logging.getLogger(__name__)

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
            "validate and inspect the local MP4",
            "transcribe with OpenAI (requires explicit paid-API authorization)",
            "prepare timed 2–4-word subtitle clips",
            "ingest and render 1080 × 1920 with Shotstack (requires explicit authorization)",
            "download a new output file locally",
        ],
        "live_integrations_ready": {
            "google_drive": bool(os.getenv("GOOGLE_OAUTH_TOKEN")),
            "openai": config.openai_api_key_present,
            "shotstack": config.shotstack_api_key_present,
        },
    }


def _new_output_path(directory: Path, source: Path) -> tuple[Path, Path]:
    """Reserve a unique output name to prevent accidental overwrite/races."""
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{source.stem}_vertical_subtitled"
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
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    """Transcribe and render one local MP4; never modifies the source.

    Live OpenAI and Shotstack calls are blocked unless the caller explicitly
    opts in. Both credentials are checked before the first API request.
    """
    if not paid_api_authorized:
        raise PermissionError(
            "Paid API calls are disabled. Re-run with --authorize-paid-apis only "
            "when you explicitly approve OpenAI transcription and Shotstack ingest/render."
        )

    settings = config or PipelineConfig.from_environment()
    source = Path(input_path).expanduser().resolve()
    video = probe_video(source)
    destination_dir = Path(output_dir).expanduser().resolve()
    if source.parent == destination_dir:
        # A separate output directory keeps source files easy to distinguish.
        logger.warning("Output directory is the same as the source directory; output still gets a new name.")

    if openai_client is None and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set in Replit Secrets.")
    if shotstack_client is None and not os.getenv("SHOTSTACK_API_KEY"):
        raise RuntimeError(
            "SHOTSTACK_API_KEY is not set in Replit Secrets. No Shotstack credential was created or assumed."
        )

    renderer = shotstack_client or ShotstackClient(
        environment=settings.shotstack_environment,
        poll_seconds=settings.shotstack_poll_seconds,
        timeout_seconds=settings.shotstack_timeout_seconds,
    )
    output_path, lock_path = _new_output_path(destination_dir, source)
    try:
        with temporary_work_directory() as work_dir:
            logger.info("Inspecting %s (%dx%d, %.2f seconds)", source.name, video.width, video.height, video.duration)
            words: list[WordTiming] = transcribe_video(
                video,
                work_dir,
                model=settings.openai_model,
                client=openai_client,
            )
            captions: list[SubtitleSegment] = build_subtitle_segments(words, video_duration=video.duration)
            logger.info("Prepared %d timed subtitle segments", len(captions))

            # The source is sent to Shotstack only after successful transcription.
            shotstack_source_url = renderer.upload_local_video(source)
            edit_payload = build_edit_payload(shotstack_source_url, video.duration, captions)
            render_id = renderer.submit_render(edit_payload)
            render_url = renderer.wait_for_render(render_id)
            output = renderer.download_render(render_url, output_path)

        return {
            "source": str(source),
            "output": str(output),
            "duration_seconds": video.duration,
            "source_dimensions": {"width": video.width, "height": video.height},
            "output_dimensions": {"width": 1080, "height": 1920},
            "transcribed_word_count": len(words),
            "subtitle_segment_count": len(captions),
            "render_id": render_id,
            "source_modified": False,
        }
    finally:
        lock_path.unlink(missing_ok=True)

