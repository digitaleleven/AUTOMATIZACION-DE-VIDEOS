"""Local media validation and temporary audio extraction using FFmpeg."""

import json
import logging
from pathlib import Path
import subprocess
import tempfile
from collections.abc import Iterator

from .models import VideoInfo

logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    """Raised for invalid media or FFmpeg/ffprobe failures."""


def probe_video(path: str | Path) -> VideoInfo:
    video = Path(path).expanduser().resolve()
    if not video.exists() or not video.is_file():
        raise MediaError(f"Input video does not exist or is not a file: {video}")
    if video.suffix.lower() != ".mp4":
        raise MediaError("Input must be an MP4 file.")

    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", str(video),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        probe = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise MediaError("ffprobe is required but was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffprobe timed out while inspecting the MP4.") from exc
    except subprocess.CalledProcessError as exc:
        logger.error("ffprobe failed for %s: %s", video.name, exc.stderr[-2000:])
        raise MediaError(f"Could not read MP4 metadata for {video.name}.") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise MediaError("ffprobe returned invalid metadata.") from exc

    streams = probe.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise MediaError("The MP4 contains no video stream.")
    duration = float(probe.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise MediaError("The MP4 has no valid duration.")

    return VideoInfo(
        path=str(video),
        duration=duration,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


def extract_audio_chunks(
    video: VideoInfo,
    work_dir: str | Path,
    chunk_seconds: int = 900,
) -> Iterator[tuple[Path, float]]:
    """Extract mono 16 kHz MP3 chunks; each is comfortably below API file limits."""
    if not video.has_audio:
        raise MediaError("The MP4 has no audio stream to transcribe.")
    if chunk_seconds < 60:
        raise ValueError("Audio chunk size must be at least 60 seconds.")

    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    chunk_index = 0
    offset = 0.0
    while offset < video.duration:
        chunk_path = directory / f"audio-{chunk_index:04d}.mp3"
        length = min(float(chunk_seconds), video.duration - offset)
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{offset:.3f}", "-i", video.path, "-t", f"{length:.3f}",
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", str(chunk_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=max(120, int(length * 4)))
        except FileNotFoundError as exc:
            raise MediaError("ffmpeg is required but was not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"FFmpeg timed out extracting audio chunk {chunk_index + 1}.") from exc
        except subprocess.CalledProcessError as exc:
            logger.error("FFmpeg audio extraction failed: %s", exc.stderr[-2000:])
            raise MediaError(f"Could not extract audio chunk {chunk_index + 1}.") from exc

        if not chunk_path.exists() or chunk_path.stat().st_size == 0:
            raise MediaError(f"FFmpeg produced an empty audio chunk ({chunk_index + 1}).")
        yield chunk_path, offset
        offset += length
        chunk_index += 1


def temporary_work_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="video-pipeline-")

