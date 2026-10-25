"""Authenticated MATRIX MOTION server-side client for the Modal Remotion worker."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable

from .style import StyleConfigError, validate_output_profile

logger = logging.getLogger(__name__)
MODAL_APP_NAME = "matrix-motion-remotion"
MODAL_FUNCTION_NAME = "render_master"
MODAL_VOLUME_NAME = "matrix-motion-render-staging"
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_OUTPUT_BYTES = 4 * 1024**3
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def modal_configuration_status() -> dict[str, Any]:
    """Report only booleans and public deployment names; never read credential contents."""
    sdk_installed = importlib.util.find_spec("modal") is not None
    profile_present = (Path.home() / ".modal.toml").is_file()
    env_credentials_present = bool(
        os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET")
    )
    return {
        "sdk_installed": sdk_installed,
        "credentials_configured": profile_present or env_credentials_present,
        "app_name": MODAL_APP_NAME,
        "function_name": MODAL_FUNCTION_NAME,
        "deployment_status": "not_checked",
    }


def _safe_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ModalRenderResult:
    output_path: Path
    duration_seconds: float
    size_bytes: int
    sha256: str
    width: int
    height: int
    fps: int
    elapsed_seconds: float
    remote_job_id: str
    temporary_storage_cleaned: bool


class ModalRenderClient:
    """Uploads per-job inputs to a Modal Volume and downloads the result locally."""

    def __init__(self, modal_sdk: Any | None = None):
        self._modal = modal_sdk
        self._worker = None

    def _sdk(self) -> Any:
        if self._modal is None:
            try:
                self._modal = importlib.import_module("modal")
            except ImportError as exc:
                raise RuntimeError(
                    "La librería Modal no está instalada en el entorno del panel."
                ) from exc
        return self._modal

    def ensure_worker_available(self) -> None:
        """Resolve the deployed function before any transcription or source upload."""
        if self._worker is not None:
            return
        config = modal_configuration_status()
        if not config["sdk_installed"] or not config["credentials_configured"]:
            raise RuntimeError("Modal no está listo en el servidor; no se contactó al worker.")
        try:
            self._worker = self._sdk().Function.from_name(
                MODAL_APP_NAME,
                MODAL_FUNCTION_NAME,
            )
        except Exception as exc:
            logger.warning(
                "modal_worker_lookup_failed error_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError(
                "El worker de Modal no está desplegado o no es accesible; "
                "no se subió el vídeo ni se inició una transcripción."
            ) from None

    def render(
        self,
        *,
        job_id: str,
        input_path: str | Path,
        transcript: dict[str, Any],
        plan: dict[str, Any],
        effects_dir: str | Path,
        output_path: str | Path,
        show_subtitles: bool,
        briefing: str = "",
        caption_style: dict[str, Any] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ModalRenderResult:
        if not JOB_ID_PATTERN.fullmatch(job_id or ""):
            raise ValueError("Identificador de trabajo Modal no válido.")
        source = Path(input_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        effects = Path(effects_dir).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("El vídeo fuente no existe o está vacío.")
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("El vídeo supera el límite de subida configurado para Modal (2 GiB).")
        if source.suffix.lower() not in {".mov", ".mp4"}:
            raise ValueError("Modal solo admite archivos .MOV o .MP4.")
        if destination.exists():
            raise FileExistsError("Se rechaza sobrescribir un MP4 de salida existente.")
        if not isinstance(transcript, dict) or not isinstance(plan, dict):
            raise ValueError("La transcripción y el plan deben ser objetos JSON.")
        try:
            expected_width, expected_height, expected_fps = validate_output_profile(
                plan.get("output_width"),
                plan.get("output_height"),
                plan.get("output_fps"),
            )
        except StyleConfigError as exc:
            raise ValueError(str(exc)) from None
        if not isinstance(show_subtitles, bool):
            raise ValueError("La opción de subtítulos debe ser booleana.")
        if not isinstance(briefing, str) or len(briefing) > 4000:
            raise ValueError("Las notas de edición superan el límite de 4.000 caracteres.")

        config = modal_configuration_status()
        if not config["sdk_installed"] or not config["credentials_configured"]:
            raise RuntimeError(
                "Modal no está listo en el servidor. Comprueba la instalación y el perfil de Modal; "
                "no se inició ningún render."
            )

        self.ensure_worker_available()
        sdk = self._sdk()
        volume = sdk.Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=True)
        remote_prefix = f"jobs/{job_id}"
        remote_input = f"/{remote_prefix}/source{source.suffix.lower()}"
        started = time.monotonic()
        if progress_callback:
            progress_callback("uploading_to_modal")

        with tempfile.TemporaryDirectory(prefix="matrix-motion-modal-stage-") as staging:
            staging_dir = Path(staging)
            transcript_file = staging_dir / "transcript.json"
            plan_file = staging_dir / "plan.json"
            manifest_file = staging_dir / "manifest.json"
            transcript_file.write_text(
                json.dumps(transcript, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            plan_file.write_text(
                json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            manifest_file.write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "source_name": source.name,
                        "show_subtitles": show_subtitles,
                        "briefing": briefing,
                        "caption_style": caption_style or {},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            with volume.batch_upload() as batch:
                batch.put_file(str(source), remote_input)
                batch.put_file(str(transcript_file), f"/{remote_prefix}/transcript.json")
                batch.put_file(str(plan_file), f"/{remote_prefix}/plan.json")
                batch.put_file(str(manifest_file), f"/{remote_prefix}/manifest.json")
                if effects.is_dir():
                    for asset in sorted(effects.iterdir()):
                        if (
                            asset.is_file()
                            and asset.suffix.lower() in {".svg", ".png", ".jpg", ".jpeg", ".webp"}
                            and asset.stat().st_size <= 25 * 1024 * 1024
                        ):
                            batch.put_file(
                                str(asset),
                                f"/{remote_prefix}/effects/{asset.name}",
                            )

        if progress_callback:
            progress_callback("rendering_on_modal")
        result = self._worker.remote(job_id)
        if not isinstance(result, dict) or result.get("job_id") != job_id:
            raise RuntimeError("Modal devolvió una respuesta de trabajo no reconocida.")
        remote_output = result.get("output_path")
        if remote_output != remote_prefix + "/output.mp4":
            raise RuntimeError("Modal devolvió una ruta de resultado no válida.")
        try:
            expected_size = int(result.get("output_size_bytes", 0))
        except (TypeError, ValueError):
            expected_size = 0
        expected_hash = result.get("output_sha256")
        if expected_size <= 0 or expected_size > MAX_OUTPUT_BYTES:
            raise RuntimeError("Modal devolvió un tamaño de MP4 fuera de los límites permitidos.")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise RuntimeError("Modal devolvió un checksum no válido para el MP4.")
        try:
            actual_width = int(result.get("width") or 0)
            actual_height = int(result.get("height") or 0)
            actual_fps = int(result.get("fps") or 0)
        except (TypeError, ValueError):
            actual_width = actual_height = actual_fps = 0
        if (actual_width, actual_height, actual_fps) != (
            expected_width,
            expected_height,
            expected_fps,
        ):
            raise RuntimeError(
                "La respuesta del motor no coincide con la resolución y los FPS aprobados."
            )

        if progress_callback:
            progress_callback("downloading_from_modal")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as output:
                for chunk in volume.read_file(remote_output):
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise RuntimeError("Modal envió un bloque de salida no binario.")
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        raise RuntimeError("La descarga superó el límite de 4 GiB.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise RuntimeError("El MP4 descargado no coincide con el tamaño o checksum del render remoto.")
            os.replace(partial, destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        temporary_storage_cleaned = True
        try:
            volume.remove_file(remote_prefix, recursive=True)
        except Exception as exc:  # Cleanup failure must not invalidate a verified local MP4.
            temporary_storage_cleaned = False
            logger.warning(
                "modal_temp_cleanup_failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )

        return ModalRenderResult(
            output_path=destination,
            duration_seconds=float(result.get("duration_seconds") or 0),
            size_bytes=size,
            sha256=digest.hexdigest(),
            width=int(result.get("width") or 0),
            height=int(result.get("height") or 0),
            fps=int(result.get("fps") or 0),
            elapsed_seconds=time.monotonic() - started,
            remote_job_id=job_id,
            temporary_storage_cleaned=temporary_storage_cleaned,
        )
