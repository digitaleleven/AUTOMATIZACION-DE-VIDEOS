"""Local browser panel and same-origin API for the video pipeline.

This server uses only Python's standard library. It never contacts provider APIs
unless a user explicitly starts a job with paid-API authorization.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
import html
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

from .auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    AuthError,
    OwnerAuth,
)
from .config import PipelineConfig
from .drive import DriveUploadError
from .drive_scheduler import DriveScanScheduler
from .google_drive import (
    GOOGLE_DOC_MIME,
    VIDEO_EXTENSIONS,
    DriveOAuthConfig,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveOAuth,
    GoogleDriveStore,
    configured_drive_folders,
    parse_style_instructions,
)
from .media import MediaError, prepare_video_for_modal, probe_video, temporary_work_directory
from .edit_plan import EditPlanError, edited_duration, normalize_edit_plan
from .models import VideoInfo
from .drive import (
    DriveAccessError,
    create_test_file,
    get_drive_file,
    get_folder,
    load_file_selections,
    load_folder_selections,
    picker_configuration,
    save_file_selections,
    save_folder_selection,
)
from .pipeline import (
    RENDER001_THIRD_RETRY_SOURCE_SHA256,
    _cost_estimate_note,
    _estimate_cost,
    process_local_video,
    sync_drive_media,
)
from .modal_pipeline import process_local_video_modal
from .modal_renderer import modal_configuration_status
from .r2_storage import configuration_status as r2_configuration_status, upload_mp4 as upload_mp4_to_r2
from .state import ProcessingLedger
from .shotstack import ShotstackClient, ShotstackError
from .state import ProcessingLedger
from .style import VideoStyle, load_video_style, save_video_style

logger = logging.getLogger(__name__)
MAX_JSON_BYTES = 64 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
ALLOWED_CODECS = {"h264", "hevc"}
OUTPUT_PROFILES = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}
VIDEO_NICHE_LABELS = {
    "beauty_skincare": "Beauty & Skincare",
    "fashion": "Fashion & Style",
    "lifestyle_travel": "Lifestyle & Travel",
    "fitness_sport": "Fitness & Sport",
    "ugc_ecommerce": "UGC & Productos",
    "generic_viral": "Genérico / Viral",
}
VIDEO_EDIT_STYLE_LABELS = {
    "natural_premium": "Natural Premium",
    "dynamic": "Dinámico",
}
DEFAULT_VIDEO_NICHE = "generic_viral"
MAX_STYLE_NOTES_CHARS = 500
CAPTION_BRIEF_DIRECTIVES = {
    "fontfamily": "font_family",
    "highlightcolor": "highlight_color",
    "textcolor": "font_color",
}
TERMINAL_STATUSES = {"completed", "failed"}


def _parse_caption_style_overrides(style_notes: str) -> dict[str, str]:
    """Read only explicit, allowlisted caption directives from a per-video brief."""
    overrides: dict[str, str] = {}
    for line in style_notes.splitlines():
        match = re.fullmatch(
            r"\s*(fontFamily|highlightColor|textColor)\s*[:=]\s*(.*?)\s*",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        key = CAPTION_BRIEF_DIRECTIVES[match.group(1).lower()]
        value = match.group(2).strip()
        if key == "font_family":
            if not value or len(value) > 80 or not re.fullmatch(r"[A-Za-z0-9 ,'-]+", value):
                raise PanelError(
                    "fontFamily en las notas debe ser un nombre de fuente sencillo (máximo 80 caracteres)."
                )
            overrides[key] = value
            continue
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise PanelError(f"{match.group(1)} en las notas debe ser un color hexadecimal #RRGGBB.")
        overrides[key] = value.upper()
    return overrides


def _validate_panel_customization(raw: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Validate the controls that the current Modal renderer really applies."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PanelError("La personalización debe ser un objeto.")
    allowed = {
        "caption_color", "caption_timing", "caption_size", "caption_position",
        "auto_zoom", "transitions", "audio_volume", "noise_reduction",
        "music_enabled", "music_ducking", "titles", "accents", "image_filter", "notes",
    }
    if set(raw) - allowed:
        raise PanelError("La personalización contiene opciones desconocidas.")

    color = raw.get("caption_color", "#FFC0CB")
    if color not in {"#FFC0CB", "#00AFA8", "#FFE600"}:
        raise PanelError("El color de subtítulos debe ser rosa pastel, turquesa o amarillo.")
    timing = raw.get("caption_timing", "groups")
    if timing not in {"word-by-word", "groups"}:
        raise PanelError("El ritmo de subtítulos no es válido.")
    size_map = {"small": 42, "medium": 54, "large": 68}
    size = raw.get("caption_size", "medium")
    if size not in size_map:
        raise PanelError("El tamaño de subtítulos no es válido.")
    position_map = {"lower": "bottom", "center": "middle", "upper": "top"}
    position = raw.get("caption_position", "lower")
    if position not in position_map:
        raise PanelError("La posición de subtítulos no es válida.")
    zoom = raw.get("auto_zoom", "smooth")
    if zoom not in {"off", "smooth"}:
        raise PanelError("Este motor permite autozoom suave o desactivado; los modos agresivos no están disponibles.")
    if raw.get("transitions", "clean") != "clean":
        raise PanelError("Este motor solo admite cortes limpios; las transiciones adicionales no están disponibles.")
    if raw.get("image_filter", "natural") not in {"none", "natural"}:
        raise PanelError("Los filtros de color adicionales requieren una muestra y aprobación visual.")
    if any(raw.get(key, False) is not False for key in ("music_enabled", "music_ducking", "titles", "accents")):
        raise PanelError("No hay biblioteca de música o capas de diseño habilitada para este render.")
    volume = raw.get("audio_volume", 100)
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 100:
        raise PanelError("El volumen original debe estar entre 0 y 100.")
    noise_reduction = raw.get("noise_reduction", False)
    if not isinstance(noise_reduction, bool):
        raise PanelError("La reducción de ruido debe ser booleana.")
    notes = raw.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 1000:
        raise PanelError("Las instrucciones especiales deben tener como máximo 1.000 caracteres.")

    style_overrides = {
        "highlight_color": color,
        "font_size": size_map[size],
        "caption_position": position_map[position],
        "max_words_per_caption": 1 if timing == "word-by-word" else 2,
    }
    render_settings = {
        "auto_zoom": zoom,
        "audio": {
            "original_volume": float(volume) / 100,
            "noise_reduction": noise_reduction,
        },
        "transitions": "clean",
        "image_filter": raw.get("image_filter", "natural"),
    }
    return style_overrides, render_settings, notes.strip()
GOOGLE_OAUTH_STATE_COOKIE = "matrix_motion_google_oauth_state"
RENDER001_THIRD_RETRY_TARGET = {
    "job_id": "8ec0c6cb1ff84b6f94d8dd0c64a56747",
    "previous_job_id": "621e5ca005984410bd922aea07144588",
    "source_id": "608c74727e18449da2899f75d426f8c9",
    "source_sha256": RENDER001_THIRD_RETRY_SOURCE_SHA256,
    "filename": "0415.mov",
}
RENDER001_THIRD_RETRY_PLAN = {
    "cuts": [],
    "zooms": [{"start_seconds": 10.0, "end_seconds": 13.0, "effect": "zoomIn"}],
    "stickers": [{
        "sticker": "sparkle",
        "start_seconds": 4.0,
        "duration_seconds": 1.5,
        "position": "topRight",
        "effect": "zoomIn",
        "color": "#FFD84D",
    }],
}


def _matches_render001_third_retry_configuration(
    style: VideoStyle,
    subtitle_mode: str,
    creative_plan: dict[str, Any],
) -> bool:
    return (
        subtitle_mode == "none"
        and style.output_width == 1080
        and style.output_height == 1920
        and style.output_fps == 50
        and style.output_quality == "high"
        and not style.logo_path
        and creative_plan == RENDER001_THIRD_RETRY_PLAN
    )


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
                    edit_options_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            job_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(dashboard_jobs)")
            }
            if "edit_options_json" not in job_columns:
                db.execute(
                    "ALTER TABLE dashboard_jobs ADD COLUMN edit_options_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "attempt_number" not in job_columns:
                db.execute(
                    "ALTER TABLE dashboard_jobs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1"
                )
            if "retry_of_job_id" not in job_columns:
                db.execute("ALTER TABLE dashboard_jobs ADD COLUMN retry_of_job_id TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS dashboard_jobs_source_created "
                "ON dashboard_jobs(source_id, created_at DESC)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS dashboard_jobs_retry_once "
                "ON dashboard_jobs(retry_of_job_id) WHERE retry_of_job_id IS NOT NULL"
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
            db.execute(
                """UPDATE processing_jobs
                   SET status = 'failed',
                       error = 'El servicio se reinició; el estado remoto debe comprobarse antes de reintentar.',
                       updated_at = ?
                   WHERE status = 'processing'
                     AND source_sha256 IN (
                       SELECT uploads.source_sha256
                       FROM dashboard_uploads AS uploads
                       JOIN dashboard_jobs AS jobs ON jobs.source_id = uploads.id
                       WHERE jobs.phase = 'interrupted' AND jobs.status = 'failed'
                     )""",
                (ProcessingLedger._now(),),
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

    def delete_upload_if_unused(self, file_id: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                """DELETE FROM dashboard_uploads
                   WHERE id = ? AND NOT EXISTS (
                     SELECT 1 FROM dashboard_jobs WHERE source_id = ?
                   )""",
                (file_id, file_id),
            )
        return cursor.rowcount == 1

    def get_upload_by_hash(self, source_hash: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM dashboard_uploads WHERE source_sha256 = ? LIMIT 1",
                (source_hash,),
            ).fetchone()
        return dict(row) if row else None

    def create_job(
        self,
        job_id: str,
        source_id: str,
        estimate: float | None,
        edit_options: dict[str, Any] | None = None,
        *,
        attempt_number: int = 1,
        retry_of_job_id: str | None = None,
    ) -> None:
        now = time.time()
        with self.lock, self._connect() as db:
            db.execute(
                """INSERT INTO dashboard_jobs
                   (id, source_id, status, phase, progress, estimated_cost_usd,
                    edit_options_json, attempt_number, retry_of_job_id, created_at, updated_at)
                   VALUES (?, ?, 'queued', 'queued', NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    source_id,
                    estimate,
                    json.dumps(edit_options or {}, ensure_ascii=False),
                    attempt_number,
                    retry_of_job_id,
                    now,
                    now,
                ),
            )

    def create_retry_job(
        self,
        previous_job_id: str,
        job_id: str,
        estimate: float | None,
        edit_options: dict[str, Any],
        *,
        attempt_number: int = 2,
    ) -> dict[str, Any]:
        """Atomically append exactly the next attempt, preserving all prior rows."""
        if attempt_number not in {2, 3}:
            raise PanelError("Número de intento no permitido.", 409)
        expected_history = list(range(1, attempt_number))
        now = time.time()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM dashboard_jobs WHERE id = ?",
                (previous_job_id,),
            ).fetchone()
            if not previous:
                raise PanelError("No se encontró el intento fallido.", 404)
            previous = dict(previous)
            latest = db.execute(
                """SELECT id FROM dashboard_jobs WHERE source_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (previous["source_id"],),
            ).fetchone()
            history = db.execute(
                """SELECT attempt_number FROM dashboard_jobs WHERE source_id = ?
                   ORDER BY attempt_number ASC, created_at ASC, rowid ASC""",
                (previous["source_id"],),
            ).fetchall()
            history_numbers = [int(row["attempt_number"] or 1) for row in history]
            if previous["status"] != "failed":
                raise PanelError("Solo se puede reintentar un intento que terminó con error.", 409)
            if int(previous.get("attempt_number") or 1) != attempt_number - 1:
                raise PanelError("El intento anterior no coincide con la secuencia autorizada.", 409)
            if history_numbers != expected_history:
                raise PanelError("El historial cambió o ya se reservó este intento.", 409)
            if not latest or latest["id"] != previous_job_id:
                raise PanelError("El intento seleccionado ya no es el más reciente.", 409)
            try:
                db.execute(
                    """INSERT INTO dashboard_jobs
                       (id, source_id, status, phase, progress, estimated_cost_usd,
                    edit_options_json, attempt_number, retry_of_job_id, created_at, updated_at)
                   VALUES (?, ?, 'queued', 'queued', NULL, ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id,
                        previous["source_id"],
                        estimate,
                        json.dumps(edit_options, ensure_ascii=False),
                        attempt_number,
                        previous_job_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise PanelError("Este intento ya tiene un reintento reservado.", 409) from None
        return self.get_job(job_id) or {}

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

    def job_history_for_source(self, source_id: str) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute(
                """SELECT id, status, phase, error, attempt_number, retry_of_job_id,
                          estimated_cost_usd, created_at, updated_at
                   FROM dashboard_jobs WHERE source_id = ?
                   ORDER BY attempt_number ASC, created_at ASC, rowid ASC""",
                (source_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_uploads(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM dashboard_uploads ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    try:
        edit_options = json.loads(job.get("edit_options_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        edit_options = {}
    try:
        result = json.loads(job.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        result = {}
    output_metadata = {
        "dimensions": result.get("output_dimensions"),
        "fps": result.get("output_fps"),
        "video_codec": result.get("output_video_codec"),
        "audio_codec": result.get("output_audio_codec"),
        "quality_preset": result.get("output_quality_preset"),
        "video_bit_rate_bps": result.get("output_video_bit_rate_bps"),
        "format_bit_rate_bps": result.get("output_format_bit_rate_bps"),
        "duration_seconds": result.get("output_duration_seconds"),
    } if isinstance(result, dict) else {}
    return {
        "id": job["id"],
        "source_id": job["source_id"],
        "status": job["status"],
        "phase": job["phase"],
        "progress": job["progress"],
        "error": job["error"],
        "estimated_cost_usd": job["estimated_cost_usd"],
        "edit_options": edit_options if isinstance(edit_options, dict) else {},
        "output_metadata": output_metadata,
        "output_id": job["output_id"],
        "attempt_number": int(job.get("attempt_number") or 1),
        "retry_of_job_id": job.get("retry_of_job_id"),
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
        self.thumbnail_dir = self.input_dir.parent / "thumbnails"
        self.style_path = Path(style_path).resolve()
        project_root = Path(__file__).resolve().parents[1]
        self.q03c_sample_path = (
            project_root
            / "generated-artifacts/drive-inspection/q03c/IMG_1254-Q03C-dynamic-viral-sample.mp4"
        )
        self.q03c_comparison_path = (
            project_root / "generated-artifacts/drive-inspection/q03c/original-vs-q03c-frame.png"
        )
        q03d_dir = project_root / "generated-artifacts/drive-inspection/q03d"
        self.q03d_summary_path = q03d_dir / "q03d-summary-25s.png"
        self.q03d_candidates_8s_path = q03d_dir / "candidate-grid-8s.png"
        self.q03d_candidates_25s_path = q03d_dir / "candidate-grid-25s.png"
        self.q03d_candidates_49s_path = q03d_dir / "candidate-grid-49s.png"
        q03e_dir = project_root / "generated-artifacts/drive-inspection/q03e"
        self.q03e_sample_path = q03e_dir / "IMG_1254-Q03E-hable0-10s.mp4"
        self.q03e_comparison_path = q03e_dir / "q03b-q03c-q03d-q03e-25s.png"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.store = DashboardStore(database_path)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-job")
        self.job_lock = threading.Lock()
        self.drive_scan_lock = threading.Lock()
        self.active_job_id: str | None = None
        self.pending_proposals: dict[str, dict[str, Any]] = {}
        self.drive_store = None
        self.drive_oauth = None
        self.drive_client = None
        self.drive_auto_scanner = DriveScanScheduler(self)
        drive_config = DriveOAuthConfig.from_environment()
        try:
            self.drive_store = GoogleDriveStore(
                self.store.database_path.with_name("google_drive.sqlite3"),
                drive_config.session_secret,
            )
            self.drive_oauth = GoogleDriveOAuth(drive_config, self.drive_store)
            self.drive_client = GoogleDriveClient(self.drive_oauth)
        except ValueError:
            logger.warning("Google Drive OAuth storage is unavailable because SESSION_SECRET is not configured.")

    def health(self) -> dict[str, Any]:
        drive_status = self.drive_status()
        modal_status = modal_configuration_status()
        try:
            config = PipelineConfig.from_environment()
            environment = config.shotstack_environment
        except ValueError:
            environment = "configuración inválida"
        return {
            "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
            "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
            "shotstack_key_configured": bool(os.getenv("SHOTSTACK_API_KEY")),
            "shotstack_environment": environment,
            "modal": modal_status,
            "r2_storage": r2_configuration_status(),
            "drive_connected": bool(drive_status.get("ready") and drive_status.get("verified")),
            "drive_oauth_ready": bool(drive_status.get("ready")),
            "drive_picker_configured": all(picker_configuration().get(key) for key in ("client_id", "api_key", "app_id")),
        }

    def _source_render_state(self, upload: dict[str, Any]) -> tuple[str, str]:
        """Return the current full-render state for compatibility with old callers."""
        return self._source_render_state_for_mode(upload, "full")

    def _source_render_state_for_mode(
        self, upload: dict[str, Any], render_mode: str
    ) -> tuple[str, str]:
        """Keep one full render and one explicitly requested 5-second demo per source."""
        history = self.store.job_history_for_source(upload["id"])
        history = sorted(
            history,
            key=lambda item: float(item.get("created_at") or 0),
            reverse=True,
        )
        for job in history:
            try:
                options = json.loads(job.get("edit_options_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                options = {}
            saved_mode = options.get("render_mode", "full")
            if saved_mode != render_mode:
                continue
            backend = options.get("renderer_backend", "shotstack_stage")
            if backend not in {"shotstack_stage", "modal"}:
                backend = "shotstack_stage"
            return str(job.get("status") or "ready"), backend

        ledger_key = hashlib.sha256(
            f"{upload['source_sha256']}:{render_mode}".encode("utf-8")
        ).hexdigest()
        processing = self.store.ledger.get(ledger_key)
        # Existing records predate separate demo/full job keys and represent a full attempt.
        if not processing and render_mode == "full":
            processing = self.store.ledger.get(upload["source_sha256"])
        if processing:
            try:
                options = json.loads(processing.get("edit_options_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                options = {}
            backend = options.get("renderer_backend", "shotstack_stage")
            if backend not in {"shotstack_stage", "modal"}:
                backend = "shotstack_stage"
            return str(processing.get("status") or "ready"), backend
        return "ready", "shotstack_stage"

    def drive_config(self) -> dict[str, Any]:
        return {
            "picker": picker_configuration(),
            "folders": load_folder_selections(),
            "files": load_file_selections(),
        }

    def drive_status(self) -> dict[str, Any]:
        if not self.drive_oauth:
            status = {
                "enabled": False,
                "configured": False,
                "ready": False,
                "connected": False,
                "authorization_pending": True,
                "missing_configuration": ["SESSION_SECRET"],
                "scopes": [],
            }
            status["automatic_scan"] = self.drive_auto_scanner.status()
            return status
        try:
            status = self.drive_oauth.status()
            status["automatic_scan"] = self.drive_auto_scanner.status()
            return status
        except GoogleDriveError:
            status = {
                "enabled": self.drive_oauth.config.enabled,
                "configured": not self.drive_oauth.config.missing,
                "ready": False,
                "authorized": False,
                "verified": False,
                "connected": False,
                "authorization_pending": True,
                "storage_error": True,
                "missing_configuration": self.drive_oauth.config.missing,
                "scopes": [],
            }
            status["automatic_scan"] = self.drive_auto_scanner.status()
            return status

    def scan_drive(self) -> dict[str, Any]:
        if not self.drive_oauth or not self.drive_client or not self.drive_store:
            raise PanelError("El almacenamiento seguro de Drive no está disponible.", 503)
        status = self.drive_status()
        if status.get("needs_reauthorization"):
            raise PanelError(
                "La conexión guardada no incluye los permisos nuevos. Reautoriza Google Drive antes de escanear.",
                409,
            )
        if not status.get("ready") or not status.get("authorized"):
            raise PanelError("Google Drive no está conectado. No se consultó ninguna carpeta.", 409)
        if not status.get("identity_verified"):
            raise PanelError("La identidad de la cuenta secundaria no está verificada.", 403)
        if not self.drive_scan_lock.acquire(blocking=False):
            raise PanelError("Ya hay un escaneo de Drive en curso.", 409)
        imported: list[dict[str, Any]] = []
        skipped = 0
        failures: list[dict[str, str]] = []
        try:
            folders = configured_drive_folders()
            input_folder_id = folders["input"]
            logos_folder_id = folders["logos"]
            input_items = self.drive_client.list_children(input_folder_id)
            logo_items = self.drive_client.list_children(logos_folder_id)
            input_items = [
                item for item in input_items
                if input_folder_id in item.get("parents", [])
            ]
            logo_items = [
                item for item in logo_items
                if logos_folder_id in item.get("parents", [])
            ]
            videos = [
                item
                for item in input_items
                if Path(str(item.get("name") or "")).suffix.lower() in VIDEO_EXTENSIONS
            ]
            docs = [item for item in input_items if item.get("mimeType") == GOOGLE_DOC_MIME]
            for remote in videos:
                drive_file_id = str(remote.get("id") or "")
                filename = str(remote.get("name") or "")
                if not drive_file_id or self.drive_store.get_import(drive_file_id):
                    skipped += 1
                    continue
                local_item = None
                suffix = Path(filename).suffix.lower()
                if suffix not in VIDEO_EXTENSIONS:
                    skipped += 1
                    continue
                try:
                    doc_matches = [
                        doc for doc in docs
                        if Path(str(doc.get("name") or "")).stem.casefold()
                        == Path(filename).stem.casefold()
                    ]
                    doc_id = str(doc_matches[0].get("id")) if len(doc_matches) == 1 else None
                    overrides: dict[str, Any] = {}
                    if doc_id:
                        text = self.drive_client.export_document_text(doc_id)
                        overrides = parse_style_instructions(text)

                    logo_matches = [
                        logo
                        for logo in logo_items
                        if Path(str(logo.get("name") or "")).stem.casefold()
                        == Path(filename).stem.casefold()
                        and Path(str(logo.get("name") or "")).suffix.lower()
                        in {".png", ".jpg", ".jpeg", ".webp"}
                    ]
                    logo_path: str | None = None
                    if len(logo_matches) == 1:
                        logo = logo_matches[0]
                        try:
                            size = int(logo.get("size") or 0)
                        except (TypeError, ValueError):
                            size = 0
                        if 0 < size <= 20 * 1024 * 1024:
                            asset_dir = self.input_dir.parent / "drive_assets"
                            logo_suffix = Path(str(logo.get("name"))).suffix.lower()
                            asset_name = hashlib.sha256(
                                str(logo.get("id")).encode("utf-8")
                            ).hexdigest() + logo_suffix
                            asset_path = asset_dir / asset_name
                            if not asset_path.exists():
                                self.drive_client.download_file(
                                    str(logo.get("id")), asset_path
                                )
                            logo_path = str(asset_path)

                    with tempfile.TemporaryDirectory(
                        prefix="matrix-drive-", dir=self.input_dir.parent
                    ) as staging:
                        staged_path = Path(staging) / f"{drive_file_id}{suffix}"
                        self.drive_client.download_file(drive_file_id, staged_path)
                        with staged_path.open("rb") as source:
                            local_item = self.upload(
                                filename, staged_path.stat().st_size, source
                            )

                    added = self.drive_store.add_import(
                        drive_file_id=drive_file_id,
                        local_upload_id=local_item["id"],
                        filename=filename,
                        md5_checksum=remote.get("md5Checksum"),
                        doc_file_id=doc_id,
                        style_overrides=overrides,
                        logo_path=logo_path,
                    )
                    if not added:
                        raise PanelError("El vídeo ya se había importado en otro escaneo.", 409)
                    imported.append({"id": local_item["id"], "filename": filename})
                except Exception as exc:
                    if local_item:
                        row = self.store.get_upload(local_item["id"])
                        if row and self.store.delete_upload_if_unused(local_item["id"]):
                            (self.input_dir / row["filename"]).unlink(missing_ok=True)
                            (self.thumbnail_dir / f"{local_item['id']}.jpg").unlink(missing_ok=True)
                    logger.warning(
                        "Drive import failed for file %s (%s)",
                        hashlib.sha256(drive_file_id.encode("utf-8")).hexdigest()[:12],
                        type(exc).__name__,
                    )
                    failures.append({"filename": filename, "error": str(exc)[:300]})
            return {
                "imported": imported,
                "skipped": skipped,
                "failed": failures,
                "items": self.library(),
            }
        except GoogleDriveError as exc:
            raise PanelError(str(exc), 502) from exc
        finally:
            self.drive_scan_lock.release()

    def select_drive_folder(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La selección de Drive debe ser un objeto JSON.")
        role = str(payload.get("role") or "")
        folder_id = str(payload.get("folder_id") or "")
        if role not in {"entrada", "logos", "procesados"}:
            raise PanelError("Selecciona Entrada, Procesados o Logos.")
        expected_folder_id = {
            "entrada": configured_drive_folders()["input"],
            "procesados": configured_drive_folders()["processed"],
            "logos": configured_drive_folders()["logos"],
        }[role]
        if folder_id != expected_folder_id:
            raise PanelError("MATRIX MOTION solo admite sus tres carpetas configuradas.", 403)
        access_token = self._verified_picker_access_token(payload)
        try:
            folder = get_folder(folder_id, access_token=access_token)
            previous_id = load_folder_selections().get(role, {}).get("id")
            selections = save_folder_selection(
                role,
                {"id": folder["id"], "name": folder["name"]},
            )
            if role in {"entrada", "logos"} and previous_id != folder["id"]:
                save_file_selections(role, [])
                if role == "entrada":
                    save_file_selections("instrucciones", [])
        except DriveAccessError as exc:
            raise PanelError(str(exc), 403) from exc
        return {
            "folders": selections,
            "role": role,
            "accessible_file_count": 0,
            "folder": {"id": folder["id"], "name": folder["name"]},
        }

    def _verified_picker_access_token(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud de Google Drive no es válida.")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise PanelError("Autoriza Google Drive en esta sesión con drive.file.", 403)
        if not self.drive_oauth:
            raise PanelError("La validación de identidad de Google no está disponible.", 503)
        try:
            self.drive_oauth.verify_access_token_identity(access_token)
        except GoogleDriveError as exc:
            raise PanelError(str(exc), 403) from exc
        return access_token

    def select_drive_files(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La selección de archivos de Drive no es válida.")
        role = str(payload.get("role") or "")
        files = payload.get("files")
        if role not in {"entrada", "logos", "instrucciones"}:
            raise PanelError("Solo se pueden autorizar MP4, instrucciones o logos.")
        access_token = self._verified_picker_access_token(payload)
        if not isinstance(files, list) or not files or len(files) > 500:
            raise PanelError("Selecciona entre 1 y 500 archivos en Google Picker.")
        folder_role = "entrada" if role == "instrucciones" else role
        folder = load_folder_selections().get(folder_role)
        if not folder:
            raise PanelError(f"Selecciona primero la carpeta {folder_role.title()} con Google Picker.")
        expected_folder_id = configured_drive_folders()[
            {"entrada": "input", "logos": "logos"}[folder_role]
        ]
        if folder.get("id") != expected_folder_id:
            raise PanelError("La carpeta guardada no es una carpeta autorizada para MATRIX MOTION.", 403)
        authorized = []
        expected_kind = {
            "entrada": "video",
            "logos": "logo",
            "instrucciones": "document",
        }[role]
        try:
            for selected in files:
                if not isinstance(selected, dict) or not isinstance(selected.get("id"), str):
                    raise PanelError("Google Picker devolvió un archivo no válido.")
                item = get_drive_file(selected["id"], access_token=access_token)
                if folder["id"] not in item.get("parents", []):
                    raise PanelError("Cada archivo elegido debe estar directamente en la carpeta seleccionada.")
                suffix = Path(item["name"]).suffix.lower()
                mime = item.get("mimeType", "")
                if expected_kind == "video" and suffix != ".mp4" and mime != "video/mp4":
                    raise PanelError("Entrada solo admite archivos MP4.")
                if expected_kind == "logo" and suffix not in {".png", ".jpg", ".jpeg", ".webp"} and mime not in {
                    "image/png", "image/jpeg", "image/webp"
                }:
                    raise PanelError("Logos solo admite PNG, JPG, JPEG y WebP.")
                if expected_kind == "document" and mime != GOOGLE_DOC_MIME:
                    raise PanelError("Instrucciones solo admite Google Docs.")
                authorized.append(item)
            selections = save_file_selections(role, authorized)
        except DriveAccessError as exc:
            raise PanelError(str(exc), 403) from exc
        return {"role": role, "files": selections.get(role, [])}

    def list_drive_files(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud de lista de Drive no es válida.")
        role = str(payload.get("role") or "")
        if role not in {"entrada", "logos", "instrucciones"}:
            raise PanelError("Solo se pueden enumerar MP4, instrucciones y logos.")
        self._verified_picker_access_token(payload)
        folder_role = "entrada" if role == "instrucciones" else role
        folder = load_folder_selections().get(folder_role)
        if not folder:
            raise PanelError(f"Selecciona primero la carpeta {role.title()} con Google Picker.")
        expected_folder_id = configured_drive_folders()[
            {"entrada": "input", "logos": "logos"}[folder_role]
        ]
        if folder.get("id") != expected_folder_id:
            raise PanelError("La carpeta guardada no es una carpeta autorizada para MATRIX MOTION.", 403)
        files = [
            item
            for item in load_file_selections().get(role, [])
            if folder["id"] in item.get("parents", [])
        ]
        return {"role": role, "files": files}

    def sync_drive(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud de descarga de Drive no es válida.")
        access_token = self._verified_picker_access_token(payload)
        try:
            config = PipelineConfig.from_environment()
            result = sync_drive_media(
                config,
                access_token,
                input_dir=self.input_dir,
                logo_dir=self.input_dir.parent / "logos",
            )
        except DriveAccessError as exc:
            raise PanelError(str(exc), 403) from exc

        registered = []
        for item in result["videos"]:
            try:
                registered.append(self.register_drive_video(item["local_path"], item["name"]))
            except PanelError as exc:
                item["library_error"] = str(exc)
        result["library_items"] = registered
        return result

    def register_drive_video(self, local_path: str, original_name: str) -> dict[str, Any]:
        path = Path(local_path).resolve()
        if path.parent != self.input_dir or path.suffix.lower() != ".mp4" or not path.is_file():
            raise PanelError("El MP4 descargado no está en la carpeta local de entrada.")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
                digest.update(block)
        source_hash = digest.hexdigest()
        existing = self.store.get_upload_by_hash(source_hash)
        if existing:
            return self._public_library_item(existing)
        style = load_video_style(self.style_path)
        try:
            metadata = probe_video(path)
        except (MediaError, OSError, ValueError) as exc:
            raise PanelError(f"El MP4 descargado no pasó la validación: {exc}", 422) from exc
        if metadata.video_codec not in ALLOWED_CODECS or not metadata.has_audio:
            raise PanelError("El MP4 descargado necesita vídeo H.264/HEVC y pista de audio.", 422)
        if metadata.duration > style.max_video_duration_seconds:
            raise PanelError("El MP4 descargado supera el límite de duración configurado.", 422)
        file_id = uuid.uuid4().hex
        self.store.add_upload(
            file_id,
            path.name,
            Path(original_name).name[:180],
            source_hash,
            path.stat().st_size,
            _metadata_dict(metadata),
        )
        return self.library_item(file_id)

    def create_drive_test_file(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud de prueba de Drive no es válida.")
        folder = load_folder_selections().get("procesados")
        if not folder or folder.get("id") != configured_drive_folders()["processed"]:
            raise PanelError("Selecciona primero la carpeta Procesados con Google Picker.")
        access_token = self._verified_picker_access_token(payload)
        try:
            return create_test_file(folder["id"], access_token=access_token)
        except DriveAccessError as exc:
            raise PanelError(str(exc), 403) from exc

    def test_drive_output_write(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise PanelError(
                "Confirma la creación y eliminación de un MP4 temporal para probar 02_PROCESADOS.",
                400,
            )
        if not self.drive_oauth or not self.drive_client:
            raise PanelError("El almacenamiento seguro de Drive no está disponible.", 503)
        status = self.drive_status()
        if status.get("needs_reauthorization"):
            raise PanelError("Reautoriza Google Drive con los permisos nuevos antes de probar la escritura.", 409)
        if not status.get("ready") or not status.get("authorized") or not status.get("identity_verified"):
            raise PanelError("Conecta la cuenta secundaria autorizada antes de probar la escritura.", 409)
        processed_folder_id = configured_drive_folders()["processed"]
        if not self.drive_scan_lock.acquire(blocking=False):
            raise PanelError("Espera a que termine el escaneo de Drive antes de probar la escritura.", 409)

        probe_id = uuid.uuid4().hex
        source_key = hashlib.sha256(f"write-probe:{probe_id}".encode("ascii")).hexdigest()
        filename = f"matrix-motion-write-test-{probe_id}.mp4"
        remote_id: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="matrix-drive-write-test-") as directory:
                video_path = Path(directory) / filename
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=black:s=360x640:r=24:d=1",
                            "-an", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                            "-y", str(video_path),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=45,
                    )
                    probe_video(video_path)
                except (OSError, subprocess.SubprocessError, MediaError) as exc:
                    logger.warning("Could not create the local Drive write probe (%s)", type(exc).__name__)
                    raise PanelError("No se pudo preparar el MP4 temporal para la prueba.", 503) from exc

                try:
                    created = self.drive_client.upload_new_file(
                        video_path,
                        folder_id=processed_folder_id,
                        app_properties={"matrixMotionSource": source_key},
                    )
                    remote_id = str(created.get("id") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", remote_id):
                        raise GoogleDriveError("Drive no confirmó el archivo temporal creado.")
                    if (
                        created.get("mimeType") != "video/mp4"
                        or processed_folder_id not in (created.get("parents") or [])
                    ):
                        raise GoogleDriveError("Drive no confirmó la carpeta o el tipo del archivo temporal.")
                    self.drive_client.delete_created_file(remote_id)
                    remote_id = None
                except Exception as exc:
                    if not remote_id:
                        try:
                            possible_orphan = self.drive_client.find_output_by_source(
                                source_key,
                                processed_folder_id,
                            )
                            remote_id = str(possible_orphan.get("id") or "") if possible_orphan else None
                        except Exception as cleanup_lookup_error:
                            logger.warning(
                                "Could not locate a possibly incomplete Drive write probe (%s).",
                                type(cleanup_lookup_error).__name__,
                            )
                    if remote_id:
                        try:
                            self.drive_client.delete_created_file(remote_id)
                            remote_id = None
                        except Exception:
                            logger.error(
                                "Could not remove the temporary Drive write-test file (id hash %s).",
                                hashlib.sha256(remote_id.encode("utf-8")).hexdigest()[:12],
                            )
                            raise PanelError(
                                "La prueba creó un MP4 temporal y no pudo borrarlo. "
                                f"El archivo de prueba queda en 02_PROCESADOS con identificador {remote_id}.",
                                502,
                            ) from None
                    if isinstance(exc, GoogleDriveError):
                        raise PanelError(str(exc), 502) from exc
                    if isinstance(exc, PanelError):
                        raise
                    logger.warning("Drive MP4 write test failed (%s)", type(exc).__name__)
                    raise PanelError(
                        "Google Drive no pudo completar la prueba de escritura. "
                        f"Si el archivo temporal llegó a crearse, busca {filename} en 02_PROCESADOS.",
                        502,
                    ) from exc
            return {"created_and_deleted": True, "mime_type": "video/mp4", "destination": "02_PROCESADOS"}
        finally:
            self.drive_scan_lock.release()

    def _style_for_upload(self, upload_id: str) -> VideoStyle:
        style = load_video_style(self.style_path)
        if not self.drive_store:
            return style
        drive_import = self.drive_store.get_import_for_local_upload(upload_id)
        if not drive_import:
            return style
        try:
            overrides = json.loads(drive_import["style_overrides_json"] or "{}")
            if not isinstance(overrides, dict):
                overrides = {}
        except (json.JSONDecodeError, TypeError):
            overrides = {}
        merged = asdict(style)
        merged.update(overrides)
        logo_path = drive_import.get("logo_path")
        if logo_path:
            candidate = Path(logo_path).resolve()
            allowed_dir = (self.input_dir.parent / "drive_assets").resolve()
            if candidate.parent == allowed_dir and candidate.is_file():
                merged["logo_path"] = str(candidate)
        with tempfile.TemporaryDirectory(prefix="matrix-style-") as temporary:
            return save_video_style(merged, Path(temporary) / "style.json")

    def retry_drive_upload(self, source_id: str) -> dict[str, Any]:
        """Retry only the Drive write for an already-rendered Drive source."""
        if not re.fullmatch(r"[0-9a-f]{32}", source_id):
            raise PanelError("Identificador de vídeo no válido.")
        if not self.drive_store or not self.drive_oauth or not self.drive_client:
            raise PanelError("La conexión de Google Drive no está disponible.", 503)
        status = self.drive_status()
        if not status.get("ready") or not status.get("authorized"):
            raise PanelError("Google Drive no está conectado.", 409)
        upload = self.store.get_upload(source_id)
        drive_import = self.drive_store.get_import_for_local_upload(source_id)
        if not upload or not drive_import:
            raise PanelError("El vídeo no procede de Google Drive.", 404)
        if drive_import.get("status") == "uploaded":
            return {
                "status": "uploaded",
                "output_drive_file_id": drive_import.get("output_drive_file_id"),
                "render_reused": True,
            }
        if drive_import.get("status") != "upload_failed":
            raise PanelError(
                "La subida no está marcada como fallida. No se iniciará otra subida ni un render.",
                409,
            )
        latest_job = self.store.latest_job_for_source(source_id)
        if not latest_job or latest_job.get("status") not in {"failed", "completed"}:
            raise PanelError("No hay un resultado listo para volver a subir.", 409)
        ledger = self.store.ledger.get(upload["source_sha256"])
        output_value = ledger.get("output_path") if isinstance(ledger, dict) else None
        if not output_value:
            raise PanelError("No se encontró un render local que pueda volver a subirse.", 409)
        output_path = Path(output_value).resolve()
        if output_path.parent != self.output_dir or not output_path.is_file() or output_path.stat().st_size == 0:
            raise PanelError("El render local ya no está disponible. No se inició otro render.", 409)
        try:
            rendered = probe_video(output_path)
            if rendered.width != 1080 or rendered.height != 1920:
                raise PanelError("El archivo local no coincide con el formato vertical esperado.", 409)
            source_key = hashlib.sha256(
                drive_import["drive_file_id"].encode("utf-8")
            ).hexdigest()
            processed_folder_id = PipelineConfig.from_environment().processed_folder_id
            if not processed_folder_id:
                raise PanelError("Selecciona la carpeta Procesados con Google Picker antes de volver a subir.", 409)
            remote_output = self.drive_client.upload_new_file(
                output_path,
                folder_id=processed_folder_id,
                app_properties={"matrixMotionSource": source_key},
            )
        except (GoogleDriveError, DriveUploadError, MediaError, OSError) as exc:
            raise PanelError(
                f"No se pudo volver a subir el render. El archivo local se conserva: {exc}",
                502,
            ) from exc
        output_drive_file_id = remote_output.get("id") if isinstance(remote_output, dict) else None
        self.drive_store.update_import(
            drive_import["drive_file_id"],
            status="uploaded",
            error=None,
            output_drive_file_id=output_drive_file_id,
        )
        self.store.ledger.mark_completed(upload["source_sha256"], str(output_path))
        result: dict[str, Any] = {}
        try:
            parsed_result = json.loads(latest_job.get("result_json") or "{}")
            if isinstance(parsed_result, dict):
                result = parsed_result
        except json.JSONDecodeError:
            pass
        result.update(
            source=str(self._source_path(upload)),
            output=str(output_path),
            drive_output=remote_output,
            drive_upload_skipped=False,
        )
        self.store.update_job(
            latest_job["id"],
            status="completed",
            phase="completed",
            progress=100,
            error=None,
            output_id=output_path.name,
            result_json=json.dumps(result, ensure_ascii=False),
        )
        return {
            "status": "uploaded",
            "output_drive_file_id": output_drive_file_id,
            "render_reused": True,
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

    def upload(
        self,
        filename: str,
        content_length: int,
        stream,
        *,
        niche: str = DEFAULT_VIDEO_NICHE,
        style_notes: str = "",
        editing_style: str = "dynamic",
    ) -> dict[str, Any]:
        if content_length <= 0:
            raise PanelError("El archivo está vacío.")
        if niche not in VIDEO_NICHE_LABELS:
            raise PanelError("La categoría del vídeo no es válida.")
        if editing_style not in VIDEO_EDIT_STYLE_LABELS:
            raise PanelError("El estilo de edición no es válido.")
        if not isinstance(style_notes, str):
            raise PanelError("Las notas de estilo deben ser texto.")
        style_notes = style_notes.strip()
        if len(style_notes) > MAX_STYLE_NOTES_CHARS:
            raise PanelError(
                f"Las notas de estilo no pueden superar {MAX_STYLE_NOTES_CHARS} caracteres."
            )
        if any(ord(char) < 32 and char not in "\n\t" for char in style_notes):
            raise PanelError("Las notas de estilo contienen caracteres de control no permitidos.")
        caption_style_overrides = _parse_caption_style_overrides(style_notes)
        style = load_video_style(self.style_path)
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
            self._create_thumbnail(final_path, file_id)
            public_metadata = _metadata_dict(metadata)
            public_metadata.update({
                "niche": niche,
                "niche_label": VIDEO_NICHE_LABELS[niche],
                "style_notes": style_notes,
                "editing_style": editing_style,
                "editing_style_label": VIDEO_EDIT_STYLE_LABELS[editing_style],
                "caption_style_overrides": caption_style_overrides,
            })
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

    def _create_thumbnail(self, source_path: Path, file_id: str) -> Path | None:
        """Create a local preview frame without changing or uploading the source."""
        temporary = self.thumbnail_dir / f".{file_id}.tmp.jpg"
        target = self.thumbnail_dir / f"{file_id}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "0.1", "-i", str(source_path), "-frames:v", "1",
                    "-vf", "scale=320:-2:flags=fast_bilinear", "-q:v", "5", str(temporary),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise OSError("FFmpeg no produjo una miniatura válida.")
            os.replace(temporary, target)
            return target
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("No se pudo generar la miniatura del vídeo %s: %s", file_id, exc)
            temporary.unlink(missing_ok=True)
            return None

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
        processing = self.store.ledger.get(upload["source_sha256"])
        attempt_history = self.store.job_history_for_source(upload["id"])
        estimate = _estimate_cost(
            float(metadata["duration"]),
            style,
            include_transcription=False,
        )
        job = self.store.latest_job_for_source(upload["id"])
        latest_options = {}
        if job:
            status = job["status"]
            output_id = job["output_id"]
            error = job["error"]
            try:
                latest_options = json.loads(job.get("edit_options_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                latest_options = {}
        else:
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
        result = {
            "id": upload["id"],
            "filename": upload["filename"],
            "original_filename": upload["original_filename"],
            "created_at": upload["created_at"],
            "thumbnail_url": (
                f"/media/thumbnails/{upload['id']}"
                if (self.thumbnail_dir / f"{upload['id']}.jpg").is_file()
                else None
            ),
            "format": Path(upload["filename"]).suffix.lstrip(".").upper(),
            "size_bytes": upload["size_bytes"],
            "duration_seconds": metadata["duration"],
            "width": metadata["width"],
            "height": metadata["height"],
            "video_codec": metadata["video_codec"],
            "audio_codec": metadata["audio_codec"],
            "rotation": metadata["rotation"],
            "fps": metadata["fps"],
            "niche": metadata.get("niche", DEFAULT_VIDEO_NICHE),
            "niche_label": metadata.get(
                "niche_label",
                VIDEO_NICHE_LABELS.get(
                    metadata.get("niche", DEFAULT_VIDEO_NICHE),
                    VIDEO_NICHE_LABELS[DEFAULT_VIDEO_NICHE],
                ),
            ),
            "style_notes": metadata.get("style_notes", ""),
            "editing_style": metadata.get("editing_style", "dynamic"),
            "editing_style_label": metadata.get(
                "editing_style_label",
                VIDEO_EDIT_STYLE_LABELS.get(metadata.get("editing_style", "dynamic"), "Dinámico"),
            ),
            "caption_style_overrides": metadata.get("caption_style_overrides", {}),
            "status": status,
            "latest_job_backend": latest_options.get("renderer_backend", "shotstack_stage"),
            "output_id": output_id,
            "error": error,
            "cost_estimate_usd": estimate,
        }
        drive_import = (
            self.drive_store.get_import_for_local_upload(upload["id"])
            if self.drive_store
            else None
        )
        result["source"] = "google_drive" if drive_import else "local"
        result["source_label"] = "Google Drive" if drive_import else "Equipo"
        result["drive_status"] = drive_import.get("status") if drive_import else None
        result["drive_error"] = drive_import.get("error") if drive_import else None
        result["can_retry_drive_upload"] = bool(
            drive_import
            and drive_import.get("status") == "upload_failed"
            and self.store.latest_job_for_source(upload["id"])
        )
        attempts_used = int(processing.get("attempts") or 0)
        has_retry = any(int(attempt.get("attempt_number") or 1) > 1 for attempt in attempt_history)
        prior_error = f"{job.get('error') or ''} {processing.get('error') or ''}" if job else ""
        remote_status_check_available = bool(processing.get("render_id"))
        try:
            saved_processing_options = json.loads(processing.get("edit_options_json") or "{}")
            if not isinstance(saved_processing_options, dict):
                saved_processing_options = {}
            saved_style_data = saved_processing_options.get("style") or {}
            saved_style = VideoStyle(**saved_style_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            saved_processing_options = {}
            saved_style = None
        target = RENDER001_THIRD_RETRY_TARGET
        exact_target = (
            upload["id"] == target["source_id"]
            and upload["source_sha256"] == target["source_sha256"]
            and upload["original_filename"] == target["filename"]
            and job is not None
            and job.get("id") == target["job_id"]
            and job.get("retry_of_job_id") == target["previous_job_id"]
        )
        failed_history = (
            len(attempt_history) == 2
            and [int(attempt.get("attempt_number") or 1) for attempt in attempt_history] == [1, 2]
            and attempt_history[0].get("id") == target["previous_job_id"]
            and attempt_history[1].get("id") == target["job_id"]
            and all("HTTP 400" in str(attempt.get("error") or "") for attempt in attempt_history)
        )
        active_job = self.store.get_job(self.active_job_id) if self.active_job_id else None
        no_active_job = not active_job or active_job.get("status") in TERMINAL_STATUSES
        saved_plan = saved_processing_options.get("creative_plan")
        saved_mode = saved_processing_options.get("subtitle_mode")
        third_retry_options_match = bool(
            saved_style
            and _matches_render001_third_retry_configuration(
                saved_style,
                saved_mode,
                saved_plan if isinstance(saved_plan, dict) else {},
            )
            and latest_options.get("subtitle_mode") == "none"
            and latest_options.get("output_profile") == "1080p"
            and latest_options.get("output_quality") == "high"
            and latest_options.get("output_fps") == 50
            and latest_options.get("creative_plan") == RENDER001_THIRD_RETRY_PLAN
        )
        result.update({
            "attempt_history": attempt_history,
            "latest_job_id": job.get("id") if job else None,
            "latest_attempt_number": int(job.get("attempt_number") or 1) if job else None,
            "retry_settings": latest_options if isinstance(latest_options, dict) else {},
            "retry_estimated_cost_usd": job.get("estimated_cost_usd") if job else None,
            "retry_remote_status_check_available": remote_status_check_available,
            "retry_will_upload_to_drive": bool(drive_import),
            "can_retry_render": bool(
                job
                and job.get("status") == "failed"
                and int(job.get("attempt_number") or 1) == 1
                and processing.get("status") == "failed"
                and attempts_used == 1
                and not has_retry
                and (remote_status_check_available or "HTTP 400" in prior_error)
            ),
            "can_run_controlled_third_render": bool(
                exact_target
                and failed_history
                and job.get("status") == "failed"
                and processing.get("status") == "failed"
                and attempts_used == 2
                and not remote_status_check_available
                and "HTTP 400" in prior_error
                and third_retry_options_match
                and no_active_job
            ),
        })
        return result

    def prepare_proposal(self, payload: Any) -> dict[str, Any]:
        """Inspect one local source and prepare an expiring plan without provider calls."""
        if not isinstance(payload, dict):
            raise PanelError("La propuesta debe ser un objeto JSON.")
        source_id = str(payload.get("source_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", source_id):
            raise PanelError("Identificador de vídeo no válido.")
        upload = self.store.get_upload(source_id)
        if not upload:
            raise PanelError("No se encontró el vídeo seleccionado.", 404)
        try:
            upload_metadata = json.loads(upload["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            upload_metadata = {}
        niche = upload_metadata.get("niche", DEFAULT_VIDEO_NICHE)
        niche_label = upload_metadata.get(
            "niche_label", VIDEO_NICHE_LABELS.get(niche, VIDEO_NICHE_LABELS[DEFAULT_VIDEO_NICHE])
        )
        style_notes = upload_metadata.get("style_notes", "")
        caption_style_overrides = upload_metadata.get("caption_style_overrides")
        if not isinstance(caption_style_overrides, dict):
            caption_style_overrides = _parse_caption_style_overrides(style_notes)
        panel_style, render_settings, panel_notes = _validate_panel_customization(
            payload.get("customization")
        )
        if panel_notes:
            style_notes = f"{style_notes}\n\n{panel_notes}".strip()
        if len(style_notes) > 4000:
            raise PanelError("Las notas combinadas superan el límite de 4.000 caracteres.")

        subtitle_mode = str(payload.get("subtitle_mode") or "none")
        if subtitle_mode not in {"dynamic", "simple", "none"}:
            raise PanelError("Elige subtítulos dinámicos, sencillos o sin subtítulos.")
        render_backend = str(payload.get("render_backend") or "modal")
        if render_backend != "modal":
            raise PanelError("Shotstack está deshabilitado. La aplicación solo permite propuestas para Modal.", 409)
        if subtitle_mode == "simple":
            raise PanelError(
                "El motor Remotion de Modal admite subtítulos dinámicos o ninguno; "
                "elige una de esas opciones."
            )
        render_mode = str(payload.get("render_mode") or "full")
        if render_mode not in {"full", "quick_demo"}:
            raise PanelError("Elige creación completa o prueba rápida de 5 segundos.")
        output_profile = str(payload.get("output_profile") or "1080p")
        if output_profile not in OUTPUT_PROFILES:
            raise PanelError(
                "El perfil solicitado no está disponible en Modal; se requiere 1080 × 1920."
            )
        output_width, output_height = OUTPUT_PROFILES[output_profile]
        output_quality = str(payload.get("output_quality") or "high")
        if output_quality != "high":
            raise PanelError("Modal usa el perfil de calidad alta aprobado.")
        try:
            raw_output_fps = float(payload.get("output_fps", 30))
        except (TypeError, ValueError):
            raise PanelError("Los FPS de salida deben ser 25, 30, 50 o 60.") from None
        if not raw_output_fps.is_integer() or raw_output_fps not in {25, 30, 50, 60}:
            raise PanelError("Los FPS de salida deben ser 25, 30, 50 o 60.")
        output_fps = int(raw_output_fps)
        if output_profile != "1080p" or output_fps != 30:
            raise PanelError("Remotion en Modal está preparado para 1080 × 1920 a 30 FPS.")

        item = self._public_library_item(upload)
        existing_status, existing_backend = self._source_render_state_for_mode(upload, render_mode)
        if existing_status in {"processing", "queued"}:
            raise PanelError("Este vídeo ya tiene un trabajo activo.", 409)
        if existing_status in {"completed", "failed"} and existing_backend == render_backend:
            raise PanelError(
                "Este motor ya tiene un resultado o un intento fallido para este vídeo. "
                "No se repetirá automáticamente.",
                409,
            )

        style = replace(
            self._style_for_upload(source_id),
            **{**caption_style_overrides, **panel_style},
            output_width=output_width,
            output_height=output_height,
            output_fps=output_fps,
            output_quality=output_quality,
        )
        if render_backend == "modal" and style.logo_path:
            raise PanelError(
                "El motor Remotion en Modal no aplica el logo global configurado; "
                "no se preparó una propuesta que lo omita."
            )
        video = probe_video(self._source_path(upload))
        try:
            creative_plan = normalize_edit_plan(payload.get("creative_plan"), video.duration)
        except EditPlanError as exc:
            raise PanelError(str(exc)) from None
        if render_backend == "modal" and creative_plan["zooms"]:
            raise PanelError(
                "Los zooms manuales aún no están soportados por Remotion en Modal; "
                "el montaje conserva el reencuadre suave automático."
            )
        if video.video_codec not in ALLOWED_CODECS:
            raise PanelError("El vídeo debe usar H.264 o HEVC/H.265.")
        if video.duration > style.max_video_duration_seconds:
            raise PanelError(
                f"El vídeo supera el límite configurado de {style.max_video_duration_seconds} segundos.",
                413,
            )
        transcription_required = subtitle_mode != "none"
        if transcription_required and not video.has_audio:
            raise PanelError("Para generar subtítulos se necesita una pista de audio.")
        transcription_chunks = (
            (int(video.duration) + 899) // 900 if transcription_required else 0
        )
        if transcription_required and transcription_chunks > style.max_transcription_chunks:
            raise PanelError(
                f"El vídeo requiere {transcription_chunks} fragmentos de transcripción; "
                f"el máximo configurado es {style.max_transcription_chunks}.",
                413,
            )
        if style.logo_path:
            logo = Path(style.logo_path).expanduser().resolve()
            if (
                not logo.is_file()
                or logo.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
                or logo.stat().st_size > 20 * 1024 * 1024
            ):
                raise PanelError("El logo configurado no es accesible o supera los límites admitidos.")
        estimate = None
        if style.max_estimated_cost_usd is not None:
            if estimate is None:
                raise PanelError(
                    "Hay un límite de gasto configurado, pero faltan tarifas para verificarlo.",
                    409,
                )
            if estimate > style.max_estimated_cost_usd:
                raise PanelError(
                    f"La estimación ${estimate:.4f} supera el límite configurado "
                    f"${style.max_estimated_cost_usd:.4f}.",
                    409,
                )

        minutes = video.duration / 60
        openai_cost = (
            0.0
            if not transcription_required
            else (
                round(minutes * style.openai_price_per_audio_minute_usd, 4)
                if style.openai_price_per_audio_minute_usd is not None
                else None
            )
        )
        shotstack_cost = None
        modal_status = modal_configuration_status()
        drive_import = (
            self.drive_store.get_import_for_local_upload(source_id)
            if self.drive_store
            else None
        )
        source_ratio = video.width / video.height if video.width and video.height else 9 / 16
        target_ratio = style.output_width / style.output_height
        proposal_id = uuid.uuid4().hex
        now = time.time()
        options = {
            "subtitle_mode": subtitle_mode,
            "style_notes": style_notes,
            "caption_style_overrides": caption_style_overrides,
            "output_profile": output_profile,
            "output_quality": output_quality,
            "output_fps": output_fps,
            "creative_plan": creative_plan,
            "renderer_backend": render_backend,
            "render_mode": render_mode,
            "render_settings": render_settings,
        }
        proposal = {
            "proposal_id": proposal_id,
            "source_id": source_id,
            "source_sha256": upload["source_sha256"],
            "options": options,
            "style": asdict(style),
            "estimate": estimate,
            "created_at": now,
            "expires_at": now + 900,
            "used": False,
        }
        with self.job_lock:
            self.pending_proposals = {
                key: value
                for key, value in self.pending_proposals.items()
                if value.get("expires_at", 0) > now and not value.get("used")
            }
            if len(self.pending_proposals) >= 100:
                oldest = min(
                    self.pending_proposals,
                    key=lambda key: self.pending_proposals[key].get("created_at", 0),
                )
                self.pending_proposals.pop(oldest, None)
            self.pending_proposals[proposal_id] = proposal

        return {
            "proposal_id": proposal_id,
            "expires_in_seconds": 900,
            "source_id": source_id,
            "source_name": upload["original_filename"],
            "source": {
                "duration_seconds": round(video.duration, 3),
                "width": video.width,
                "height": video.height,
                "video_codec": video.video_codec,
                "audio_codec": video.audio_codec,
                "fps": video.fps,
                "video_bit_rate_bps": video.video_bit_rate_bps,
                "format_bit_rate_bps": video.format_bit_rate_bps,
                "origin": "Google Drive · 01_ENTRADA" if drive_import else "Subida local",
            },
            "edit": {
                "subtitle_mode": subtitle_mode,
                "render_mode": render_mode,
                "niche": niche,
                "niche_label": niche_label,
                "style_notes": style_notes,
                "caption_style_overrides": caption_style_overrides,
                "render_settings": render_settings,
                "caption_style": {
                    "fontFamily": style.font_family,
                    "highlightColor": style.highlight_color,
                    "textColor": style.font_color,
                    "fontSize": style.font_size,
                    "position": style.caption_position,
                    "maxWordsPerCaption": style.max_words_per_caption,
                },
                "output_width": style.output_width,
                "output_height": style.output_height,
                "output_fps": output_fps,
                "output_profile": output_profile,
                "output_quality": output_quality,
                "expected_output_duration_seconds": edited_duration(video.duration, creative_plan["cuts"]),
                "framing": (
                    "rellenar; la relación 9:16 coincide, sin recorte previsto"
                    if abs(source_ratio - target_ratio) <= 0.015
                    else "contener sin deformar; puede haber bandas"
                ),
                "keep_original_audio": video.has_audio,
                "logo_enabled": bool(style.logo_path),
                "background_music": False,
                "cut_count": len(creative_plan["cuts"]),
                "zoom_count": len(creative_plan["zooms"]),
                "sticker_count": len(creative_plan["stickers"]),
                "creative_plan": creative_plan,
                "transitions": False,
                "higher_resolution_note": (
                    "Remotion en Modal está fijado a 1080 × 1920."
                    if render_backend == "modal"
                    else "La salida de Modal está limitada a 1080 × 1920."
                ),
            },
            "api_plan": {
                "openai_transcription": transcription_required,
                "openai_transcription_chunks": transcription_chunks,
                "renderer_backend": render_backend,
                "shotstack_environment": None,
                "shotstack_render_jobs": 0,
                "shotstack_ingested_assets": 0,
                "max_renders_per_attempt": 1,
                "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
                "shotstack_key_configured": False,
                "modal_sdk_installed": modal_status["sdk_installed"],
                "modal_credentials_configured": modal_status["credentials_configured"],
                "modal_deployment_status": modal_status["deployment_status"],
                "external_services_called_during_proposal": [],
            },
            "costs": {
                "openai_usd": openai_cost,
                "shotstack_usd": shotstack_cost,
                "modal_compute_usd": None,
                "total_estimate_usd": estimate,
                "estimate_note": (
                    "El coste del cómputo Modal depende de duración y recursos; el panel no tiene una tarifa cotizada."
                ),
            },
            "destination": (
                "Cloudflare R2 privado · matrix-motion-videos"
                + (" y Google Drive · 02_PROCESADOS" if drive_import else "")
            ),
            "warnings": [
                warning
                for warning in [
                    f"La salida {style.output_width} × {style.output_height} reduce la resolución respecto a la entrada."
                    if video.width > style.output_width or video.height > style.output_height
                    else "",
                    f"La salida {style.output_width} × {style.output_height} amplía la entrada y no recupera detalle."
                    if video.width < style.output_width or video.height < style.output_height
                    else "",
                    (
                        "Modal ejecutará Remotion/FFmpeg en cómputo cloud; el panel no puede cotizar el coste final."
                        if render_backend == "modal"
                    else ""
                    ),
                    (
                        "No se consultó el saldo ni la tarifa de Modal."
                        if render_backend == "modal"
                        else ""
                    ),
                    (
                        "La salida Remotion está fijada a H.264 CRF 15, 1080 × 1920 y 30 FPS."
                        if render_backend == "modal"
                        else ""
                    ),
                    (
                        "No se inició ningún render al preparar esta propuesta."
                        if render_backend == "modal"
                        else ""
                    ),
                    "La estimación usa la duración original, no descuenta los cortes y no representa una tarifa de plan 4K."
                    if creative_plan["cuts"]
                    else "",
                    "No incluye transiciones, música ni efectos de sonido."
                    if video.has_audio
                    else "No incluye transiciones, música ni efectos de sonido.",
                    "Sin subtítulos: no se enviará audio a OpenAI."
                    if not transcription_required
                    else "",
                ]
                if warning
            ],
        }

    def start_job(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise PanelError("La solicitud del trabajo debe ser un objeto JSON.")
        if payload.get("authorize_modal_compute") is not True:
            raise PanelError("Falta la autorización explícita para usar cómputo Modal.", 403)
        proposal_id = str(payload.get("proposal_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise PanelError("Prepara y revisa una propuesta antes de autorizar el render.", 409)
        with self.job_lock:
            proposal = self.pending_proposals.get(proposal_id)
            if (
                not proposal
                or proposal.get("used")
                or proposal.get("expires_at", 0) <= time.time()
            ):
                raise PanelError(
                    "La propuesta no existe, ya se utilizó o ha caducado. Prepara una nueva.",
                    409,
                )
            proposal = dict(proposal)

        source_id = proposal["source_id"]
        upload = self.store.get_upload(source_id)
        if not upload or upload["source_sha256"] != proposal["source_sha256"]:
            raise PanelError("El vídeo de la propuesta ya no coincide con el original.", 409)
        style = VideoStyle(**proposal["style"])
        edit_options = dict(proposal["options"])
        render_backend = edit_options.get("renderer_backend", "modal")
        if render_backend != "modal":
            raise PanelError("Shotstack está deshabilitado. No se enviará ningún render por ese motor.", 409)
        if (
            edit_options["subtitle_mode"] != "none"
            and payload.get("authorize_openai_transcription") is not True
        ):
            raise PanelError("Falta autorizar el envío del audio a OpenAI para transcribirlo.", 403)

        render_mode = str(proposal["options"].get("render_mode") or "full")
        existing_status, existing_backend = self._source_render_state_for_mode(upload, render_mode)
        if existing_status in {"processing", "queued"}:
            raise PanelError("Este vídeo ya tiene un trabajo activo; no se iniciará otro render.", 409)
        if existing_status in {"completed", "failed"} and existing_backend == render_backend:
            raise PanelError(
                "Este motor ya tiene un resultado o un intento fallido para este vídeo. "
                "No se iniciará un duplicado ni un reintento automático.",
                409,
            )
        estimate = proposal["estimate"]
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
        needs_openai = edit_options["subtitle_mode"] != "none"
        if needs_openai and not health["openai_key_configured"]:
            raise PanelError("Falta OPENAI_API_KEY en Secrets; no se inició ningún trabajo.", 409)
        modal_status = health.get("modal") or {}
        if not modal_status.get("sdk_installed") or not modal_status.get("credentials_configured"):
            raise PanelError("Modal no está instalado o configurado en el servidor.", 409)
        if not (health.get("r2_storage") or {}).get("ready"):
            raise PanelError(
                "Cloudflare R2 no está configurado con los cuatro valores requeridos y el bucket esperado; "
                "no se inició ningún render.",
                409,
            )
        drive_import = (
            self.drive_store.get_import_for_local_upload(source_id)
            if self.drive_store
            else None
        )
        drive_status = self.drive_status()
        if drive_import and (
            not drive_status.get("ready") or not drive_status.get("authorized")
        ):
            raise PanelError(
                "El vídeo procede de Drive, pero la conexión no está activa. No se inició el procesamiento.",
                409,
            )
        if drive_import and not PipelineConfig.from_environment().processed_folder_id:
            raise PanelError("Selecciona la carpeta Procesados con Google Picker antes de procesar.", 409)

        with self.job_lock:
            current_proposal = self.pending_proposals.get(proposal_id)
            if (
                not current_proposal
                or current_proposal.get("used")
                or current_proposal.get("expires_at", 0) <= time.time()
            ):
                raise PanelError("La propuesta ha caducado o ya se utilizó. Prepara una nueva.", 409)
            if self.active_job_id:
                active = self.store.get_job(self.active_job_id)
                if active and active["status"] not in TERMINAL_STATUSES:
                    raise PanelError("Ya hay un vídeo en procesamiento.", 409)
            job_id = uuid.uuid4().hex
            self.store.create_job(job_id, source_id, estimate, edit_options)
            current_proposal["used"] = True
            self.active_job_id = job_id
        self.executor.submit(self._run_job, job_id, upload, estimate, style, edit_options)
        return self.store.get_job(job_id) or {}

    def retry_job(
        self,
        job_id: str,
        payload: Any,
        *,
        controlled_third_attempt: bool = False,
    ) -> dict[str, Any]:
        """Reserve an owner-authorized retry; the one-off third attempt is target-locked."""
        if not isinstance(payload, dict):
            raise PanelError("La solicitud de reintento debe ser un objeto JSON.")
        raise PanelError(
            "Los reintentos de render están deshabilitados en el motor Modal exclusivo; "
            "no se consultó ni se llamó a ningún proveedor.",
            409,
        )
        # The legacy implementation below is retained for stored attempt history,
        # but is intentionally unreachable from the application.
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise PanelError("Identificador de trabajo no válido.")
        retry_target = self.store.get_job(job_id)
        if retry_target:
            try:
                retry_options = json.loads(retry_target.get("edit_options_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                retry_options = {}
            if retry_options.get("renderer_backend") == "modal":
                raise PanelError(
                    "Los reintentos de Modal están deshabilitados. El estado remoto debe reconciliarse "
                    "antes de autorizar otro render.",
                    409,
                )
        expected_previous_attempt = 2 if controlled_third_attempt else 1
        if controlled_third_attempt:
            target = RENDER001_THIRD_RETRY_TARGET
            if (
                job_id != target["job_id"]
                or payload.get("confirm_final_third_attempt") is not True
            ):
                raise PanelError("La tercera prueba requiere autorización explícita y está limitada a RENDER-001.", 403)

        with self.job_lock:
            previous = self.store.get_job(job_id)
            if not previous:
                raise PanelError("No se encontró el intento fallido.", 404)
            if previous["status"] != "failed":
                raise PanelError("Solo se puede reintentar un intento que terminó con error.", 409)
            if int(previous.get("attempt_number") or 1) != expected_previous_attempt:
                raise PanelError("El intento anterior no coincide con el límite de reintentos autorizado.", 409)

            upload = self.store.get_upload(previous["source_id"])
            if not upload:
                raise PanelError("No se encontró el vídeo original.", 404)
            if controlled_third_attempt and (
                upload["id"] != target["source_id"]
                or upload["source_sha256"] != target["source_sha256"]
                or upload["original_filename"] != target["filename"]
                or previous.get("retry_of_job_id") != target["previous_job_id"]
            ):
                raise PanelError("El trabajo no coincide exactamente con el vídeo autorizado para esta prueba.", 403)
            latest = self.store.latest_job_for_source(upload["id"])
            if not latest or latest["id"] != job_id:
                raise PanelError("El intento seleccionado ya no es el más reciente.", 409)
            if self.active_job_id:
                active = self.store.get_job(self.active_job_id)
                if active and active["status"] not in TERMINAL_STATUSES:
                    raise PanelError("Hay otro trabajo en curso; no se enviará este reintento.", 409)

            drive_import = (
                self.drive_store.get_import_for_local_upload(upload["id"])
                if self.drive_store
                else None
            )
            if drive_import:
                drive_status = self.drive_status()
                if not drive_status.get("ready") or not drive_status.get("authorized"):
                    raise PanelError(
                        "El vídeo procede de Drive, pero la conexión no está activa; no se inició el reintento.",
                        409,
                    )
                if not self.drive_client:
                    raise PanelError("El cliente de Google Drive no está disponible.", 409)
                if not PipelineConfig.from_environment().processed_folder_id:
                    raise PanelError("No está configurada la carpeta de salida de Drive.", 409)
                if payload.get("confirm_drive_output_write") is not True:
                    raise PanelError(
                        "Confirma que se creará un MP4 nuevo en 02_PROCESADOS sin modificar el original.",
                        403,
                    )

            processing = self.store.ledger.get(upload["source_sha256"])
            if (
                processing.get("status") != "failed"
                or int(processing.get("attempts") or 0) != expected_previous_attempt
            ):
                raise PanelError(
                    "El estado local no confirma exactamente los intentos fallidos requeridos; se bloquea el reenvío.",
                    409,
                )
            history = self.store.job_history_for_source(upload["id"])
            if (
                not history
                or history[-1]["id"] != job_id
                or [int(attempt.get("attempt_number") or 1) for attempt in history]
                != list(range(1, expected_previous_attempt + 1))
            ):
                raise PanelError("El historial cambió; actualiza el panel antes de reintentar.", 409)
            if controlled_third_attempt and (
                len(history) != 2
                or history[0].get("id") != target["previous_job_id"]
                or history[1].get("id") != target["job_id"]
                or any(
                    attempt.get("status") != "failed"
                    or "HTTP 400" not in str(attempt.get("error") or "")
                    for attempt in history
                )
            ):
                raise PanelError("No se verifican los dos rechazos HTTP 400 previos de RENDER-001.", 409)

            saved = json.loads(processing.get("edit_options_json") or "{}")
            if not isinstance(saved, dict) or not isinstance(saved.get("style"), dict):
                raise PanelError("No se pudo recuperar la configuración exacta del primer intento.", 409)
            try:
                style = VideoStyle(**saved["style"])
            except (TypeError, ValueError) as exc:
                raise PanelError("La configuración guardada del primer intento no es válida.", 409) from exc
            subtitle_mode = saved.get("subtitle_mode")
            creative_plan = saved.get("creative_plan")
            if subtitle_mode not in {"none", "simple", "dynamic"} or not isinstance(creative_plan, dict):
                raise PanelError("Faltan opciones guardadas para reproducir la misma edición.", 409)
            if controlled_third_attempt and not _matches_render001_third_retry_configuration(
                style,
                subtitle_mode,
                creative_plan,
            ):
                raise PanelError("La configuración guardada no coincide con la edición aprobada para RENDER-001.", 409)

            original_options = previous.get("edit_options") or {}
            output_profile = next(
                (
                    profile for profile, dimensions in OUTPUT_PROFILES.items()
                    if dimensions == (style.output_width, style.output_height)
                ),
                None,
            )
            if (
                original_options.get("subtitle_mode") != subtitle_mode
                or original_options.get("output_profile") != output_profile
                or original_options.get("output_quality") != style.output_quality
                or int(original_options.get("output_fps") or 0) != style.output_fps
                or original_options.get("creative_plan") != creative_plan
            ):
                raise PanelError(
                    "Las opciones guardadas del trabajo no coinciden; no se cambiará ni reconstruirá la edición.",
                    409,
                )

            source_path = self._source_path(upload)
            try:
                with source_path.open("rb") as source_file:
                    source_hash = hashlib.file_digest(source_file, "sha256").hexdigest()
            except OSError:
                raise PanelError("No se puede leer el original; no se iniciará el reintento.", 409) from None
            if source_hash != upload["source_sha256"]:
                raise PanelError("El original cambió desde el primer intento; no se reintentará.", 409)

            try:
                config = PipelineConfig.from_environment()
            except ValueError as exc:
                raise PanelError("La configuración de Shotstack no está lista para Stage.", 409) from exc
            if config.shotstack_environment != "stage":
                raise PanelError("Los reintentos están bloqueados fuera de Shotstack Stage.", 409)
            health = self.health()
            if not health["shotstack_key_configured"]:
                raise PanelError("Falta la configuración de Shotstack; no se inició ningún trabajo.", 409)
            if subtitle_mode != "none" and not health["openai_key_configured"]:
                raise PanelError("Falta la configuración de OpenAI requerida por esta edición.", 409)

            estimate = previous.get("estimated_cost_usd")
            if estimate is None and payload.get("unknown_cost_acknowledged") is not True:
                raise PanelError("El coste es desconocido; confirma expresamente ese riesgo.", 403)
            if style.max_estimated_cost_usd is not None:
                if estimate is None or estimate > style.max_estimated_cost_usd:
                    raise PanelError("El reintento supera el límite de coste configurado.", 409)

            render_id = processing.get("render_id")
            resume_existing_render = False
            if render_id:
                try:
                    remote_status, _ = ShotstackClient(environment="stage").render_status(render_id)
                except ShotstackError as exc:
                    raise PanelError(
                        f"No se pudo verificar el estado remoto; no se envió un reintento: {exc}",
                        502,
                    ) from None
                if remote_status == "done":
                    resume_existing_render = True
                elif remote_status not in {"failed", "cancelled"}:
                    raise PanelError(
                        "Shotstack informa un render activo o un estado no concluyente; no se duplicará.",
                        409,
                    )
            else:
                previous_error = f"{previous.get('error') or ''} {processing.get('error') or ''}"
                if "HTTP 400" not in previous_error:
                    raise PanelError(
                        "No hay ID remoto ni rechazo HTTP 400 verificable; no es seguro reenviar.",
                        409,
                    )
                if payload.get("confirm_no_render_id") is not True:
                    raise PanelError(
                        "No existe ID de render para consultar a Shotstack. Confirma que aceptas esta limitación.",
                        403,
                    )

            retry_options = {
                "subtitle_mode": subtitle_mode,
                "output_profile": output_profile,
                "output_quality": style.output_quality,
                "output_fps": style.output_fps,
                "creative_plan": creative_plan,
            }
            retry_id = uuid.uuid4().hex
            retry = self.store.create_retry_job(
                job_id,
                retry_id,
                estimate,
                retry_options,
                attempt_number=expected_previous_attempt + 1,
            )
            self.active_job_id = retry_id

        try:
            self.executor.submit(
                self._run_job,
                retry_id,
                upload,
                estimate,
                style,
                retry_options,
                retry_failed=True,
                allow_third_attempt=controlled_third_attempt,
            )
        except RuntimeError:
            self.store.update_job(
                retry_id,
                status="failed",
                phase="failed",
                error="No se pudo iniciar el trabajador local; no se envió ninguna solicitud.",
            )
            with self.job_lock:
                if self.active_job_id == retry_id:
                    self.active_job_id = None
            raise PanelError("No se pudo iniciar el reintento local.", 503) from None
        retry["resumes_existing_render"] = resume_existing_render
        retry["controlled_third_attempt"] = controlled_third_attempt
        return retry

    def _run_job(
        self,
        job_id: str,
        upload: dict[str, Any],
        estimate: float | None,
        style: VideoStyle,
        edit_options: dict[str, Any],
        *,
        retry_failed: bool = False,
        allow_third_attempt: bool = False,
    ) -> None:
        drive_import = (
            self.drive_store.get_import_for_local_upload(upload["id"])
            if self.drive_store
            else None
        )
        modal_ledger: ProcessingLedger | None = None
        modal_ledger_claimed = False
        result: dict[str, Any] | None = None
        try:
            source_path = self._source_path(upload)
            def update_phase(phase: str) -> None:
                self.store.update_job(job_id, status="processing", phase=phase, progress=None)

            drive_uploader = None
            if drive_import:
                if not self.drive_client:
                    raise GoogleDriveError("La conexión de Google Drive no está disponible.")

                def upload_drive_output(path, folder_id, access_token, app_properties=None):
                    return self.drive_client.upload_new_file(
                        path,
                        folder_id=folder_id,
                        app_properties=app_properties,
                    )

                drive_uploader = upload_drive_output

            drive_source_key = (
                hashlib.sha256(drive_import["drive_file_id"].encode("utf-8")).hexdigest()
                if drive_import
                else None
            )
            if edit_options.get("renderer_backend", "modal") != "modal":
                raise PanelError("Shotstack está deshabilitado; no se envió ningún trabajo.")
            if retry_failed or allow_third_attempt:
                raise PanelError("Los reintentos de Modal están bloqueados para evitar renders duplicados.", 409)
            original_video = probe_video(source_path)
            update_phase("normalizing")
            with temporary_work_directory() as normalization_dir:
                normalized_video, normalization_report = prepare_video_for_modal(
                    original_video,
                    normalization_dir,
                )
                modal_ledger = ProcessingLedger(
                    self.store.database_path.with_name("modal_processing.sqlite3")
                )
                ledger_key = hashlib.sha256(
                    f"{upload['source_sha256']}:{edit_options.get('render_mode', 'full')}".encode("utf-8")
                ).hexdigest()
                modal_ledger.claim(
                    ledger_key,
                    str(source_path),
                    max_attempts=1,
                    edit_options=edit_options,
                )
                modal_ledger_claimed = True
                result = process_local_video_modal(
                    normalized_video.path,
                    self.output_dir,
                    job_id=job_id,
                    paid_api_authorized=True,
                    upload_to_drive=bool(drive_import),
                    drive_uploader=drive_uploader,
                    drive_source_key=drive_source_key,
                    config=(
                        PipelineConfig.from_environment()
                        if drive_import
                        else PipelineConfig()
                    ),
                    style=style,
                    subtitle_mode=edit_options["subtitle_mode"],
                    render_mode=edit_options.get("render_mode", "full"),
                    render_settings=edit_options.get("render_settings"),
                    creative_plan=edit_options.get("creative_plan"),
                    briefing=edit_options.get("style_notes", ""),
                    progress_callback=update_phase,
                )
                update_phase("uploading_to_r2")
                result["r2_storage"] = upload_mp4_to_r2(result["output"], job_id)
                result["source"] = str(source_path)
                result["duration_seconds"] = original_video.duration
                result["source_dimensions"] = {
                    "width": original_video.width,
                    "height": original_video.height,
                }
                result["source_video_codec"] = original_video.video_codec
                result["source_audio_codec"] = original_video.audio_codec
                result["source_fps"] = original_video.fps
                result["normalized_input"] = {
                    "width": normalized_video.width,
                    "height": normalized_video.height,
                    "video_codec": normalized_video.video_codec,
                    "audio_codec": normalized_video.audio_codec,
                    "fps": normalized_video.fps,
                    "pixel_format": normalized_video.pixel_format,
                    "color_range": normalized_video.color_range,
                    "color_primaries": normalized_video.color_primaries,
                    "color_transfer": normalized_video.color_transfer,
                    "color_space": normalized_video.color_space,
                }
                result["normalization"] = normalization_report
                result.setdefault("conversion", {})["input_normalization"] = normalization_report
                result["source_modified"] = False
                modal_ledger.mark_completed(ledger_key, result["output"])
            if drive_import and self.drive_store:
                output = result.get("drive_output") or {}
                output_file_id = output.get("id") if isinstance(output, dict) else None
                self.drive_store.update_import(
                    drive_import["drive_file_id"],
                    status="uploaded",
                    error=None,
                    output_drive_file_id=output_file_id,
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
            if modal_ledger is not None and modal_ledger_claimed:
                try:
                    modal_ledger.mark_failed(
                        hashlib.sha256(
                            f"{upload['source_sha256']}:{edit_options.get('render_mode', 'full')}".encode("utf-8")
                        ).hexdigest(),
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    logger.exception("Could not persist Modal failure state for job %s", job_id)
            if (
                drive_import
                and self.drive_store
                and isinstance(exc, (GoogleDriveError, DriveUploadError))
            ):
                self.drive_store.update_import(
                    drive_import["drive_file_id"],
                    status="upload_failed",
                    error=str(exc)[:300],
                )
            self.store.update_job(
                job_id,
                status="failed",
                phase="failed",
                progress=None,
                error=str(exc)[:1600],
                estimated_cost_usd=estimate,
                output_id=(
                    Path(result["output"]).name
                    if result and result.get("output") and Path(result["output"]).is_file()
                    else None
                ),
                result_json=(
                    json.dumps(result, ensure_ascii=False)
                    if result and result.get("output") and Path(result["output"]).is_file()
                    else None
                ),
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
        self.auth = OwnerAuth(panel.store.database_path)


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
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _json(
        self,
        payload: Any,
        status: int = 200,
        head: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _cookies(self) -> SimpleCookie:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return SimpleCookie()
        return cookies

    def _cookie_value(self, name: str) -> str | None:
        morsel = self._cookies().get(name)
        return morsel.value if morsel else None

    def _cookie_attributes(self, max_age: int) -> str:
        attributes = f"Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        if self.server.auth.cookie_secure:
            attributes += "; Secure"
        return attributes

    def _send_login_page(
        self,
        *,
        status: int = 200,
        message: str = "",
        head: bool = False,
    ) -> None:
        configured = self.server.auth.configured
        challenge = self.server.auth.issue_login_challenge() if configured else ""
        setup_message = (
            ""
            if configured
            else (
                "<p class=\"notice\">El acceso aún no está configurado. "
                "Añade <code>PANEL_OWNER_PASSWORD</code> en Replit Secrets y "
                "comprueba que <code>SESSION_SECRET</code> esté configurado.</p>"
            )
        )
        error_message = (
            f'<p class="error" role="alert">{html.escape(message)}</p>' if message else ""
        )
        disabled = "" if configured else " disabled"
        body = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Acceso · MATRIX MOTION</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#f4f7f8;color:#18303a;font:15px system-ui,sans-serif;padding:24px}}
main{{width:min(100%,420px);background:#fff;border:1px solid #dce5e8;border-radius:20px;
padding:34px;box-shadow:0 18px 50px #18303a12}} .brand{{font-size:18px;font-weight:800;
letter-spacing:-.04em}} .sub{{margin:5px 0 28px;color:#6e8188;font-size:11px;
letter-spacing:.12em}} h1{{font-size:25px;margin:0 0 8px}} p{{line-height:1.55;color:#49616a}}
label{{display:block;margin:22px 0 8px;font-weight:700}} input{{width:100%;padding:13px;
border:1px solid #c6d4d9;border-radius:10px;font:inherit}} button{{width:100%;margin-top:18px;
padding:13px;border:0;border-radius:10px;background:#102d3a;color:#fff;font:inherit;
font-weight:750}} button:disabled{{opacity:.5;cursor:not-allowed}} .error{{color:#a6464a}}
.notice{{padding:12px;background:#edf3f5;border-radius:10px;font-size:13px}}
code{{font-family:ui-monospace,monospace;font-size:.92em}}
</style></head><body><main>
<div class="brand">MATRIX MOTION</div><div class="sub">VIDEO AUTOMATION STUDIO</div>
<h1>Acceso al panel</h1><p>Inicia sesión para abrir tu espacio de trabajo.</p>
{setup_message}{error_message}
<form method="post" action="/login" autocomplete="on">
<input type="hidden" name="csrf_token" value="{html.escape(challenge, quote=True)}">
<label for="password">Contraseña</label>
<input id="password" name="password" type="password" autocomplete="current-password"
required minlength="16" {disabled}>
<button type="submit"{disabled}>Iniciar sesión</button>
</form></main></body></html>""".encode("utf-8")
        self.send_response(status)
        self._headers("text/html; charset=utf-8", len(body))
        if configured:
            self.send_header(
                "Set-Cookie",
                f"{LOGIN_CSRF_COOKIE}={challenge}; {self._cookie_attributes(300)}",
            )
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_with_cookie(self, location: str, cookie_value: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cookie_value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_google_oauth_callback(self) -> None:
        if self.command not in {"GET", "HEAD"}:
            raise PanelError("Método no permitido.", 405)
        oauth = self.server.panel.drive_oauth
        if not oauth:
            raise PanelError("El almacenamiento seguro de Drive no está disponible.", 503)
        params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        state = (params.get("state") or [""])[0]
        state_cookie = self._cookie_value(GOOGLE_OAUTH_STATE_COOKIE) or ""
        oauth_error = (params.get("error") or [""])[0]
        try:
            if oauth_error:
                oauth.store.consume_oauth_state(state, state_cookie)
            else:
                code = (params.get("code") or [""])[0]
                oauth.complete_authorization(code, state, state_cookie)
        except GoogleDriveError as exc:
            logger.warning("Google OAuth callback rejected (%s)", type(exc).__name__)
            raise PanelError(str(exc), 400) from exc
        secure = "; Secure" if self.server.auth.cookie_secure else ""
        clear_cookie = (
            f"{GOOGLE_OAUTH_STATE_COOKIE}=; Path=/oauth/google/callback; "
            f"Max-Age=0; HttpOnly; SameSite=Lax{secure}"
        )
        self._redirect_with_cookie("/", clear_cookie)

    def _handle_google_oauth_start(self) -> None:
        oauth = self.server.panel.drive_oauth
        if not oauth:
            raise PanelError("El almacenamiento seguro de Drive no está disponible.", 503)
        try:
            authorization_url, state = oauth.authorization_url()
        except GoogleDriveError as exc:
            raise PanelError(str(exc), 409) from exc
        secure = "; Secure" if self.server.auth.cookie_secure else ""
        state_cookie = (
            f"{GOOGLE_OAUTH_STATE_COOKIE}={state}; Path=/oauth/google/callback; "
            f"Max-Age=600; HttpOnly; SameSite=Lax{secure}"
        )
        self._json(
            {"authorization_url": authorization_url},
            extra_headers={"Set-Cookie": state_cookie},
        )

    def _handle_login(self) -> None:
        if not self.server.auth.configured:
            self._send_login_page(status=503)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self.close_connection = True
            self._send_login_page(status=400, message="Solicitud de acceso no válida.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                self.close_connection = True
                self._send_login_page(status=400, message="Solicitud de acceso no válida.")
                return
            form = parse_qs(
                self.rfile.read(length).decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=4,
            )
        except (ValueError, UnicodeDecodeError):
            self._send_login_page(status=400, message="Solicitud de acceso no válida.")
            return

        try:
            session_token = self.server.auth.login(
                password=(form.get("password") or [""])[0],
                challenge_cookie=self._cookie_value(LOGIN_CSRF_COOKIE) or "",
                challenge_form=(form.get("csrf_token") or [""])[0],
                client_id=str(self.client_address[0]),
            )
        except AuthError as exc:
            self._send_login_page(status=exc.status_code, message=str(exc))
            return

        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={session_token}; {self._cookie_attributes(12 * 60 * 60)}",
        )
        self.send_header(
            "Set-Cookie",
            f"{LOGIN_CSRF_COOKIE}=; {self._cookie_attributes(0)}",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_unauthorized(self, path: str, head: bool = False) -> None:
        if path.startswith(("/api/", "/media/")) or self.command not in {"GET", "HEAD"}:
            self._json({"detail": "Inicia sesión para continuar."}, 401, head)
            if self.command in {"POST", "PUT", "PATCH", "DELETE"}:
                self.close_connection = True
            return
        self._send_redirect("/login")

    def _require_csrf(self, session) -> None:
        if not self.server.auth.validate_csrf(
            session,
            self.headers.get("X-CSRF-Token"),
        ):
            raise PanelError("La solicitud no superó la comprobación de seguridad (CSRF).", 403)

    def _handle_logout(self, session) -> None:
        self._require_csrf(session)
        self.server.auth.logout(self._cookie_value(SESSION_COOKIE))
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; {self._cookie_attributes(0)}",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")

        def hostname(value: str) -> str | None:
            if not value or value != value.strip() or "," in value or "*" in value:
                return None
            try:
                parsed_host = urlsplit(f"//{value}")
                if (
                    not parsed_host.hostname
                    or parsed_host.username
                    or parsed_host.password
                    or parsed_host.path
                    or parsed_host.query
                    or parsed_host.fragment
                ):
                    return None
                parsed_host.port  # Validate that a supplied port is numeric and in range.
            except ValueError:
                return None
            return parsed_host.hostname.rstrip(".").lower()

        local_hosts = {"localhost", "127.0.0.1", "::1"}
        proxy_hosts = local_hosts | {"replit.com"}
        app_hosts = set()
        for variable in ("REPLIT_DEV_DOMAIN", "REPLIT_DOMAINS"):
            for configured in os.getenv(variable, "").split(","):
                configured = configured.strip()
                if not configured:
                    continue
                if "://" in configured:
                    configured = urlsplit(configured).netloc
                configured_host = hostname(configured)
                if configured_host:
                    app_hosts.add(configured_host)
        allowed_hosts = local_hosts | app_hosts

        request_host = hostname(self.headers.get("Host", ""))
        forwarded_header = self.headers.get("X-Forwarded-Host", "")
        forwarded_host = hostname(forwarded_header) if forwarded_header else None
        forwarded_proto_header = self.headers.get("X-Forwarded-Proto", "")
        forwarded_proto = forwarded_proto_header.strip().lower()
        try:
            parsed_origin = urlsplit(origin) if origin else None
            origin_host = hostname(parsed_origin.netloc) if parsed_origin else None
            origin_port = parsed_origin.port if parsed_origin else None
        except ValueError:
            parsed_origin = None
            origin_host = None
            origin_port = None

        if origin is None:
            origin_label = "missing"
        elif origin == "null":
            origin_label = "null"
        elif parsed_origin and origin_host:
            origin_label = f"{parsed_origin.scheme.lower()}://{origin_host}"
        else:
            origin_label = "malformed"

        def reject(check: str) -> None:
            logger.warning(
                "Origin/Host rejected check=%s Host=%s Origin=%s "
                "X-Forwarded-Host=%s X-Forwarded-Proto=%s",
                check,
                request_host or "invalid",
                origin_label,
                forwarded_host or ("invalid" if forwarded_header else "missing"),
                forwarded_proto if forwarded_proto in {"http", "https"} else (
                    "invalid" if forwarded_proto_header else "missing"
                ),
            )
            raise PanelError("Solicitud rechazada: origen no permitido.", 403)

        if not request_host:
            reject("invalid_host")
        if forwarded_proto and (
            "," in forwarded_proto or forwarded_proto not in {"http", "https"}
        ):
            reject("invalid_forwarded_proto")
        if forwarded_header and forwarded_host not in app_hosts:
            reject("forwarded_host_not_allowed")

        forwarded_proxy_host = (
            request_host in proxy_hosts and forwarded_host in app_hosts
        )
        if request_host not in allowed_hosts and not forwarded_proxy_host:
            reject("host_not_allowed")

        effective_host = request_host
        if forwarded_host:
            if request_host in proxy_hosts:
                effective_host = forwarded_host
            elif forwarded_host != request_host:
                reject("forwarded_host_mismatch")

        if not origin:
            return

        if not parsed_origin or not origin_host:
            if origin == "null":
                null_origin_login = (
                    self.command == "POST"
                    and urlsplit(self.path).path == "/login"
                    and request_host in app_hosts
                    and effective_host in app_hosts
                    and forwarded_proto == "https"
                    and (not forwarded_header or forwarded_host == request_host)
                )
                if null_origin_login:
                    return
                reject("opaque_origin_not_allowed")
            reject("invalid_origin")
        origin_scheme = parsed_origin.scheme.lower()
        if origin_host == "replit.com":
            # The editor's embedded Preview is cross-origin. Trust that exact
            # editor origin only when Replit forwards this app's exact
            # development hostname over HTTPS.
            if (
                origin_scheme == "https"
                and origin_port in {None, 443}
                and effective_host in app_hosts
                and forwarded_host in app_hosts
                and forwarded_proto == "https"
            ):
                return
            reject("editor_origin_without_matching_preview_proxy")
        if (
            origin_scheme not in {"http", "https"}
            or parsed_origin.username
            or parsed_origin.password
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or not origin_host
            or origin_host not in allowed_hosts
            or origin_host != effective_host
            or (origin_host not in local_hosts and origin_scheme != "https")
            or (origin_host not in local_hosts and origin_port not in {None, 443})
            or (
                origin_host not in local_hosts
                and forwarded_proto
                and forwarded_proto != origin_scheme
            )
        ):
            reject("origin_not_matching_allowed_host")

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
        allowed = {"app.js", "upload-controller.js", "premium.css"}
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
        safe_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "video"
        encoded_name = quote(filename, safe="")
        self.send_header(
            "Content-Disposition",
            f'inline; filename="{safe_name}"; filename*=UTF-8\'\'{encoded_name}',
        )
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
            self._check_origin()
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                if self.command in {"GET", "HEAD"}:
                    self.send_response(204)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raise PanelError("Método no permitido.", 405)
            if path == "/oauth/google/callback":
                self._handle_google_oauth_callback()
                return
            if path == "/login":
                if self.command in {"GET", "HEAD"}:
                    session = self.server.auth.get_session(
                        self._cookie_value(SESSION_COOKIE)
                    )
                    if session:
                        self._send_redirect("/")
                    else:
                        self._send_login_page(head=head)
                    return
                if self.command == "POST":
                    self._handle_login()
                    return
                raise PanelError("Método no permitido.", 405)

            session = self.server.auth.get_session(self._cookie_value(SESSION_COOKIE))
            if not session:
                self._send_unauthorized(path, head)
                return

            if self.command in {"POST", "PUT"}:
                self._require_csrf(session)
                if self.command == "POST" and path == "/logout":
                    self._handle_logout(session)
                    return
                if path == "/logout":
                    raise PanelError("Método no permitido.", 405)
            elif path == "/logout":
                raise PanelError("Método no permitido.", 405)

            if self.command in {"GET", "HEAD"}:
                if path == "/":
                    self._send_static("index.html", head)
                elif path in {"/app.js", "/upload-controller.js", "/premium.css"}:
                    self._send_static(path.lstrip("/"), head)
                elif path == "/api/session":
                    self._json({"csrf_token": session.csrf_token}, head=head)
                elif path == "/api/health":
                    self._json(self.server.panel.health(), head=head)
                elif path == "/api/drive/status":
                    self._json(self.server.panel.drive_status(), head=head)
                elif path == "/api/drive/config":
                    self._json(self.server.panel.drive_config(), head=head)
                elif path == "/api/library":
                    self._json({"items": self.server.panel.library()}, head=head)
                elif path == "/api/style":
                    self._json(self.server.panel.style(), head=head)
                elif path.startswith("/api/jobs/"):
                    job_id = path.rsplit("/", 1)[-1]
                    self._json(self.server.panel.get_job(job_id), head=head)
                elif path == "/media/q03c-sample":
                    media = self.server.panel.q03c_sample_path
                    if not media.is_file():
                        raise PanelError("No se encontró la muestra Q-03C.", 404)
                    self._send_media(media, media.name, head)
                elif path == "/media/q03c-comparison":
                    media = self.server.panel.q03c_comparison_path
                    if not media.is_file():
                        raise PanelError("No se encontró la comparación visual Q-03C.", 404)
                    self._send_media(media, media.name, head)
                elif path.startswith("/media/q03d/"):
                    media_files = {
                        "/media/q03d/summary": self.server.panel.q03d_summary_path,
                        "/media/q03d/candidates-8s": self.server.panel.q03d_candidates_8s_path,
                        "/media/q03d/candidates-25s": self.server.panel.q03d_candidates_25s_path,
                        "/media/q03d/candidates-49s": self.server.panel.q03d_candidates_49s_path,
                    }
                    media = media_files.get(path)
                    if media is None or not media.is_file():
                        raise PanelError("No se encontró la comparación visual Q-03D.", 404)
                    self._send_media(media, media.name, head)
                elif path == "/media/q03e-sample":
                    media = self.server.panel.q03e_sample_path
                    if not media.is_file():
                        raise PanelError("No se encontró la muestra Q-03E.", 404)
                    self._send_media(media, media.name, head)
                elif path == "/media/q03e-comparison":
                    media = self.server.panel.q03e_comparison_path
                    if not media.is_file():
                        raise PanelError("No se encontró la comparación visual Q-03E.", 404)
                    self._send_media(media, media.name, head)
                elif path.startswith("/media/incoming/"):
                    file_id = path.rsplit("/", 1)[-1]
                    media, name = self.server.panel.source_path(file_id)
                    self._send_media(media, name, head)
                elif path.startswith("/media/outputs/"):
                    output_id = unquote(path.rsplit("/", 1)[-1])
                    media, name = self.server.panel.output_path(output_id)
                    self._send_media(media, name, head)
                elif path.startswith("/media/thumbnails/"):
                    thumbnail_id = unquote(path.rsplit("/", 1)[-1])
                    if not re.fullmatch(r"[0-9a-f]{32}", thumbnail_id):
                        raise PanelError("Identificador de miniatura no válido.", 404)
                    media = self.server.panel.thumbnail_dir / f"{thumbnail_id}.jpg"
                    if not media.is_file() or media.resolve().parent != self.server.panel.thumbnail_dir:
                        raise PanelError("No se encontró la miniatura.", 404)
                    self._send_media(media, f"{thumbnail_id}.jpg", head)
                else:
                    raise PanelError("Ruta no encontrada.", 404)
            elif self.command == "POST" and path == "/api/upload":
                name = unquote(self.headers.get("X-Upload-Filename", ""))
                if not name:
                    raise PanelError("Falta el nombre del archivo.")
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    style_notes = unquote(
                        self.headers.get("X-Style-Notes", ""),
                        errors="strict",
                    )
                except UnicodeDecodeError:
                    raise PanelError("Las notas de estilo no contienen texto UTF-8 válido.") from None
                item = self.server.panel.upload(
                    name,
                    length,
                    self.rfile,
                    niche=self.headers.get("X-Video-Niche", DEFAULT_VIDEO_NICHE),
                    style_notes=style_notes,
                    editing_style=self.headers.get("X-Editing-Style", "dynamic"),
                )
                self._json({"item": item}, 201)
            elif self.command == "POST" and path == "/api/proposals":
                self._json(self.server.panel.prepare_proposal(self._read_json()), 200)
            elif self.command == "POST" and path == "/api/jobs":
                self._json(self.server.panel.start_job(self._read_json()), 202)
            elif (
                self.command == "POST"
                and re.fullmatch(r"/api/jobs/[0-9a-f]{32}/retry-third", path)
            ):
                failed_job_id = path.split("/")[3]
                self._json(
                    self.server.panel.retry_job(
                        failed_job_id,
                        self._read_json(),
                        controlled_third_attempt=True,
                    ),
                    202,
                )
            elif (
                self.command == "POST"
                and re.fullmatch(r"/api/jobs/[0-9a-f]{32}/retry", path)
            ):
                failed_job_id = path.split("/")[3]
                self._json(
                    self.server.panel.retry_job(failed_job_id, self._read_json()),
                    202,
                )
            elif self.command == "POST" and path == "/api/drive/oauth/start":
                self._handle_google_oauth_start()
            elif self.command == "POST" and path == "/api/drive/scan":
                self._json(self.server.panel.scan_drive(), 200)
            elif self.command == "POST" and path == "/api/drive/test-write":
                payload = self._read_json()
                if not isinstance(payload, dict) or payload.get("confirm") is not True:
                    raise PanelError(
                        "Confirma la creación y eliminación de un MP4 temporal para probar 02_PROCESADOS.",
                        400,
                    )
                self._json(self.server.panel.test_drive_output_write(payload), 200)
            elif self.command == "POST" and path == "/api/drive/retry-upload":
                payload = self._read_json()
                source_id = str(payload.get("source_id") or "") if isinstance(payload, dict) else ""
                self._json(self.server.panel.retry_drive_upload(source_id), 200)
            elif self.command == "POST" and path == "/api/drive/folders":
                self._json(self.server.panel.select_drive_folder(self._read_json()))
            elif self.command == "POST" and path == "/api/drive/files/select":
                self._json(self.server.panel.select_drive_files(self._read_json()))
            elif self.command == "POST" and path == "/api/drive/list":
                self._json(self.server.panel.list_drive_files(self._read_json()))
            elif self.command == "POST" and path == "/api/drive/sync":
                self._json(self.server.panel.sync_drive(self._read_json()))
            elif self.command == "POST" and path == "/api/drive/test-file":
                self._json(self.server.panel.create_drive_test_file(self._read_json()), 201)
            elif self.command == "PUT" and path == "/api/style":
                self._json(self.server.panel.save_style(self._read_json()))
            else:
                raise PanelError("Ruta o método no permitido.", 404)
        except PanelError as exc:
            if self.command in {"POST", "PUT"}:
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
    panel.drive_auto_scanner.start()
    server = VideoPanelServer((host, port), VideoPanelHandler, panel)
    logger.info("Local video panel listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping local video panel")
    finally:
        server.server_close()
        panel.drive_auto_scanner.stop()
        panel.executor.shutdown(wait=False, cancel_futures=True)
