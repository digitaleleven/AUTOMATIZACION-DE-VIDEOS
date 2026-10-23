"""Shotstack Ingest and Edit API client.

No requests are made by importing this module or building an edit payload.
The caller must explicitly authorize paid API calls before using the client.
"""

import logging
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from .models import SubtitleSegment

logger = logging.getLogger(__name__)


class ShotstackError(RuntimeError):
    """Raised for Shotstack API, ingestion, render, or download failures."""


def build_edit_payload(source_url: str, duration: float, subtitles: list[SubtitleSegment]) -> dict[str, Any]:
    if not source_url.startswith("https://"):
        raise ValueError("Shotstack source must be an HTTPS URL.")
    if duration <= 0:
        raise ValueError("Video duration must be positive.")
    if not subtitles:
        raise ValueError("At least one subtitle segment is required.")

    caption_clips = []
    for caption in subtitles:
        start = max(0.0, caption.start)
        end = min(duration, caption.end)
        if not caption.text.strip() or end <= start:
            continue
        caption_clips.append(
            {
                "asset": {
                    "type": "rich-text",
                    "text": caption.text,
                    "width": 920,
                    "height": 190,
                    "font": {
                        "family": "Arial",
                        "size": 68,
                        "weight": "700",
                        "color": "#FFFFFF",
                    },
                    "align": {"horizontal": "center", "vertical": "middle"},
                    "background": {
                        "color": "#000000",
                        "opacity": 0.72,
                        "padding": 18,
                        "borderRadius": 14,
                    },
                },
                "start": round(start, 3),
                "length": round(end - start, 3),
                "position": "bottom",
            }
        )
    if not caption_clips:
        raise ValueError("No subtitle segments overlap the video duration.")

    return {
        "timeline": {
            "background": "#000000",
            "tracks": [
                {"clips": caption_clips},
                {
                    "clips": [
                        {
                            "asset": {
                                "type": "video",
                                "src": source_url,
                                "volume": 1.0,
                                "fit": "crop",
                                "transcode": True,
                            },
                            "start": 0,
                            "length": round(duration, 3),
                            "position": "center",
                        }
                    ]
                },
            ],
        },
        "output": {
            "format": "mp4",
            "size": {"width": 1080, "height": 1920},
            "fps": 30,
        },
    }


class ShotstackClient:
    def __init__(
        self,
        api_key: str | None = None,
        environment: str = "v1",
        session: requests.Session | None = None,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
    ):
        key = api_key or os.getenv("SHOTSTACK_API_KEY")
        if not key:
            raise ShotstackError("SHOTSTACK_API_KEY is not set in Replit Secrets.")
        if environment not in {"v1", "stage"}:
            raise ValueError("Shotstack environment must be 'v1' or 'stage'.")
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
            raise ShotstackError(f"Shotstack {operation} failed (HTTP {status}).") from None
        try:
            payload = response.json()
        except ValueError as exc:
            raise ShotstackError(f"Shotstack {operation} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ShotstackError(f"Shotstack {operation} returned an unexpected response.")
        return payload

    def upload_local_video(self, video_path: str | Path) -> str:
        path = Path(video_path)
        try:
            response = self.session.post(
                f"{self.ingest_base}/upload",
                headers=self._headers,
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
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".amazonaws.com"):
            raise ShotstackError("Shotstack returned an unexpected upload host; upload was stopped.")

        try:
            with path.open("rb") as source:
                upload_response = self.session.put(
                    upload_url,
                    data=source,
                    headers={"Content-Type": "video/mp4"},
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
            payload = self._get_json(f"{self.edit_base}/render/{render_id}", "render status")
            data = payload.get("data", {})
            attributes = data.get("attributes", {})
            status = attributes.get("status")
            if status == "done":
                url = attributes.get("url")
                if url and url.startswith("https://"):
                    return url
                raise ShotstackError("Shotstack completed the render without returning an HTTPS URL.")
            if status in {"failed", "cancelled"}:
                raise ShotstackError(f"Shotstack render ended with status '{status}'.")
            time.sleep(self.poll_seconds)
        raise ShotstackError(f"Timed out waiting for Shotstack render {render_id}.")

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

