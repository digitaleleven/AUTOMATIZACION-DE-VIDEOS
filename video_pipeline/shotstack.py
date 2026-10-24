"""Shotstack Ingest and Edit API client.

No requests are made by importing this module or building an edit payload.
The caller must explicitly authorize paid API calls before using the client.
"""

import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .models import SubtitleSegment
from .edit_plan import (
    edited_duration,
    map_subtitles_to_output,
    normalize_edit_plan,
    map_source_time_to_output,
    retained_source_ranges,
    sticker_svg,
)
from .style import VideoStyle

logger = logging.getLogger(__name__)


class ShotstackError(RuntimeError):
    """Raised for Shotstack API, ingestion, render, or download failures."""


def _redact_provider_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL redacted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|authorization)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return text.strip()[:limit]


def _provider_error_summary(response: requests.Response) -> str | None:
    """Extract bounded safe details from every structured error; never log raw bodies."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None

    candidates: list[dict[str, Any] | str] = []
    nested_keys = (
        "errors", "error", "response", "data", "details", "issues",
        "validationErrors", "validation_errors",
    )
    visited = 0

    def collect(value: Any, depth: int = 0) -> None:
        nonlocal visited
        if visited >= 48 or depth > 6:
            return
        visited += 1
        if isinstance(value, dict):
            if any(key in value for key in ("code", "title", "message", "detail", "source", "path", "pointer")):
                candidates.append(value)
            for key in nested_keys:
                child = value.get(key)
                if isinstance(child, (dict, list)):
                    collect(child, depth + 1)
                elif isinstance(child, str) and child.strip():
                    candidates.append(child)
        elif isinstance(value, list):
            for child in value[:12]:
                collect(child, depth + 1)
        elif isinstance(value, str) and value.strip():
            candidates.append(value)

    collect(payload)
    summaries: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            summary = _redact_provider_text(item, 280)
        else:
            fields: list[str] = []
            for field in ("code", "title", "message", "detail", "error", "response"):
                value = item.get(field)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    safe = _redact_provider_text(value, 220)
                    if safe and safe not in fields:
                        fields.append(safe)
            source = item.get("source")
            pointer = source.get("pointer") if isinstance(source, dict) else None
            pointer = pointer or item.get("path") or item.get("pointer")
            if isinstance(pointer, str) and re.fullmatch(r"/?[A-Za-z0-9_./~\-]{1,180}", pointer):
                fields.append(f"field={pointer}")
            summary = " | ".join(fields)
        if summary and summary not in seen:
            seen.add(summary)
            summaries.append(summary)
        if len(summaries) >= 8:
            break
    return " || ".join(summaries)[:1600] or None


def validate_shotstack_payload(payload: dict[str, Any]) -> None:
    """Fail locally on invalid output essentials or a misplaced video trim."""
    if not isinstance(payload, dict):
        raise ValueError("Shotstack edit payload must be an object.")
    timeline = payload.get("timeline")
    output = payload.get("output")
    tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("Shotstack edit payload must include timeline tracks.")
    video_assets = 0
    for track_index, track in enumerate(tracks):
        clips = track.get("clips") if isinstance(track, dict) else None
        if not isinstance(clips, list):
            raise ValueError(f"Shotstack track {track_index} must contain a clips array.")
        for clip_index, clip in enumerate(clips):
            if not isinstance(clip, dict) or not isinstance(clip.get("asset"), dict):
                raise ValueError(f"Shotstack clip {track_index}/{clip_index} must contain an asset.")
            if "trim" in clip:
                raise ValueError(
                    f"Shotstack Clip {track_index}/{clip_index} cannot contain trim; use asset.trim."
                )
            asset = clip["asset"]
            if asset.get("type") == "video":
                video_assets += 1
                trim = asset.get("trim")
                if (
                    isinstance(trim, bool)
                    or not isinstance(trim, (int, float))
                    or not math.isfinite(trim)
                    or trim < 0
                ):
                    raise ValueError(
                        f"Shotstack VideoAsset {track_index}/{clip_index} requires a non-negative asset.trim."
                    )
    if not video_assets:
        raise ValueError("Shotstack edit payload must include at least one video asset.")
    if not isinstance(output, dict) or output.get("format") != "mp4":
        raise ValueError("Shotstack output format must be MP4.")
    size = output.get("size")
    if not isinstance(size, dict) or any(
        isinstance(size.get(dimension), bool)
        or not isinstance(size.get(dimension), int)
        or not 1 <= size[dimension] <= 3840
        for dimension in ("width", "height")
    ):
        raise ValueError("Shotstack output dimensions are invalid.")
    fps = output.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Shotstack output FPS is invalid.")
    if output.get("quality") not in {"medium", "high"}:
        raise ValueError("Shotstack output quality must be medium or high.")


def build_edit_payload(
    source_url: str,
    duration: float,
    subtitles: list[SubtitleSegment],
    subtitle_url: str | None,
    style: VideoStyle | None = None,
    logo_url: str | None = None,
    source_width: int = 0,
    source_height: int = 0,
    subtitle_mode: str = "dynamic",
    cuts: list[dict[str, Any]] | None = None,
    zooms: list[dict[str, Any]] | None = None,
    stickers: list[dict[str, Any]] | None = None,
    subtitles_already_mapped: bool = False,
) -> dict[str, Any]:
    style = style or VideoStyle()
    if not source_url.startswith("https://"):
        raise ValueError("Shotstack source must be an HTTPS URL.")
    if subtitle_mode not in {"dynamic", "simple", "none"}:
        raise ValueError("subtitle_mode must be dynamic, simple, or none.")
    if subtitle_mode != "none" and (
        not subtitle_url or not subtitle_url.startswith("https://")
    ):
        raise ValueError("Shotstack subtitle source must be an HTTPS URL when subtitles are enabled.")
    if logo_url is not None and not logo_url.startswith("https://"):
        raise ValueError("Shotstack logo source must be an HTTPS URL.")
    if duration <= 0:
        raise ValueError("Video duration must be positive.")
    plan = normalize_edit_plan(
        {"cuts": cuts or [], "zooms": zooms or [], "stickers": stickers or []},
        duration,
    )
    cuts = plan["cuts"]
    zooms = plan["zooms"]
    stickers = plan["stickers"]
    if subtitle_mode != "none" and not subtitles:
        raise ValueError("At least one subtitle segment is required.")
    mapped_subtitles = (
        subtitles
        if subtitles_already_mapped
        else map_subtitles_to_output(subtitles, cuts)
    )
    if subtitle_mode != "none" and not any(
        caption.text.strip() and caption.start < edited_duration(duration, cuts) and caption.end > 0
        for caption in mapped_subtitles
    ):
        raise ValueError("No subtitle segment overlaps the video duration.")
    target_ratio = style.output_width / style.output_height
    source_ratio = source_width / source_height if source_width > 0 and source_height > 0 else target_ratio
    # Center-crop only a near-matching vertical source. Preserve all of a
    # differently-shaped source with letterboxing rather than cutting faces.
    video_fit = "crop" if abs(source_ratio - target_ratio) <= 0.015 else "contain"
    tracks = []
    if subtitle_mode != "none":
        caption_asset = {
            "type": "rich-caption",
            "src": subtitle_url,
            "font": {
                "family": style.font_family,
                "size": style.font_size,
                "weight": 700 if subtitle_mode == "dynamic" else 500,
                "color": style.font_color,
            },
            "align": {"vertical": "middle"},
            "stroke": {
                "width": style.stroke_width,
                "color": style.stroke_color,
                "opacity": 1,
            },
            "animation": {
                "style": style.animation_style if subtitle_mode == "dynamic" else "none"
            },
        }
        if subtitle_mode == "dynamic":
            caption_asset["active"] = {
                "font": {"color": style.highlight_color, "opacity": 1}
            }
        tracks.append(
            {
                "clips": [
                    {
                        "asset": caption_asset,
                        "start": 0,
                        "length": "end",
                        "width": 960,
                        "height": 250,
                        "position": style.caption_position,
                        "offset": {"x": 0, "y": style.caption_offset_y},
                    }
                ]
            }
        )
    if logo_url:
        tracks.append(
            {
                "clips": [
                    {
                        "asset": {"type": "image", "src": logo_url},
                        "start": 0,
                        "length": round(duration, 3),
                        "fit": "contain",
                        "scale": style.logo_scale,
                        "position": style.logo_position,
                        "offset": {"x": style.logo_offset_x, "y": style.logo_offset_y},
                    }
                ]
            }
        )
    sticker_clips = []
    for sticker in stickers:
        sticker_start = sticker["start_seconds"]
        sticker_end = sticker_start + sticker["duration_seconds"]
        for part_start, part_end in retained_source_ranges(sticker_start, sticker_end, cuts):
            output_start = map_source_time_to_output(part_start, cuts)
            output_end = map_source_time_to_output(part_end, cuts)
            if output_end - output_start <= 0.01:
                continue
            sticker_clips.append({
                "asset": {
                    "type": "svg",
                    "src": sticker_svg(sticker["sticker"], sticker["color"]),
                },
                "start": round(output_start, 3),
                "length": round(output_end - output_start, 3),
                "width": 260,
                "height": 260,
                "position": sticker["position"],
                "effect": sticker["effect"],
            })
    if sticker_clips:
        tracks.append({"clips": sticker_clips})

    video_clips = []
    boundaries = {0.0, float(duration)}
    for item in cuts + zooms:
        boundaries.add(float(item["start_seconds"]))
        boundaries.add(float(item["end_seconds"]))
    ordered_boundaries = sorted(boundaries)
    output_start = 0.0
    video_asset = {
        "type": "video",
        "src": source_url,
        "volume": 1.0,
        "transcode": True,
    }
    for source_start, source_end in zip(ordered_boundaries, ordered_boundaries[1:]):
        length = source_end - source_start
        if length <= 0.001:
            continue
        midpoint = (source_start + source_end) / 2
        if any(cut["start_seconds"] <= midpoint < cut["end_seconds"] for cut in cuts):
            continue
        asset = dict(video_asset)
        asset["trim"] = round(source_start, 3)
        clip = {
            "asset": asset,
            "start": round(output_start, 3),
            "length": round(length, 3),
            "fit": video_fit,
            "position": "center",
        }
        zoom = next(
            (
                item for item in zooms
                if item["start_seconds"] <= midpoint < item["end_seconds"]
            ),
            None,
        )
        if zoom:
            clip["effect"] = zoom["effect"]
        video_clips.append(clip)
        output_start += length
    if not video_clips:
        raise ValueError("The edit plan does not leave any video clips.")
    tracks.append({"clips": video_clips})
    payload = {
        "timeline": {
            "background": "#000000",
            "tracks": tracks,
        },
        "output": {
            "format": "mp4",
            "size": {"width": style.output_width, "height": style.output_height},
            "fps": style.output_fps,
            "quality": style.output_quality,
        },
    }
    validate_shotstack_payload(payload)
    return payload


class ShotstackClient:
    def __init__(
        self,
        api_key: str | None = None,
        environment: str = "stage",
        session: requests.Session | None = None,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
    ):
        key = api_key or os.getenv("SHOTSTACK_API_KEY")
        if not key:
            raise ShotstackError("SHOTSTACK_API_KEY is not set in Replit Secrets.")
        if environment != "stage":
            raise ValueError("This project is locked to Shotstack Stage. Production endpoints are disabled.")
        if poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("Shotstack polling and timeout values must be positive.")
        self._api_key = key
        self.environment = environment
        self.session = session or requests.Session()
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.ingest_base = f"https://api.shotstack.io/ingest/{environment}"
        self.edit_base = f"https://api.shotstack.io/edit/{environment}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Accept": "application/json"}

    @staticmethod
    def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(response, "status_code", "unknown")
            # Don't chain HTTP errors: their URL can contain a one-time signed
            # upload URL, which must not end up in logs or terminal output.
            detail = _provider_error_summary(response)
            if detail:
                logger.error(
                    "Shotstack %s failed (HTTP %s); provider detail: %s",
                    operation,
                    status,
                    detail,
                )
                message = f"Shotstack {operation} failed (HTTP {status}): {detail}"
            else:
                logger.error(
                    "Shotstack %s failed (HTTP %s); response had no safe structured detail.",
                    operation,
                    status,
                )
                message = f"Shotstack {operation} failed (HTTP {status})."
            raise ShotstackError(message) from None
        try:
            payload = response.json()
        except ValueError as exc:
            raise ShotstackError(f"Shotstack {operation} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ShotstackError(f"Shotstack {operation} returned an unexpected response.")
        return payload

    def upload_local_asset(self, asset_path: str | Path, content_type: str) -> str:
        path = Path(asset_path)
        if not path.is_file():
            raise ShotstackError(f"Local asset does not exist: {path.name}")
        if content_type not in {
            "video/mp4", "video/quicktime", "application/x-subrip",
            "image/png", "image/jpeg", "image/webp",
        }:
            raise ValueError(f"Unsupported Shotstack asset content type: {content_type}")
        try:
            response = self.session.post(
                f"{self.ingest_base}/upload",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"filename": path.name},
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            raise ShotstackError("Could not request a Shotstack upload URL.") from None
        data = self._json_response(response, "upload URL request").get("data", {})
        source_id = data.get("id")
        upload_url = data.get("attributes", {}).get("url")
        if not source_id or not upload_url:
            raise ShotstackError("Shotstack upload response did not include an upload ID and URL.")
        parsed = urlsplit(upload_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".amazonaws.com")
            or "shotstack-ingest" not in parsed.hostname
        ):
            raise ShotstackError("Shotstack returned an unexpected upload host; upload was stopped.")

        try:
            with path.open("rb") as source:
                upload_response = self.session.put(
                    upload_url,
                    data=source,
                    headers={"Content-Type": content_type},
                    timeout=(20, 600),
                )
                upload_response.raise_for_status()
        except requests.RequestException as exc:
            raise ShotstackError(f"Could not upload {path.name} to Shotstack Ingest.") from None
        except OSError as exc:
            raise ShotstackError(f"Could not read local video {path.name}.") from exc

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            source_payload = self._get_json(f"{self.ingest_base}/sources/{source_id}", "source status")
            source_data = source_payload.get("data", {})
            status = source_data.get("attributes", {}).get("status")
            if status == "ready":
                source_url = source_data.get("attributes", {}).get("source")
                if not source_url or not source_url.startswith("https://"):
                    raise ShotstackError("Shotstack marked the source ready without a valid HTTPS source URL.")
                return source_url
            if status in {"failed", "deleted"}:
                raise ShotstackError(f"Shotstack source ingestion ended with status '{status}'.")
            time.sleep(self.poll_seconds)
        raise ShotstackError("Timed out waiting for Shotstack to ingest the video.")

    def upload_local_video(self, video_path: str | Path) -> str:
        path = Path(video_path)
        content_type = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"
        return self.upload_local_asset(path, content_type)

    def submit_render(self, payload: dict[str, Any]) -> str:
        try:
            response = self.session.post(
                f"{self.edit_base}/render",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            raise ShotstackError("Could not submit the Shotstack render.") from None
        data = self._json_response(response, "render submission").get("data", {})
        render_id = data.get("id")
        if not render_id:
            raise ShotstackError("Shotstack render response did not include a render ID.")
        return str(render_id)

    def wait_for_render(self, render_id: str) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            status, url = self.render_status(render_id)
            if status == "done":
                if url and url.startswith("https://"):
                    return url
                raise ShotstackError("Shotstack completed the render without returning an HTTPS URL.")
            if status in {"failed", "cancelled"}:
                raise ShotstackError(f"Shotstack render ended with status '{status}'.")
            time.sleep(self.poll_seconds)
        raise ShotstackError(f"Timed out waiting for Shotstack render {render_id}.")

    def render_status(self, render_id: str) -> tuple[str, str | None]:
        payload = self._get_json(f"{self.edit_base}/render/{render_id}", "render status")
        attributes = payload.get("data", {}).get("attributes", {})
        return str(attributes.get("status") or "unknown"), attributes.get("url")

    def download_render(self, url: str, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        created_target = False
        try:
            with self.session.get(url, stream=True, timeout=(20, 300)) as response:
                response.raise_for_status()
                with target.open("xb") as output:
                    created_target = True
                    for block in response.iter_content(chunk_size=1024 * 1024):
                        if block:
                            output.write(block)
        except FileExistsError as exc:
            raise ShotstackError(f"Refusing to overwrite existing output: {target}") from exc
        except (requests.RequestException, OSError) as exc:
            try:
                if created_target and target.exists():
                    target.unlink()
            except OSError:
                logger.exception("Could not remove incomplete output file %s", target)
            raise ShotstackError(f"Could not download the rendered video to {target}.") from None
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise ShotstackError("Shotstack returned an empty render file.")
        return target

    def _get_json(self, url: str, operation: str) -> dict[str, Any]:
        try:
            response = self.session.get(url, headers=self._headers, timeout=(10, 60))
        except requests.RequestException as exc:
            raise ShotstackError(f"Could not retrieve Shotstack {operation}.") from None
        return self._json_response(response, operation)

