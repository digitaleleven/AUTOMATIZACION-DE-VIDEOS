"""Deterministic visual-effect cues from already-timed transcript words.

This module does not call transcription or rendering providers. It only turns
word timings that already exist into conservative, cut-aware visual cues.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from .edit_plan import edited_duration, map_source_time_to_output
from .models import WordTiming


def retained_source_ranges(
    removed_ranges: list[Mapping[str, Any]],
    source_duration: float,
) -> list[tuple[float, float]]:
    """Convert approved silence-removal intervals into source ranges to keep."""
    if isinstance(source_duration, bool) or not math.isfinite(float(source_duration)) or float(source_duration) <= 0:
        raise ValueError("La duración de origen debe ser positiva y finita.")
    duration = float(source_duration)
    removals: list[tuple[float, float]] = []
    for item in removed_ranges:
        try:
            start = float(item.get("start_seconds", item.get("start")))
            end = float(item.get("end_seconds", item.get("end")))
        except (TypeError, ValueError):
            raise ValueError("Cada corte debe incluir un inicio y un fin numéricos.") from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration:
            raise ValueError("Los intervalos que se eliminan deben estar dentro de la duración de origen.")
        removals.append((start, end))
    removals.sort()
    retained: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in removals:
        if start < cursor - 1e-6:
            raise ValueError("Los intervalos de corte se solapan.")
        if start > cursor + 1e-6:
            retained.append((cursor, start))
        cursor = end
    if cursor < duration - 1e-6:
        retained.append((cursor, duration))
    if not retained:
        raise ValueError("Los intervalos de corte eliminan todo el vídeo.")
    return retained


_EFFECTS_BY_TOKEN: dict[str, tuple[str, float, tuple[str, ...]]] = {
    "mua": ("kiss", 0.85, ("kiss-lips", "kiss-heart")),
    "tonico": ("droplet", 0.95, ("water-drop",)),
    "perlado": ("pearl", 1.05, ("pearl-glints",)),
}

_SVG_ASSETS: dict[str, str] = {
    "kiss-lips": """<svg xmlns="http://www.w3.org/2000/svg" width="200" height="150" viewBox="0 0 200 150">
<metadata>Original MATRIX MOTION vector artwork; no third-party asset.</metadata>
<defs><linearGradient id="lip" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#F58EA5"/><stop offset="1" stop-color="#B92E59"/></linearGradient></defs>
<path d="M18 76 C48 48 76 51 100 68 C124 51 153 48 182 76 C155 93 131 119 100 121 C69 119 45 93 18 76Z" fill="url(#lip)" stroke="#9F274D" stroke-width="4" stroke-linejoin="round"/>
<path d="M38 76 C61 66 80 68 100 79 C120 68 139 66 162 76 C143 90 123 98 100 98 C77 98 57 90 38 76Z" fill="#66233F"/>
<path d="M54 57 C69 55 82 61 93 68" fill="none" stroke="#FFD4DD" stroke-width="5" stroke-linecap="round" opacity=".85"/>
</svg>""",
    "kiss-heart": """<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
<metadata>Original MATRIX MOTION vector artwork; no third-party asset.</metadata>
<path d="M50 84 C41 75 16 57 16 37 C16 19 38 13 50 30 C62 13 84 19 84 37 C84 57 59 75 50 84Z" fill="#E85D83" stroke="#A92D56" stroke-width="3"/>
<path d="M31 34 C35 28 42 29 46 35" fill="none" stroke="#FFDDE5" stroke-width="4" stroke-linecap="round"/>
</svg>""",
    "water-drop": """<svg xmlns="http://www.w3.org/2000/svg" width="110" height="150" viewBox="0 0 110 150">
<metadata>Original MATRIX MOTION vector artwork; no third-party asset.</metadata>
<defs><linearGradient id="water" x1=".2" y1=".1" x2=".8" y2="1"><stop stop-color="#D6FAF3"/><stop offset=".48" stop-color="#73D9D0"/><stop offset="1" stop-color="#3E9FA7"/></linearGradient></defs>
<path d="M55 8 C45 29 19 58 19 88 C19 112 35 132 55 132 C75 132 91 112 91 88 C91 58 65 29 55 8Z" fill="url(#water)" stroke="#F1FFFC" stroke-width="4"/>
<path d="M39 80 C39 68 46 55 54 43" fill="none" stroke="#FFFFFF" stroke-width="6" stroke-linecap="round" opacity=".9"/>
<circle cx="69" cy="102" r="4" fill="#E8FFFB" opacity=".85"/>
</svg>""",
    "pearl-glints": """<svg xmlns="http://www.w3.org/2000/svg" width="150" height="150" viewBox="0 0 150 150">
<metadata>Original MATRIX MOTION vector artwork; no third-party asset.</metadata>
<g fill="#FFF0BE" stroke="#D7AD58" stroke-width="2" stroke-linejoin="round">
<path d="M45 9 L52 37 L80 45 L52 52 L45 81 L37 52 L9 45 L37 37Z"/>
<path d="M108 58 L114 78 L134 84 L114 90 L108 111 L102 90 L82 84 L102 78Z"/>
<path d="M47 103 L51 117 L65 121 L51 125 L47 140 L43 125 L29 121 L43 117Z"/>
</g>
<circle cx="46" cy="45" r="5" fill="#FFFFFF"/>
<circle cx="109" cy="84" r="4" fill="#FFFFFF"/>
</svg>""",
}


def _word_fields(word: WordTiming | Mapping[str, Any]) -> tuple[str, float, float]:
    if isinstance(word, WordTiming):
        text, start, end = word.text, word.start, word.end
    elif isinstance(word, Mapping):
        text = word.get("word", word.get("text", ""))
        start, end = word.get("start"), word.get("end")
    else:
        raise ValueError("Cada marca temporal debe ser WordTiming o un objeto con palabra, inicio y fin.")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("La palabra de una marca temporal no puede estar vacía.")
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValueError("Las marcas temporales deben ser números finitos.")
    try:
        start_value, end_value = float(start), float(end)
    except (TypeError, ValueError):
        raise ValueError("Las marcas temporales deben ser números finitos.") from None
    if not math.isfinite(start_value) or not math.isfinite(end_value) or start_value < 0 or end_value < start_value:
        raise ValueError("Las marcas temporales deben ser finitas y tener fin igual o posterior al inicio.")
    return text, start_value, end_value


def _normalize_token(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(re.findall(r"[a-z0-9]+", without_marks))


def plan_semantic_effects(
    words: Iterable[WordTiming | Mapping[str, Any]],
    cuts: list[dict[str, Any]],
    source_duration: float,
) -> list[dict[str, Any]]:
    """Plan small, cut-aware overlays from exact existing word timings.

    Cue start times always equal the mapped start time of the triggering word.
    No cue is inferred from a loose phrase or from segment-level text.
    """
    if not math.isfinite(source_duration) or source_duration <= 0:
        raise ValueError("La duración de origen debe ser positiva y finita.")
    output_duration = edited_duration(source_duration, cuts)
    cues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_word in words:
        text, source_start, source_end = _word_fields(raw_word)
        token = _normalize_token(text)
        if token not in _EFFECTS_BY_TOKEN or token in seen:
            continue
        effect, preferred_duration, assets = _EFFECTS_BY_TOKEN[token]
        output_start = map_source_time_to_output(source_start, cuts)
        output_word_end = map_source_time_to_output(source_end, cuts)
        # A trigger removed by a cut must not create an orphaned overlay.
        if output_word_end - output_start <= 0.02 or output_start >= output_duration:
            continue
        duration = min(preferred_duration, output_duration - output_start)
        if duration < 0.2:
            continue
        cues.append(
            {
                "effect": effect,
                "trigger_word": token,
                "source_start_seconds": round(source_start, 3),
                "source_end_seconds": round(source_end, 3),
                "start_seconds": round(output_start, 3),
                "duration_seconds": round(duration, 3),
                "position": "topRight",
                "assets": list(assets),
            }
        )
        seen.add(token)
    return sorted(cues, key=lambda item: item["start_seconds"])


def semantic_effect_svg(asset_name: str) -> str:
    """Return a fixed transparent SVG; no remote assets or user markup."""
    try:
        return _SVG_ASSETS[asset_name]
    except KeyError:
        raise ValueError(f"Recurso de efecto no permitido: {asset_name!r}.") from None


_OVERLAY_LAYOUTS: dict[str, tuple[int, str, str]] = {
    # width, x expression, y expression. Every placement stays in the upper-right
    # safe area, away from the speaker's face, hands, and product.
    "kiss-lips": (150, "875+42*P", "72-14*P"),
    "kiss-heart": (58, "930+78*P", "114-82*P"),
    "water-drop": (72, "956+10*P", "150-54*P"),
    "pearl-glints": (104, "904+38*P", "66-18*P"),
}


def semantic_overlay_input_specs(
    cues: Iterable[Mapping[str, Any]],
    asset_directory: str,
    *,
    first_input_index: int = 1,
    loop_duration_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return allowlisted SVG inputs in the exact order used by the filter graph."""
    specs: list[dict[str, Any]] = []
    cue_list = list(cues)
    cue_end_times = []
    for cue in cue_list:
        try:
            cue_end_times.append(float(cue["start_seconds"]) + float(cue["duration_seconds"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cada efecto debe incluir inicio y duración válidos.") from None
    if loop_duration_seconds is None:
        loop_duration_seconds = max(cue_end_times, default=0.0)
    if (
        isinstance(loop_duration_seconds, bool)
        or not math.isfinite(float(loop_duration_seconds))
        or float(loop_duration_seconds) <= 0
    ):
        raise ValueError("La duración de las entradas SVG debe ser positiva y finita.")
    stream_duration = float(loop_duration_seconds)
    if any(end > stream_duration + 1e-6 for end in cue_end_times):
        raise ValueError("Una entrada SVG termina antes de su efecto programado.")
    index = first_input_index
    for cue in cue_list:
        effect = cue.get("effect")
        assets = cue.get("assets")
        start = cue.get("start_seconds")
        duration = cue.get("duration_seconds")
        if not isinstance(assets, list) or not assets:
            raise ValueError("Cada efecto necesita al menos un recurso vectorial autorizado.")
        if isinstance(start, bool) or isinstance(duration, bool):
            raise ValueError("El inicio y la duración de cada efecto deben ser números.")
        try:
            start_value, duration_value = float(start), float(duration)
        except (TypeError, ValueError):
            raise ValueError("El inicio y la duración de cada efecto deben ser números.") from None
        if not math.isfinite(start_value) or not math.isfinite(duration_value) or start_value < 0 or duration_value <= 0:
            raise ValueError("El inicio debe ser no negativo y la duración positiva.")
        for asset_name in assets:
            if asset_name not in _OVERLAY_LAYOUTS or asset_name not in _SVG_ASSETS:
                raise ValueError(f"Recurso de efecto no permitido: {asset_name!r}.")
            specs.append(
                {
                    "input_index": index,
                    "effect": effect,
                    "asset_name": asset_name,
                    "path": f"{asset_directory.rstrip('/')}/{asset_name}.svg",
                    "start_seconds": start_value,
                    "effect_duration_seconds": duration_value,
                    "stream_duration_seconds": stream_duration,
                }
            )
            index += 1
    return specs


def build_semantic_overlay_filter(
    cues: Iterable[Mapping[str, Any]],
    *,
    first_input_index: int = 1,
    base_label: str = "vbase",
) -> tuple[str, str, int]:
    """Build a local FFmpeg overlay graph with fade and semantic movement.

    The caller must provide looped 30 fps SVG inputs in the order returned by
    :func:`semantic_overlay_input_specs`.
    """
    graph: list[str] = []
    current_label = base_label
    cue_list = list(cues)
    specs = semantic_overlay_input_specs(cue_list, "effects", first_input_index=first_input_index)
    overlay_index = 0
    for spec in specs:
        start = spec["start_seconds"]
        duration = spec["effect_duration_seconds"]
        end = start + duration
        fade_in = min(0.14, duration / 3)
        fade_out = min(0.24, duration / 3)
        fade_out_start = max(start, end - fade_out)
        asset_name = spec["asset_name"]
        width, x_template, y_template = _OVERLAY_LAYOUTS[asset_name]
        p = f"((t-{start:.3f})/{duration:.3f})"
        x_expr = x_template.replace("P", p)
        y_expr = y_template.replace("P", p)
        overlay_stream = f"fxin{overlay_index}"
        next_label = f"vfx{overlay_index}"
        graph.append(
            f"[{spec['input_index']}:v]format=rgba,scale={width}:-1:flags=lanczos,"
            f"fade=t=in:st={start:.3f}:d={fade_in:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}:alpha=1,"
            f"setpts=PTS-STARTPTS[{overlay_stream}]"
        )
        graph.append(
            f"[{current_label}][{overlay_stream}]overlay="
            f"x='{x_expr}':y='{y_expr}':eval=frame:"
            f"enable='between(t,{start:.3f},{end:.3f})':"
            f"eof_action=pass:repeatlast=0:alpha=straight[{next_label}]"
        )
        current_label = next_label
        overlay_index += 1
    return ";".join(graph), current_label, overlay_index
