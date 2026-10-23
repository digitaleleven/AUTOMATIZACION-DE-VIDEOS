"""Local browser panel and same-origin API for the video pipeline.

This server uses only Python's standard library. It never contacts provider APIs
unless a user explicitly starts a job with paid-API authorization.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import unquote, urlsplit
import uuid

from .config import PipelineConfig
from .media import MediaError, probe_video
from .models import VideoInfo
from .pipeline import _estimate_cost, process_local_video
from .state import ProcessingLedger
from .style import VideoStyle, load_video_style, save_video_style

logger = logging.getLogger(__name__)
MAX_JSON_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
ALLOWED_CODECS = {"h264", "hevc"}
TERMINAL_STATUSES = {"completed", "failed"}


class PanelError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class DashboardStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.ledger = ProcessingLedger(self.database_path)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboard_uploads (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboard_jobs (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress REAL,
                    error TEXT,
                    estimated_cost_usd REAL,
                    output_id TEXT,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS dashboard_jobs_source_created "
                "ON dashboard_jobs(source_id, created_at DESC)"
            )
            # A worker cannot survive a process restart. Report that honestly
            # rather than leaving stale jobs marked as actively running.
            db.execute(
                """UPDATE dashboard_jobs
                   SET status = 'failed', phase = 'interrupted',
                       error = 'El servicio se reinició antes de completar este trabajo.',
                       updated_at = ?
                   WHERE status IN ('queued', 'processing')""",
                (time.time(),),
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def add_upload(
        self,
        file_id: str,
        filename: str,
        original_filename: str,
        source_sha256: str,
        size_bytes: int,
        metadata: dict[str, Any],
    ) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """INSERT INTO dashboard_uploads
                   (id, filename, original_filename, source_sha256, size_bytes, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_id, filename, original_filename, source_sha256, size_bytes,
                    json.dumps(metadata, ensure_ascii=False), time.time(),
                ),
            )

    def get_upload(self, file_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM dashboard_uploads WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def create_job(self, job_id: str, source_id: str, estimate: float | None) -> None:
        now = time.time()
        with self.lock, self._connect() as db:
            db.execute(
                """INSERT INTO dashboard_jobs
                   (id, source_id, status, phase, progress, estimated_cost_usd, created_at, updated_at)
                   VALUES (?, ?, 'queued', 'queued', NULL, ?, ?, ?)""",
                (job_id, source_id, estimate, now, now),
            )

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "phase", "progress", "error", "estimated_cost_usd", "output_id", "result_json"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported dashboard job update.")
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at = ?")
        values.extend((time.time(), job_id))
        with self.lock, self._connect() as db:
            db.execute(
                f"UPDATE dashboard_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM dashboard_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return _public_job(dict(row))

    def latest_job_for_source(self, source_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM dashboard_jobs WHERE source_id = ? ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_uploads(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM dashboard_uploads ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "source_id": job["source_id"],
        "status": job["status"],
        "phase": job["phase"],
        "progress": job["progress"],
        "error": job["error"],
        "estimated_cost_usd": job["estimated_cost_usd"],
        "output_id": job["output_id"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


class VideoPanel:
    def __init__(
        self,
        input_dir: str | Path = "data/incoming",
        output_dir: str | Path = "outputs",
        database_path: str | Path = "data/processing.sqlite3",
        style_path: str | Path = "video_style.json",
    ):
        self.input_dir = Path(input_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.style_path = Path(style_path).resolve()
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = DashboardStore(database_path)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-job")
        self.job_lock = threading.Lock()
        self.active_job_id: str | None = None

    def health(self) -> dict[str, Any]:
        try:
            config = PipelineConfig.from_environment()
            environment = config.shotstack_environment
        except ValueError:
            environment = "configuración inválida"
        return {
            "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
            "shotstack_key_configured": bool(os.getenv("SHOTSTACK_API_KEY")),
            "shotstack_environment": environment,
            "drive_connected": False,
        }

    def style(self) -> dict[str, Any]:
        return asdict(load_video_style(self.style_path))

    def save_style(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La configuración de estilo debe ser un objeto JSON.")
        try:
            return asdict(save_video_style(payload, self.style_path))
        except (ValueError, TypeError, OSError) as exc:
            raise PanelError(str(exc), 400) from exc

    def upload(self, filename: str, content_length: int, stream) -> dict[str, Any]:
        style = load_video_style(self.style_path)
        if content_length <= 0:
            raise PanelError("El archivo está vacío.")
        maximum = style.max_input_size_mb * 1024 * 1024
        if content_length > maximum:
            raise PanelError(f"El vídeo supera el límite configurado de {style.max_input_size_mb} MB.", 413)
        original_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        original_name = "".join(char for char in original_name if char.isprintable()).strip()[:180]
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov"}:
            raise PanelError("Solo se admiten archivos .MP4 y .MOV.")
        file_id = uuid.uuid4().hex
        temp_path = self.input_dir / f".{file_id}.upload{suffix}"
        filename_on_disk = f"{file_id}{suffix}"
        final_path = self.input_dir / filename_on_disk
        digest = hashlib.sha256()
        written = 0
        try:
            with temp_path.open("xb") as output:
                while written < content_length:
                    block = stream.read(min(COPY_CHUNK_BYTES, content_length - written))
                    if not block:
                        raise PanelError("La carga terminó antes de recibir el archivo completo.")
                    written += len(block)
                    if written > maximum:
                        raise PanelError("El vídeo supera el límite configurado.", 413)
                    digest.update(block)
                    output.write(block)
            if written != content_length:
                raise PanelError("El tamaño recibido no coincide con el tamaño anunciado.")

            metadata = probe_video(temp_path)
            if metadata.video_codec not in ALLOWED_CODECS:
                raise PanelError(
                    f"Códec de vídeo no compatible: {metadata.video_codec}. Se admiten H.264 y HEVC/H.265."
                )
            if not metadata.has_audio:
                raise PanelError("El vídeo no contiene audio para transcribir.")
            if metadata.duration > style.max_video_duration_seconds:
                raise PanelError(
                    f"El vídeo dura {metadata.duration:.1f} s y supera el límite de "
                    f"{style.max_video_duration_seconds} s."
                )
            os.replace(temp_path, final_path)
            public_metadata = _metadata_dict(metadata)
            self.store.add_upload(
                file_id,
                filename_on_disk,
                original_name,
                digest.hexdigest(),
                written,
                public_metadata,
            )
        except PanelError:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        except (MediaError, OSError, ValueError) as exc:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise PanelError(f"No se pudo validar el vídeo: {exc}", 422) from exc
        except Exception:
            temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        return self.library_item(file_id)

    def _source_path(self, upload: dict[str, Any]) -> Path:
        path = (self.input_dir / upload["filename"]).resolve()
        if path.parent != self.input_dir or not path.is_file():
            raise PanelError("El archivo local ya no está disponible.", 404)
        return path

    def library_item(self, file_id: str) -> dict[str, Any]:
        upload = self.store.get_upload(file_id)
        if not upload:
            raise PanelError("No se encontró el vídeo solicitado.", 404)
        return self._public_library_item(upload)

    def library(self) -> list[dict[str, Any]]:
        return [self._public_library_item(item) for item in self.store.list_uploads()]

    def _public_library_item(self, upload: dict[str, Any]) -> dict[str, Any]:
        metadata = json.loads(upload["metadata_json"])
        style = load_video_style(self.style_path)
        estimate = _estimate_cost(float(metadata["duration"]), style)
        job = self.store.latest_job_for_source(upload["id"])
        if job:
            status = job["status"]
            output_id = job["output_id"]
            error = job["error"]
        else:
            processing = self.store.ledger.get(upload["source_sha256"])
            if processing.get("status") == "completed":
                status = "completed"
                output_path = processing.get("output_path")
                output_id = Path(output_path).name if output_path else None
            elif processing.get("status") in {"processing", "failed"}:
                status = processing["status"]
                output_id = None
            else:
                status = "ready"
                output_id = None
            error = processing.get("error")
        if output_id:
            rendered_path = (self.output_dir / output_id).resolve()
            if rendered_path.parent != self.output_dir or not rendered_path.is_file():
                output_id = None
                error = "El resultado registrado ya no está disponible en la carpeta de salida."
        return {
            "id": upload["id"],
            "filename": upload["filename"],
            "original_filename": upload["original_filename"],
            "format": Path(upload["filename"]).suffix.lstrip(".").upper(),
            "size_bytes": upload["size_bytes"],
            "duration_seconds": metadata["duration"],
            "width": metadata["width"],
            "height": metadata["height"],
            "video_codec": metadata["video_codec"],
            "audio_codec": metadata["audio_codec"],
            "rotation": metadata["rotation"],
            "fps": metadata["fps"],
            "status": status,
            "output_id": output_id,
            "error": error,
            "cost_estimate_usd": estimate,
        }

    def start_job(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud del trabajo debe ser un objeto JSON.")
        source_id = str(payload.get("source_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", source_id):
            raise PanelError("Identificador de vídeo no válido.")
        upload = self.store.get_upload(source_id)
        if not upload:
            raise PanelError("No se encontró el vídeo seleccionado.", 404)
        style = load_video_style(self.style_path)
        item = self._public_library_item(upload)
        if item["status"] in {"completed", "processing", "queued"}:
            raise PanelError(
                "Este archivo ya tiene un resultado o un trabajo activo; no se iniciará otro render.",
                409,
            )
        if item["status"] == "failed":
            raise PanelError(
                "El último intento falló. El panel no reintenta automáticamente para evitar cargos duplicados.",
                409,
            )
        estimate = item["cost_estimate_usd"]
        if payload.get("authorize_paid_apis") is not True:
            raise PanelError("Falta la autorización explícita para OpenAI y Shotstack Stage.", 403)
        if estimate is None and payload.get("unknown_cost_acknowledged") is not True:
            raise PanelError("El coste estimado es desconocido; confirma ese riesgo para continuar.", 403)
        if style.max_estimated_cost_usd is not None:
            if estimate is None:
                raise PanelError("Hay un límite USD configurado, pero faltan tarifas para calcularlo.", 400)
            if estimate > style.max_estimated_cost_usd:
                raise PanelError(
                    f"La estimación ${estimate:.4f} supera el límite configurado "
                    f"${style.max_estimated_cost_usd:.4f}.",
                    409,
                )
        health = self.health()
        if not health["openai_key_configured"] or not health["shotstack_key_configured"]:
            raise PanelError("Falta una clave de API en Secrets; no se inició ningún trabajo.", 409)
        if health["shotstack_environment"] != "stage":
            raise PanelError("La configuración no permite iniciar trabajos fuera de Shotstack Stage.", 409)

        with self.job_lock:
            if self.active_job_id:
                active = self.store.get_job(self.active_job_id)
                if active and active["status"] not in TERMINAL_STATUSES:
                    raise PanelError("Ya hay un vídeo en procesamiento.", 409)
            job_id = uuid.uuid4().hex
            self.store.create_job(job_id, source_id, estimate)
            self.active_job_id = job_id
        self.executor.submit(self._run_job, job_id, upload, estimate, style)
        return self.store.get_job(job_id) or {}

    def _run_job(
        self,
        job_id: str,
        upload: dict[str, Any],
        estimate: float | None,
        style: VideoStyle,
    ) -> None:
        try:
            source_path = self._source_path(upload)
            def update_phase(phase: str) -> None:
                self.store.update_job(job_id, status="processing", phase=phase, progress=None)

            result = process_local_video(
                source_path,
                self.output_dir,
                paid_api_authorized=True,
                config=PipelineConfig.from_environment(),
                style=style,
                state_db_path=self.store.database_path,
                progress_callback=update_phase,
            )
            self.store.update_job(
                job_id,
                status="completed",
                phase="completed",
                progress=100,
                estimated_cost_usd=result["estimated_cost_usd"],
                output_id=Path(result["output"]).name,
                result_json=json.dumps(result, ensure_ascii=False),
            )
        except Exception as exc:
            logger.exception("Video job %s failed", job_id)
            self.store.update_job(
                job_id,
                status="failed",
                phase="failed",
                progress=None,
                error=str(exc)[:500],
                estimated_cost_usd=estimate,
            )
        finally:
            with self.job_lock:
                if self.active_job_id == job_id:
                    self.active_job_id = None

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise PanelError("Identificador de trabajo no válido.")
        result = self.store.get_job(job_id)
        if not result:
            raise PanelError("No se encontró el trabajo solicitado.", 404)
        return result

    def source_path(self, file_id: str) -> tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise PanelError("Identificador de vídeo no válido.")
        upload = self.store.get_upload(file_id)
        if not upload:
            raise PanelError("No se encontró el vídeo solicitado.", 404)
        return self._source_path(upload), upload["original_filename"]

    def output_path(self, file_id: str) -> tuple[Path, str]:
        if not file_id or Path(file_id).name != file_id or "/" in file_id or "\\" in file_id:
            raise PanelError("Identificador de resultado no válido.")
        path = (self.output_dir / file_id).resolve()
        if path.parent != self.output_dir or path.suffix.lower() != ".mp4" or not path.is_file():
            raise PanelError("No se encontró el vídeo editado.", 404)
        return path, file_id


def _metadata_dict(video: VideoInfo) -> dict[str, Any]:
    return {
        "duration": video.duration,
        "width": video.width,
        "height": video.height,
        "video_codec": video.video_codec,
        "audio_codec": video.audio_codec,
        "rotation": video.rotation,
        "fps": video.fps,
        "pixel_format": video.pixel_format,
        "color_primaries": video.color_primaries,
        "color_transfer": video.color_transfer,
        "color_space": video.color_space,
        "container": video.container,
    }


class VideoPanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, panel: VideoPanel):
        super().__init__(server_address, handler_class)
        self.panel = panel


class VideoPanelHandler(BaseHTTPRequestHandler):
    server: VideoPanelServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format_string % args)

    def _headers(self, content_type: str, content_length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _json(self, payload: Any, status: int = 200, head: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return
        host = self.headers.get("Host", "")
        parsed = urlsplit(origin)
        if not host or parsed.netloc.lower() != host.lower():
            raise PanelError("Solicitud rechazada: origen no permitido.", 403)

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise PanelError("Falta Content-Length.", 411)
        length = int(raw_length)
        if length <= 0 or length > MAX_JSON_BYTES:
            raise PanelError("Tamaño JSON no válido.", 413)
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PanelError("JSON no válido.") from exc

    def _send_static(self, relative: str, head: bool = False) -> None:
        allowed = {"app.js", "styles.css"}
        if relative not in allowed and relative != "index.html":
            raise PanelError("No se encontró el recurso solicitado.", 404)
        path = Path(__file__).resolve().parent.parent / "web" / relative
        if not path.is_file():
            raise PanelError("No se encontró el recurso del panel.", 404)
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(200)
        self._headers(f"{content_type}; charset=utf-8", len(body))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_media(self, path: Path, filename: str, head: bool = False) -> None:
        size = path.stat().st_size
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, max(0, size - 1)
        status = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or size <= 0:
                self.send_error(416, "Invalid byte range")
                return
            left, right = match.groups()
            if left:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            elif right:
                start = max(0, size - int(right))
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self._headers(content_type, 0)
                self.end_headers()
                return
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self._headers(content_type, length)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", formatdate(path.stat().st_mtime, usegmt=True))
        safe_name = filename.replace('"', "")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head or length == 0:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                block = source.read(min(COPY_CHUNK_BYTES, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _dispatch(self, head: bool = False) -> None:
        try:
            path = urlsplit(self.path).path
            if self.command in {"POST", "PUT"}:
                self._check_origin()
            if self.command in {"GET", "HEAD"}:
                if path == "/":
                    self._send_static("index.html", head)
                elif path in {"/app.js", "/styles.css"}:
                    self._send_static(path.lstrip("/"), head)
                elif path == "/api/health":
                    self._json(self.server.panel.health(), head=head)
                elif path == "/api/library":
                    self._json({"items": self.server.panel.library()}, head=head)
                elif path == "/api/style":
                    self._json(self.server.panel.style(), head=head)
                elif path.startswith("/api/jobs/"):
                    job_id = path.rsplit("/", 1)[-1]
                    self._json(self.server.panel.get_job(job_id), head=head)
                elif path.startswith("/media/incoming/"):
                    file_id = path.rsplit("/", 1)[-1]
                    media, name = self.server.panel.source_path(file_id)
                    self._send_media(media, name, head)
                elif path.startswith("/media/outputs/"):
                    output_id = unquote(path.rsplit("/", 1)[-1])
                    media, name = self.server.panel.output_path(output_id)
                    self._send_media(media, name, head)
                else:
                    raise PanelError("Ruta no encontrada.", 404)
            elif self.command == "POST" and path == "/api/upload":
                name = unquote(self.headers.get("X-Upload-Filename", ""))
                if not name:
                    raise PanelError("Falta el nombre del archivo.")
                length = int(self.headers.get("Content-Length", "0"))
                item = self.server.panel.upload(name, length, self.rfile)
                self._json({"item": item}, 201)
            elif self.command == "POST" and path == "/api/jobs":
                self._json(self.server.panel.start_job(self._read_json()), 202)
            elif self.command == "PUT" and path == "/api/style":
                self._json(self.server.panel.save_style(self._read_json()))
            else:
                raise PanelError("Ruta o método no permitido.", 404)
        except PanelError as exc:
            if self.command == "POST" and urlsplit(self.path).path == "/api/upload":
                self.close_connection = True
            if self.command in {"GET", "HEAD"} and not urlsplit(self.path).path.startswith("/api/"):
                self._json({"detail": str(exc)}, exc.status_code, head)
            else:
                self._json({"detail": str(exc)}, exc.status_code, head)
        except (ValueError, BrokenPipeError, ConnectionResetError) as exc:
            if isinstance(exc, ValueError):
                self._json({"detail": "Solicitud no válida."}, 400, head)
            else:
                logger.info("Client disconnected during response: %s", type(exc).__name__)
        except Exception:
            logger.exception("Unhandled local panel request error")
            self._json({"detail": "Error interno del servicio local."}, 500, head)

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()


def serve(
    host: str = "0.0.0.0",
    port: int = 5000,
    input_dir: str | Path = "data/incoming",
    output_dir: str | Path = "outputs",
    database_path: str | Path = "data/processing.sqlite3",
    style_path: str | Path = "video_style.json",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    panel = VideoPanel(input_dir, output_dir, database_path, style_path)
    server = VideoPanelServer((host, port), VideoPanelHandler, panel)
    logger.info("Local video panel listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping local video panel")
    finally:
        server.server_close()
        panel.executor.shutdown(wait=False, cancel_futures=True)
