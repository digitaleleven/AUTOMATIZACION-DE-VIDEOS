"""Local media validation and temporary audio extraction using FFmpeg."""

import json
import logging
from pathlib import Path
import subprocess
import tempfile
from collections.abc import Iterator

from .models import VideoInfo
from .style import VideoStyle

logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    """Raised for invalid media or FFmpeg/ffprobe failures."""


class OutputValidationError(MediaError):
    """Raised when a rendered MP4 does not meet the configured output contract."""


def probe_video(path: str | Path) -> VideoInfo:
    video = Path(path).expanduser().resolve()
    if not video.exists() or not video.is_file():
        raise MediaError(f"Input video does not exist or is not a file: {video}")
    if video.suffix.lower() not in {".mp4", ".mov"}:
        raise MediaError("Input must be an MP4 or MOV file.")

    command = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,size,format_name,bit_rate:"
        "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,profile,"
        "color_primaries,color_transfer,color_space,color_range,bit_rate:"
        "stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(video),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        probe = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise MediaError("ffprobe is required but was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffprobe timed out while inspecting the video.") from exc
    except subprocess.CalledProcessError as exc:
        logger.error("ffprobe failed for %s: %s", video.name, exc.stderr[-2000:])
        raise MediaError(f"Could not read video metadata for {video.name}.") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise MediaError("ffprobe returned invalid metadata.") from exc

    streams = probe.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video_stream:
        raise MediaError("The video file contains no video stream.")
    duration = float(probe.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise MediaError("The video file has no valid duration.")

    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    rotation_value = (video_stream.get("tags") or {}).get("rotate")
    if rotation_value is None:
        rotation_value = next(
            (
                side_data.get("rotation")
                for side_data in video_stream.get("side_data_list", [])
                if side_data.get("rotation") is not None
            ),
            0,
        )
    try:
        rotation = int(round(float(rotation_value or 0))) % 360
    except (TypeError, ValueError):
        rotation = 0
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if rotation in {90, 270}:
        width, height = height, width
    rate = str(video_stream.get("r_frame_rate", "0/1")).split("/", 1)
    try:
        fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        video_bit_rate = int(video_stream.get("bit_rate") or 0) or None
    except (TypeError, ValueError):
        video_bit_rate = None
    try:
        format_bit_rate = int(probe.get("format", {}).get("bit_rate") or 0) or None
    except (TypeError, ValueError):
        format_bit_rate = None
    return VideoInfo(
        path=str(video),
        duration=duration,
        width=width,
        height=height,
        has_audio=audio_stream is not None,
        video_codec=str(video_stream.get("codec_name") or "unknown"),
        audio_codec=str(audio_stream.get("codec_name")) if audio_stream else None,
        fps=fps,
        size_bytes=int(probe.get("format", {}).get("size") or video.stat().st_size),
        rotation=rotation,
        pixel_format=str(video_stream.get("pix_fmt") or "unknown"),
        color_primaries=video_stream.get("color_primaries"),
        color_transfer=video_stream.get("color_transfer"),
        color_space=video_stream.get("color_space"),
        color_range=video_stream.get("color_range"),
        container=str(probe.get("format", {}).get("format_name") or "unknown"),
        video_bit_rate_bps=video_bit_rate,
        format_bit_rate_bps=format_bit_rate,
    )


def prepare_video_for_modal(
    video: VideoInfo,
    work_dir: str | Path,
) -> tuple[VideoInfo, dict[str, object]]:
    """Create a new 1080x1920 H.264/BT.709/30fps input using Remotion's color profile."""
    source_path = Path(video.path).expanduser().resolve()
    directory = Path(work_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{source_path.stem}.mp4"
    if target.exists() or target.resolve() == source_path:
        raise MediaError("La normalización requiere una ruta temporal nueva; no se sobrescribirá el original.")

    normalizer = Path(__file__).resolve().parents[1] / "remotion-renderer" / "src" / "normalize-input.mjs"
    if not normalizer.is_file():
        raise MediaError("No está disponible el normalizador de entrada de Remotion.")
    timeout = min(6 * 60 * 60, max(300, int(video.duration * 10)))
    try:
        result = subprocess.run(
            ["node", str(normalizer), str(source_path), str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaError("Node.js y FFmpeg son necesarios para normalizar la entrada antes de Modal.") from exc
    except subprocess.TimeoutExpired as exc:
        target.unlink(missing_ok=True)
        raise MediaError("La normalización local superó el tiempo máximo; no se envió el archivo a Modal.") from exc
    except subprocess.CalledProcessError as exc:
        target.unlink(missing_ok=True)
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        safe_detail = detail[-1][:500] if detail else "error de FFmpeg"
        logger.error("Modal input normalization failed for %s: %s", source_path.name, safe_detail)
        raise MediaError(f"No se pudo normalizar la entrada para Modal: {safe_detail}") from None

    normalized = probe_video(target)
    if (
        normalized.video_codec != "h264"
        or (normalized.width, normalized.height) != (1080, 1920)
        or not normalized.fps
        or abs(normalized.fps - 30) > 0.05
        or normalized.pixel_format != "yuv420p"
        or normalized.color_primaries != "bt709"
        or normalized.color_transfer != "bt709"
        or normalized.color_space != "bt709"
        or normalized.color_range != "tv"
        or not normalized.has_audio
        or abs(normalized.duration - video.duration) > max(0.1, 1 / 30)
    ):
        target.unlink(missing_ok=True)
        raise MediaError("La copia normalizada no cumple el perfil H.264/BT.709/30 FPS requerido por Modal.")
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        report = {}
    return normalized, {
        "converted": True,
        "method": "Remotion color profile + FFmpeg",
        "input": {
            "codec": video.video_codec,
            "width": video.width,
            "height": video.height,
            "fps": video.fps,
            "color_primaries": video.color_primaries,
            "color_transfer": video.color_transfer,
            "color_space": video.color_space,
            "color_range": video.color_range,
        },
        "output": {
            "codec": normalized.video_codec,
            "width": normalized.width,
            "height": normalized.height,
            "fps": normalized.fps,
            "pixel_format": normalized.pixel_format,
            "color_primaries": normalized.color_primaries,
            "color_transfer": normalized.color_transfer,
            "color_space": normalized.color_space,
            "color_range": normalized.color_range,
            "audio_codec": report.get("audio_codec", normalized.audio_codec),
            "audio_sample_rate": report.get("audio_sample_rate"),
            "audio_channels": report.get("audio_channels"),
        },
        "source_color_profile": report.get("source_color_profile", "no informado"),
        "original_preserved": True,
        "visual_color_review_required": True,
    }


def extract_audio_chunks(
    video: VideoInfo,
    work_dir: str | Path,
    chunk_seconds: int = 900,
) -> Iterator[tuple[Path, float]]:
    """Extract mono 16 kHz MP3 chunks; each is comfortably below API file limits."""
    if not video.has_audio:
        raise MediaError("The video has no audio stream to transcribe.")
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


def validate_rendered_output(
    source: VideoInfo,
    output_path: str | Path,
    style: VideoStyle,
    expected_duration_seconds: float | None = None,
) -> VideoInfo:
    output = Path(output_path)
    if not output.is_file() or output.stat().st_size <= 0:
        raise OutputValidationError("Rendered output is missing or empty.")
    if Path(source.path).resolve() == output.resolve():
        raise OutputValidationError("Rendered output must not overwrite the original video.")
    result = probe_video(output)
    if (result.width, result.height) != (style.output_width, style.output_height):
        raise OutputValidationError(
            f"Unexpected output size {result.width}×{result.height}; "
            f"expected {style.output_width}×{style.output_height}."
        )
    if result.video_codec != "h264":
        raise OutputValidationError(f"Expected H.264 output, received {result.video_codec}.")
    if not result.has_audio:
        raise OutputValidationError("Rendered video has no audio stream.")
    expected_duration = expected_duration_seconds if expected_duration_seconds is not None else source.duration
    tolerance = max(2.0, expected_duration * 0.02)
    if abs(result.duration - expected_duration) > tolerance:
        raise OutputValidationError(
            f"Output duration differs from the edited timeline by more than {tolerance:.1f} seconds."
        )
    if not result.fps or abs(result.fps - style.output_fps) > 1.5:
        raise OutputValidationError(
            f"Output frame rate {result.fps:.2f} does not match requested {style.output_fps} fps."
        )
    return result


def prepare_video_for_shotstack(
    video: VideoInfo,
    work_dir: str | Path,
) -> tuple[VideoInfo, dict[str, str | bool]]:
    """Normalize only HEVC or incompatible audio/rotation for broad H.264 editing support."""
    source_path = Path(video.path)
    needs_video_encode = video.video_codec == "hevc" or video.rotation in {90, 270}
    needs_audio_encode = video.audio_codec != "aac"
    if not needs_video_encode and not needs_audio_encode:
        return video, {
            "converted": False,
            "method": "none",
            "reason": "Container, H.264 video, AAC audio, and orientation are already compatible.",
        }

    target = Path(work_dir) / f"{source_path.stem}_compatible.mp4"
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "0",
        "-sn", "-dn",
    ]
    if needs_video_encode:
        is_10_bit = any(token in video.pixel_format.lower() for token in ("10", "12", "p010"))
        pixel_format = "yuv420p10le" if is_10_bit else "yuv420p"
        command.extend(
            [
                "-c:v", "libx264",
                "-preset", "slow",
                "-crf", "17",
                "-profile:v", "high10" if is_10_bit else "high",
                "-pix_fmt", pixel_format,
            ]
        )
        # Keep the source transfer/primaries/range metadata where it is known.
        for option, value in (
            ("-color_primaries", video.color_primaries),
            ("-color_trc", video.color_transfer),
            ("-colorspace", video.color_space),
        ):
            if value and value.lower() != "unknown":
                command.extend([option, value])
    else:
        # Remux the original video bit-for-bit; only transcode audio to AAC.
        command.extend(["-c:v", "copy"])
    if needs_audio_encode:
        command.extend(["-c:a", "aac", "-b:a", "256k"])
    else:
        command.extend(["-c:a", "copy"])
    command.extend(["-movflags", "+faststart", str(target)])
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=max(180, int(video.duration * 8)),
        )
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg is required to normalize this video but was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("FFmpeg timed out normalizing the video for editing.") from exc
    except subprocess.CalledProcessError as exc:
        logger.error("FFmpeg normalization failed: %s", exc.stderr[-2000:])
        raise MediaError("Could not normalize the video for editing.") from exc

    normalized = probe_video(target)
    if normalized.video_codec != "h264" or not normalized.has_audio:
        raise MediaError("FFmpeg output did not meet the H.264 video/audio compatibility checks.")
    reasons = []
    if video.video_codec == "hevc":
        reasons.append("HEVC encoded as high-quality H.264 (CRF 17, slow preset)")
    if needs_audio_encode:
        reasons.append("audio encoded as AAC at 256 kb/s")
    if video.rotation in {90, 270}:
        reasons.append("rotation baked into pixels")
    return normalized, {
        "converted": True,
        "method": "transcode" if needs_video_encode else "remux-video-and-transcode-audio",
        "reason": "; ".join(reasons),
    }

