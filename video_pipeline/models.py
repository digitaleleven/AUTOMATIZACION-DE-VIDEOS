"""Typed data structures shared by transcription and rendering."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    has_audio: bool
    video_codec: str = "unknown"
    audio_codec: str | None = None
    fps: float = 0.0
    size_bytes: int = 0
    rotation: int = 0
    pixel_format: str = "unknown"
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    container: str = "unknown"


@dataclass(frozen=True)
class WordTiming:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class SubtitleSegment:
    text: str
    start: float
    end: float


def dataclass_to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)

