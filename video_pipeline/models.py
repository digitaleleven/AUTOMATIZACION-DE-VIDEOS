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

