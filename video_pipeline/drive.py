"""Resumable Google Drive upload for creating new processed video files."""

import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

import requests


DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MINIMUM_CHUNK_SIZE = 256 * 1024


class DriveUploadError(RuntimeError):
    """Raised when a new Drive file cannot be created safely."""


def upload_new_file(
    video_path: str | Path,
    *,
    folder_id: str,
    access_token: str | None,
    session: requests.Session | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    app_properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Upload a render as a new Drive file; never updates or deletes a source."""
    path = Path(video_path)
    if not access_token:
        raise DriveUploadError("GOOGLE_OAUTH_TOKEN is required to upload to Drive.")
    if not folder_id:
        raise DriveUploadError("A destination Drive folder ID is required.")
    if not path.is_file() or path.stat().st_size == 0:
        raise DriveUploadError("The rendered MP4 is missing or empty.")
    if chunk_size < MINIMUM_CHUNK_SIZE or chunk_size % MINIMUM_CHUNK_SIZE:
        raise ValueError("Drive chunk size must be a positive multiple of 256 KiB.")

    total_size = path.stat().st_size
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(total_size),
    }
    metadata = {
        "name": path.name,
        "mimeType": "video/mp4",
        "parents": [folder_id],
    }
    if app_properties is not None:
        if (
            not isinstance(app_properties, dict)
            or len(app_properties) > 10
            or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,124}", key)
                or not isinstance(value, str)
                or len(value) > 124
                for key, value in app_properties.items()
            )
        ):
            raise ValueError("Drive appProperties must contain short, valid string keys and values.")
        metadata["appProperties"] = app_properties
    client = session or requests.Session()
    try:
        response = client.post(
            DRIVE_UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "fields": "id,name,mimeType,parents",
            },
            headers={**headers, "Content-Type": "application/json"},
            json=metadata,
            timeout=(10, 60),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise DriveUploadError("Could not start the Google Drive upload.") from None

    if response.status_code not in {200, 201}:
        raise DriveUploadError(
            f"Google Drive rejected upload initialization (HTTP {response.status_code})."
        )
    upload_url = response.headers.get("Location")
    parsed_url = urlsplit(upload_url or "")
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or not (
            parsed_url.hostname == "googleapis.com"
            or parsed_url.hostname.endswith(".googleapis.com")
        )
    ):
        raise DriveUploadError("Google Drive returned an invalid resumable upload URL.")

    offset = 0
    with path.open("rb") as video:
        while offset < total_size:
            video.seek(offset)
            chunk = video.read(min(chunk_size, total_size - offset))
            if not chunk:
                raise DriveUploadError("The rendered MP4 could not be read completely.")
            chunk_end = offset + len(chunk) - 1
            chunk_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{chunk_end}/{total_size}",
            }
            try:
                upload_response = client.put(
                    upload_url,
                    data=chunk,
                    headers=chunk_headers,
                    timeout=(20, 600),
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise DriveUploadError(
                    "The resumable Google Drive upload was interrupted."
                ) from None

            if upload_response.status_code in {200, 201}:
                if chunk_end != total_size - 1:
                    raise DriveUploadError(
                        "Google Drive completed an upload before all bytes were sent."
                    )
                try:
                    result = upload_response.json()
                except (ValueError, json.JSONDecodeError):
                    raise DriveUploadError(
                        "Google Drive returned invalid metadata for the created file."
                    ) from None
                if not isinstance(result, dict) or not result.get("id"):
                    raise DriveUploadError(
                        "Google Drive did not confirm creation of the new video file."
                    )
                return result

            if upload_response.status_code != 308:
                raise DriveUploadError(
                    f"Google Drive rejected an upload chunk "
                    f"(HTTP {upload_response.status_code})."
                )

            acknowledged = upload_response.headers.get("Range")
            match = re.fullmatch(r"bytes=0-(\d+)", acknowledged or "")
            if not match:
                raise DriveUploadError(
                    "Google Drive did not confirm the uploaded byte range."
                )
            next_offset = int(match.group(1)) + 1
            if next_offset <= offset or next_offset > chunk_end + 1:
                raise DriveUploadError("Google Drive returned an invalid upload range.")
            offset = next_offset

    raise DriveUploadError("Google Drive did not confirm completion of the new file.")

