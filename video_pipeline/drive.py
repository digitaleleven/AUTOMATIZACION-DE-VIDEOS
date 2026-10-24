"""Google Drive operations limited to files explicitly selected with Drive Picker."""

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

import requests


DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MINIMUM_CHUNK_SIZE = 256 * 1024


class DriveAccessError(RuntimeError):
    """Raised when an explicitly selected Drive item cannot be accessed safely."""


class DriveUploadError(RuntimeError):
    """Raised when a new Drive file cannot be created safely."""


def picker_configuration() -> dict[str, str | None]:
    """Return browser-only Picker settings; OAuth access tokens are never stored."""
    return {
        "client_id": os.getenv("GOOGLE_DRIVE_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "api_key": os.getenv("GOOGLE_DRIVE_API_KEY"),
        "app_id": os.getenv("GOOGLE_DRIVE_APP_ID"),
        "scope": "https://www.googleapis.com/auth/drive.file",
    }


def load_folder_selections(path: str | Path = "data/drive_folders.json") -> dict[str, dict[str, str]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        role: {"id": item["id"], "name": item["name"]}
        for role, item in value.items()
        if role in {"entrada", "procesados", "logos"}
        and isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("name"), str)
    }


def save_folder_selection(
    role: str,
    folder: dict[str, str],
    path: str | Path = "data/drive_folders.json",
) -> dict[str, dict[str, str]]:
    if role not in {"entrada", "procesados", "logos"}:
        raise DriveAccessError("Tipo de carpeta de Drive no válido.")
    folder_id = folder.get("id", "").strip()
    name = folder.get("name", "").strip()
    if not folder_id or not name:
        raise DriveAccessError("Google Picker no devolvió una carpeta válida.")
    target = Path(path)
    selections = load_folder_selections(target)
    selections[role] = {"id": folder_id, "name": name}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(selections, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return selections


def load_file_selections(path: str | Path = "data/drive_files.json") -> dict[str, list[dict[str, Any]]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        role: [item for item in items if isinstance(item, dict) and item.get("id") and item.get("name")]
        for role, items in value.items()
        if role in {"entrada", "logos", "instrucciones"} and isinstance(items, list)
    }


def save_file_selections(
    role: str,
    items: list[dict[str, Any]],
    path: str | Path = "data/drive_files.json",
) -> dict[str, list[dict[str, Any]]]:
    if role not in {"entrada", "logos", "instrucciones"}:
        raise DriveAccessError("Solo se pueden autorizar MP4, instrucciones o logos.")
    target = Path(path)
    selections = load_file_selections(target)
    unique: dict[str, dict[str, Any]] = {}
    for item in selections.get(role, []):
        file_id = item.get("id")
        if isinstance(file_id, str) and file_id:
            unique[file_id] = item
    for item in items:
        file_id = item.get("id")
        if not isinstance(file_id, str) or not file_id or not isinstance(item.get("name"), str):
            continue
        unique[file_id] = {
            key: item.get(key)
            for key in ("id", "name", "mimeType", "size", "modifiedTime", "parents")
        }
    selections[role] = list(unique.values())
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(selections, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return selections


def _drive_request(
    method: str,
    url: str,
    *,
    access_token: str | None,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    if not access_token:
        raise DriveAccessError("Autoriza Google Drive en esta sesión para continuar.")
    client = session or requests.Session()
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = client.request(
            method,
            url,
            headers=headers,
            timeout=(10, 60),
            allow_redirects=False,
            **kwargs,
        )
    except requests.RequestException:
        raise DriveAccessError("No se pudo contactar con Google Drive.") from None
    if response.status_code in {401, 403}:
        raise DriveAccessError(
            "Google Drive no autorizó el acceso. Vuelve a autorizar con drive.file y selecciona la carpeta o el archivo."
        )
    if not response.ok:
        raise DriveAccessError(f"Google Drive respondió con HTTP {response.status_code}.")
    return response


def get_folder(
    folder_id: str,
    *,
    access_token: str | None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if not folder_id or len(folder_id) > 256:
        raise DriveAccessError("El ID de carpeta no es válido.")
    response = _drive_request(
        "GET",
        f"{DRIVE_API_URL}/{quote(folder_id, safe='')}",
        access_token=access_token,
        session=session,
        params={"fields": "id,name,mimeType,trashed,capabilities(canListChildren,canAddChildren)"},
    )
    try:
        folder = response.json()
    except (ValueError, json.JSONDecodeError):
        raise DriveAccessError("Google Drive devolvió datos de carpeta no válidos.") from None
    if (
        not isinstance(folder, dict)
        or folder.get("id") != folder_id
        or folder.get("mimeType") != "application/vnd.google-apps.folder"
        or folder.get("trashed")
    ):
        raise DriveAccessError("La selección no es una carpeta de Drive disponible.")
    return folder


def get_drive_file(
    file_id: str,
    *,
    access_token: str | None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if not file_id or len(file_id) > 256:
        raise DriveAccessError("El ID de archivo no es válido.")
    response = _drive_request(
        "GET",
        f"{DRIVE_API_URL}/{quote(file_id, safe='')}",
        access_token=access_token,
        session=session,
        params={"fields": "id,name,mimeType,size,modifiedTime,parents,trashed"},
    )
    try:
        item = response.json()
    except (ValueError, json.JSONDecodeError):
        raise DriveAccessError("Google Drive devolvió metadatos no válidos.") from None
    if not isinstance(item, dict) or item.get("id") != file_id or item.get("trashed"):
        raise DriveAccessError("El archivo elegido no está disponible en Google Drive.")
    return item


def download_drive_file(
    file_id: str,
    destination: str | Path,
    *,
    access_token: str | None,
    session: requests.Session | None = None,
) -> Path:
    """Download to a fresh local path; exclusive creation prevents overwrites."""
    if not file_id or len(file_id) > 256:
        raise DriveAccessError("El ID de archivo no es válido.")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DriveAccessError("El archivo de destino ya existe; no se sobrescribió.")
    response = _drive_request(
        "GET",
        f"{DRIVE_API_URL}/{quote(file_id, safe='')}",
        access_token=access_token,
        session=session,
        params={"alt": "media"},
        stream=True,
    )
    try:
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            for block in response.iter_content(chunk_size=1024 * 1024):
                if block:
                    output.write(block)
    except FileExistsError:
        raise DriveAccessError("El archivo de destino ya existe; no se sobrescribió.") from None
    except OSError:
        path.unlink(missing_ok=True)
        raise DriveAccessError("No se pudo guardar el archivo descargado.") from None
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise DriveAccessError("Google Drive devolvió un archivo vacío.")
    return path


def create_test_file(
    folder_id: str,
    *,
    access_token: str | None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Create one new empty test file; never updates, moves, or deletes anything."""
    if not folder_id:
        raise DriveAccessError("Selecciona primero la carpeta Procesados.")
    response = _drive_request(
        "POST",
        DRIVE_API_URL,
        access_token=access_token,
        session=session,
        params={"fields": "id,name,mimeType,parents"},
        headers={"Content-Type": "application/json"},
        json={
            "name": f"prueba_conexion_drive_{uuid.uuid4().hex[:12]}.txt",
            "mimeType": "text/plain",
            "parents": [folder_id],
        },
    )
    try:
        result = response.json()
    except (ValueError, json.JSONDecodeError):
        raise DriveAccessError("Google Drive no confirmó el archivo de prueba.") from None
    if not isinstance(result, dict) or not result.get("id") or folder_id not in result.get("parents", []):
        raise DriveAccessError("Google Drive no confirmó la creación en Procesados.")
    return result


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
        raise DriveUploadError("A Drive OAuth access token is required to upload to Drive.")
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

