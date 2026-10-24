"""Validated, per-video creative edit choices shared by the panel and renderer."""

from __future__ import annotations

import math
import re
from typing import Any

from .models import SubtitleSegment


STICKER_PATHS = {
    "sparkle": "M100 8 L119 76 L188 100 L119 124 L100 192 L81 124 L12 100 L81 76 Z",
    "star": "M100 8 L123 72 L192 76 L139 119 L157 188 L100 149 L43 188 L61 119 L8 76 L77 72 Z",
    "heart": "M100 178 C82 160 24 116 24 69 C24 25 79 17 100 58 C121 17 176 25 176 69 C176 116 118 160 100 178 Z",
    "bolt": "M116 8 L47 109 L91 109 L80 192 L153 88 L109 88 Z",
}

ALLOWED_POSITIONS = {
    "top", "topRight", "right", "bottomRight", "bottom",
    "bottomLeft", "left", "topLeft", "center",
}
ALLOWED_STICKER_EFFECTS = {"zoomIn", "zoomOut"}
MAX_PLAN_ITEMS = 12


class EditPlanError(ValueError):
    """Raised when a per-video creative plan is invalid."""


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise EditPlanError(f"{label} debe ser un número.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise EditPlanError(f"{label} debe ser un número.") from None
    if not math.isfinite(number):
        raise EditPlanError(f"{label} debe ser finito.")
    return number


def normalize_edit_plan(raw: Any, duration: float) -> dict[str, list[dict[str, Any]]]:
    """Validate source-timeline cut, zoom and independent sticker ranges."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise EditPlanError("El plan creativo debe ser un objeto.")
    if not math.isfinite(duration) or duration <= 0:
        raise EditPlanError("La duración del vídeo no es válida.")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for name in ("cuts", "zooms", "stickers"):
        entries = raw.get(name, [])
        if not isinstance(entries, list) or len(entries) > MAX_PLAN_ITEMS:
            raise EditPlanError(f"El plan admite como máximo {MAX_PLAN_ITEMS} elementos de tipo {name}.")
        normalized[name] = []

    for index, item in enumerate(raw.get("cuts", [])):
        if not isinstance(item, dict):
            raise EditPlanError("Cada corte debe incluir un inicio y un fin.")
        start = _finite_number(item.get("start_seconds"), f"Corte {index + 1}: inicio")
        end = _finite_number(item.get("end_seconds"), f"Corte {index + 1}: fin")
        if start < 0 or end > duration or end - start < 0.05:
            raise EditPlanError(f"El corte {index + 1} debe estar dentro del vídeo y durar al menos 0,05 s.")
        normalized["cuts"].append({"start_seconds": round(start, 3), "end_seconds": round(end, 3)})

    normalized["cuts"].sort(key=lambda item: item["start_seconds"])
    for previous, current in zip(normalized["cuts"], normalized["cuts"][1:]):
        if current["start_seconds"] < previous["end_seconds"]:
            raise EditPlanError("Los intervalos de corte no pueden solaparse.")
    remaining = duration - sum(item["end_seconds"] - item["start_seconds"] for item in normalized["cuts"])
    if remaining < 1:
        raise EditPlanError("Los cortes deben dejar al menos 1 segundo de vídeo.")

    for index, item in enumerate(raw.get("zooms", [])):
        if not isinstance(item, dict):
            raise EditPlanError("Cada zoom debe incluir inicio, fin y efecto.")
        start = _finite_number(item.get("start_seconds"), f"Zoom {index + 1}: inicio")
        end = _finite_number(item.get("end_seconds"), f"Zoom {index + 1}: fin")
        effect = item.get("effect")
        if start < 0 or end > duration or end - start < 0.1:
            raise EditPlanError(f"El zoom {index + 1} debe estar dentro del vídeo y durar al menos 0,1 s.")
        if effect not in {"zoomIn", "zoomOut"}:
            raise EditPlanError("El efecto de zoom debe ser acercamiento o alejamiento.")
        normalized["zooms"].append({
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "effect": effect,
        })
    normalized["zooms"].sort(key=lambda item: item["start_seconds"])
    for previous, current in zip(normalized["zooms"], normalized["zooms"][1:]):
        if current["start_seconds"] < previous["end_seconds"]:
            raise EditPlanError("Los intervalos de zoom no pueden solaparse.")

    for index, item in enumerate(raw.get("stickers", [])):
        if not isinstance(item, dict):
            raise EditPlanError("Cada sticker debe ser un objeto.")
        sticker = item.get("sticker")
        position = item.get("position", "topRight")
        effect = item.get("effect", "zoomIn")
        start = _finite_number(item.get("start_seconds"), f"Sticker {index + 1}: inicio")
        length = _finite_number(item.get("duration_seconds"), f"Sticker {index + 1}: duración")
        color = item.get("color", "#FFD84D")
        if sticker not in STICKER_PATHS:
            raise EditPlanError(f"Sticker no permitido: {sticker!r}.")
        if position not in ALLOWED_POSITIONS:
            raise EditPlanError(f"Posición no permitida para el sticker {index + 1}.")
        if effect not in ALLOWED_STICKER_EFFECTS:
            raise EditPlanError("La animación del sticker debe ser acercamiento o alejamiento.")
        if start < 0 or length < 0.2 or start + length > duration:
            raise EditPlanError(f"El sticker {index + 1} debe estar dentro del vídeo y durar al menos 0,2 s.")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise EditPlanError(f"El color del sticker {index + 1} debe ser hexadecimal #RRGGBB.")
        normalized["stickers"].append({
            "sticker": sticker,
            "start_seconds": round(start, 3),
            "duration_seconds": round(length, 3),
            "position": position,
            "effect": effect,
            "color": color.upper(),
        })

    normalized["stickers"].sort(key=lambda item: item["start_seconds"])
    return normalized


def map_source_time_to_output(source_time: float, cuts: list[dict[str, Any]]) -> float:
    """Map an input-time point to the compressed output timeline."""
    removed = 0.0
    for cut in cuts:
        start = cut["start_seconds"]
        end = cut["end_seconds"]
        removed += max(0.0, min(source_time, end) - start)
        if source_time <= end:
            break
    return max(0.0, source_time - removed)


def retained_source_ranges(
    start: float,
    end: float,
    cuts: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """Return the portions of a source interval not removed by cuts."""
    ranges = [(start, end)]
    for cut in cuts:
        next_ranges: list[tuple[float, float]] = []
        cut_start, cut_end = cut["start_seconds"], cut["end_seconds"]
        for range_start, range_end in ranges:
            if cut_end <= range_start or cut_start >= range_end:
                next_ranges.append((range_start, range_end))
                continue
            if cut_start > range_start:
                next_ranges.append((range_start, cut_start))
            if cut_end < range_end:
                next_ranges.append((cut_end, range_end))
        ranges = next_ranges
    return [(left, right) for left, right in ranges if right - left > 0.01]


def map_subtitles_to_output(
    subtitles: list[SubtitleSegment],
    cuts: list[dict[str, Any]],
) -> list[SubtitleSegment]:
    """Shift timed captions with the same cuts applied to picture and audio."""
    mapped: list[SubtitleSegment] = []
    for subtitle in subtitles:
        start = map_source_time_to_output(subtitle.start, cuts)
        end = map_source_time_to_output(subtitle.end, cuts)
        if end - start > 0.02:
            mapped.append(SubtitleSegment(subtitle.text, round(start, 3), round(end, 3)))
    return mapped


def edited_duration(duration: float, cuts: list[dict[str, Any]]) -> float:
    return round(duration - sum(cut["end_seconds"] - cut["start_seconds"] for cut in cuts), 3)


def sticker_svg(name: str, color: str) -> str:
    """Build a fixed, safe inline SVG sticker (no user-provided markup)."""
    path = STICKER_PATHS.get(name)
    if not path or not re.fullmatch(r"#[0-9A-F]{6}", color):
        raise EditPlanError("Sticker o color no permitido.")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
        '<circle cx="100" cy="100" r="96" fill="#FFFFFF"/>'
        f'<path d="{path}" fill="{color}"/>'
        "</svg>"
    )
