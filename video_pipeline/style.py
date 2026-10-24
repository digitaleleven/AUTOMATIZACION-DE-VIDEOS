"""Validated, user-editable visual and usage-limit configuration."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid


class StyleConfigError(ValueError):
    """Raised for invalid video style configuration."""


@dataclass(frozen=True)
class VideoStyle:
    font_family: str = "Roboto"
    font_size: int = 64
    font_color: str = "#FFFFFF"
    highlight_color: str = "#FFD84D"
    stroke_color: str = "#000000"
    stroke_width: int = 4
    animation_style: str = "highlight"
    caption_position: str = "bottom"
    caption_offset_y: float = 0.22
    emoji_by_keyword: dict[str, str] | None = None
    logo_path: str | None = None
    logo_position: str = "topRight"
    logo_scale: float = 0.12
    logo_offset_x: float = -0.06
    logo_offset_y: float = -0.08
    output_width: int = 1080
    output_height: int = 1920
    output_fps: int = 30
    output_quality: str = "high"
    # Kept only to accept legacy config files; input size is no longer capped.
    max_input_size_mb: int | None = None
    max_video_duration_seconds: int = 1800
    max_transcription_chunks: int = 2
    openai_price_per_audio_minute_usd: float | None = None
    shotstack_price_per_render_minute_usd: float | None = None
    max_estimated_cost_usd: float | None = None


def _validate_hex(value: str, key: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        raise StyleConfigError(f"{key} must be a six-digit hex color such as #FFFFFF.")
    return value.upper()


def load_video_style(path: str | Path = "video_style.json") -> VideoStyle:
    config_path = Path(path)
    if not config_path.exists():
        return VideoStyle()
    try:
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StyleConfigError(f"Could not read style config {config_path}.") from exc
    if not isinstance(raw, dict):
        raise StyleConfigError("Style configuration must be a JSON object.")

    defaults = VideoStyle()
    values = {field: getattr(defaults, field) for field in defaults.__dataclass_fields__}
    unknown = set(raw) - set(values)
    if unknown:
        raise StyleConfigError(f"Unknown style setting(s): {', '.join(sorted(unknown))}")
    values.update(raw)

    for key in ("font_color", "highlight_color", "stroke_color"):
        values[key] = _validate_hex(values[key], key)
    if not isinstance(values["font_family"], str) or not values["font_family"].strip():
        raise StyleConfigError("font_family must be a non-empty font name.")
    if not 12 <= int(values["font_size"]) <= 200:
        raise StyleConfigError("font_size must be between 12 and 200.")
    if not 0 <= int(values["stroke_width"]) <= 20:
        raise StyleConfigError("stroke_width must be between 0 and 20.")
    if values["animation_style"] not in {"highlight", "karaoke"}:
        raise StyleConfigError("animation_style must be 'highlight' or 'karaoke'.")
    if values["caption_position"] not in {"bottom", "middle", "top"}:
        raise StyleConfigError("caption_position must be bottom, middle, or top.")
    if not 0 <= float(values["caption_offset_y"]) <= 0.8:
        raise StyleConfigError("caption_offset_y must be between 0 and 0.8.")
    if values["logo_position"] not in {
        "top", "topRight", "right", "bottomRight", "bottom",
        "bottomLeft", "left", "topLeft", "center",
    }:
        raise StyleConfigError("logo_position must be one of Shotstack's nine clip positions.")
    if not 0.02 <= float(values["logo_scale"]) <= 0.4:
        raise StyleConfigError("logo_scale must be between 0.02 and 0.4.")
    if any(not -1 <= float(values[key]) <= 1 for key in ("logo_offset_x", "logo_offset_y")):
        raise StyleConfigError("Logo offsets must be between -1 and 1.")
    if (int(values["output_width"]), int(values["output_height"])) not in {
        (720, 1280), (1080, 1920),
    }:
        raise StyleConfigError("Output must use the 720 × 1280 or 1080 × 1920 portrait profile.")
    if int(values["output_fps"]) not in {25, 30, 50, 60}:
        raise StyleConfigError("output_fps must be one of 25, 30, 50, or 60.")
    if not isinstance(values["output_quality"], str) or values["output_quality"] not in {"medium", "high"}:
        raise StyleConfigError("output_quality must be 'medium' or 'high'.")
    legacy_size_limit = values["max_input_size_mb"]
    if legacy_size_limit is not None and int(legacy_size_limit) <= 0:
        raise StyleConfigError("max_input_size_mb must be positive when provided.")
    values["max_input_size_mb"] = None
    if int(values["max_video_duration_seconds"]) <= 0:
        raise StyleConfigError("Video duration limit must be positive.")
    if int(values["max_transcription_chunks"]) <= 0:
        raise StyleConfigError("max_transcription_chunks must be positive.")
    for key in (
        "openai_price_per_audio_minute_usd",
        "shotstack_price_per_render_minute_usd",
        "max_estimated_cost_usd",
    ):
        rate = values[key]
        if rate is not None:
            if not math.isfinite(float(rate)) or float(rate) < 0:
                raise StyleConfigError(f"{key} must be a finite, non-negative number.")
    if values["logo_path"] is not None and not isinstance(values["logo_path"], str):
        raise StyleConfigError("logo_path must be a local file path or null.")
    emojis = values["emoji_by_keyword"]
    if emojis is not None and (
        not isinstance(emojis, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in emojis.items())
    ):
        raise StyleConfigError("emoji_by_keyword must map text keywords to emoji strings.")

    # Normalize numeric types so JSON values are checked and consumed consistently.
    for key in ("font_size", "stroke_width", "output_width", "output_height", "output_fps",
                "max_video_duration_seconds", "max_transcription_chunks"):
        values[key] = int(values[key])
    for key in (
        "caption_offset_y", "logo_scale", "logo_offset_x", "logo_offset_y",
        "openai_price_per_audio_minute_usd", "shotstack_price_per_render_minute_usd",
        "max_estimated_cost_usd",
    ):
        if values[key] is None:
            continue
        values[key] = float(values[key])
    return VideoStyle(**values)


def save_video_style(data: dict[str, Any], path: str | Path = "video_style.json") -> VideoStyle:
    """Validate the full style before atomically replacing the configuration."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        validated = load_video_style(temporary)
        os.replace(temporary, target)
        return validated
    finally:
        temporary.unlink(missing_ok=True)
