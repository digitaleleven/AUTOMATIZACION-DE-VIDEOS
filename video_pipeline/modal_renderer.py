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
NORMALIZATION_PROFILE_ID = "matrix-sdr-bt709-1080x1920-30-crf12-v1"
MASTER_4K_NORMALIZATION_PROFILE_ID = "matrix-sdr-bt709-2160x3840-30-crf17-v1"
NORMALIZATION_PROFILE_SPECS = {
    NORMALIZATION_PROFILE_ID: {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "max_bytes": 2 * 1024**3,
        "cli_flag": "--master-proxy",
    },
    MASTER_4K_NORMALIZATION_PROFILE_ID: {
        "width": 2160,
        "height": 3840,
        "fps": 30,
        "max_bytes": 8 * 1024**3,
        "cli_flag": "--master-4k-proxy",
    },
}
MAX_NORMALIZED_PROXY_BYTES = 8 * 1024**3
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


@dataclass(frozen=True)
class ModalNormalizationResult:
    output_path: Path
    duration_seconds: float
    size_bytes: int
    sha256: str
    source_sha256: str
    profile_id: str
    remote_proxy_path: str
    cache_hit: bool
    remote_processing_seconds: float | None
    elapsed_seconds: float
    profile: dict[str, Any]
    capabilities: dict[str, Any] | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "source_sha256": self.source_sha256,
            "profile_id": self.profile_id,
            "cache_hit": self.cache_hit,
            "remote_processing_seconds": 0.0 if self.cache_hit else self.remote_processing_seconds,
            "cached_conversion_seconds": (
                self.remote_processing_seconds if self.cache_hit else None
            ),
            "transfer_and_validation_seconds": round(
                max(0.0, self.elapsed_seconds - float(self.remote_processing_seconds or 0)),
                3,
            ),
            "profile": self.profile,
            "capabilities": self.capabilities,
        }


@dataclass(frozen=True)
class ModalNormalizationSubmission:
    job_id: str
    function_call_id: str
    source_sha256: str
    profile_id: str
    submitted_at: float


class ModalSubmissionUnknownError(RuntimeError):
    """The worker may have accepted a call whose ID was not durably recorded."""

    def __init__(self, message: str, function_call_id: str | None = None):
        super().__init__(message)
        self.function_call_id = function_call_id


class ModalRenderClient:
    """Uploads per-job inputs to a Modal Volume and downloads the result locally."""

    def __init__(self, modal_sdk: Any | None = None):
        self._modal = modal_sdk
        self._worker = None
        self._normalized_jobs: dict[str, dict[str, Any]] = {}
        self._staged_sources: dict[str, str] = {}
        self._staged_jobs: set[str] = set()
        self._render_started_jobs: set[str] = set()

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

    @staticmethod
    def _read_volume_small(volume: Any, remote_path: str, *, maximum_bytes: int = 64 * 1024) -> bytes | None:
        try:
            chunks = volume.read_file(remote_path)
            payload = bytearray()
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise RuntimeError("Modal devolvió metadatos de caché no binarios.")
                payload.extend(chunk)
                if len(payload) > maximum_bytes:
                    raise RuntimeError("El manifiesto remoto de normalización supera el límite permitido.")
            return bytes(payload)
        except FileNotFoundError:
            return None

    @staticmethod
    def _validate_normalized_profile(
        profile: Any,
        profile_id: str = NORMALIZATION_PROFILE_ID,
    ) -> dict[str, Any]:
        spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
        if spec is None:
            raise RuntimeError("Modal devolvió un perfil de normalización no admitido.")
        if not isinstance(profile, dict):
            raise RuntimeError("Modal no devolvió metadatos FFprobe del proxy.")
        expected = {
            "width": spec["width"],
            "height": spec["height"],
            "codec": "h264",
            "pixel_format": "yuv420p",
            "color_range": "tv",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        }
        if any(profile.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Los metadatos del proxy Modal no coinciden con el perfil SDR aprobado.")
        try:
            fps = float(profile.get("fps"))
            duration = float(profile.get("duration_seconds"))
        except (TypeError, ValueError):
            raise RuntimeError("Modal devolvió metadatos de cadencia o duración no válidos.") from None
        if abs(fps - spec["fps"]) > 0.05 or duration <= 0:
            raise RuntimeError("El proxy Modal no conserva los FPS del perfil o tiene duración inválida.")
        return profile

    def normalize_input(
        self,
        *,
        job_id: str,
        input_path: str | Path,
        render_source_path: str | Path | None = None,
        output_path: str | Path,
        profile_id: str = NORMALIZATION_PROFILE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ModalNormalizationResult:
        """Normalize on Modal once per source/profile and return a verified local proxy."""
        if not JOB_ID_PATTERN.fullmatch(job_id or ""):
            raise ValueError("Identificador de trabajo Modal no válido.")
        source = Path(input_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("El vídeo fuente no existe o está vacío.")
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("El vídeo supera el límite de subida configurado para Modal (2 GiB).")
        if source.suffix.lower() not in {".mov", ".mp4"}:
            raise ValueError("Modal solo admite archivos .MOV o .MP4.")
        if destination.exists():
            raise FileExistsError("Se rechaza sobrescribir un proxy normalizado existente.")
        profile_spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
        if profile_spec is None:
            raise ValueError("Perfil de normalización Modal no admitido.")
        if profile_id == MASTER_4K_NORMALIZATION_PROFILE_ID:
            from .media import probe_video

            source_profile = probe_video(source)
            is_hlg = (
                source_profile.color_range == "tv"
                and source_profile.color_primaries == "bt2020"
                and source_profile.color_transfer == "arib-std-b67"
                and source_profile.color_space == "bt2020nc"
            )
            is_sdr_bt709 = (
                source_profile.color_range in {"tv", "pc"}
                and source_profile.color_primaries == "bt709"
                and source_profile.color_transfer == "bt709"
                and source_profile.color_space == "bt709"
            )
            display_width, display_height = source_profile.width, source_profile.height
            if abs(int(round(source_profile.rotation or 0))) % 180 == 90:
                display_width, display_height = display_height, display_width
            if (
                display_width < 2160
                or display_height < 3840
                or abs(source_profile.duration - 59.98) > 0.5
            ):
                raise ValueError(
                    "La normalización máster 4K requiere el original vertical 2160×3840 de 59,98 s."
                )
            if not (is_hlg or is_sdr_bt709):
                raise ValueError(
                    "La entrada debe tener etiquetas explícitas HLG BT.2020 limitada o SDR BT.709."
                )
            from .cost_controls import enforce_4k_normalization_budget

            enforce_4k_normalization_budget()

        self.ensure_worker_available()
        sdk = self._sdk()
        volume = sdk.Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=True)
        started = time.monotonic()
        source_sha256 = _safe_sha256(source)
        render_source = (
            Path(render_source_path).expanduser().resolve()
            if render_source_path is not None else source
        )
        source_is_render_source = source == render_source
        cache_prefix = f"normalized-cache/{source_sha256}/{profile_id}"
        cache_manifest_path = f"{cache_prefix}/manifest.json"
        cache_manifest_bytes = self._read_volume_small(volume, cache_manifest_path)
        cache_result: dict[str, Any] | None = None
        if cache_manifest_bytes is not None:
            try:
                cached = json.loads(cache_manifest_bytes)
            except json.JSONDecodeError:
                raise RuntimeError("El manifiesto de la caché Modal está malformado; se bloqueó una conversión nueva.") from None
            if (
                cached.get("source_sha256") != source_sha256
                or cached.get("profile_id") != profile_id
                or cached.get("proxy_path") != f"{cache_prefix}/proxy.mp4"
            ):
                raise RuntimeError("La caché Modal no coincide con el origen y perfil solicitados.")
            self._validate_normalized_profile(cached.get("profile"), profile_id)
            cache_result = {
                "job_id": job_id,
                "normalized_proxy_path": cached["proxy_path"],
                "source_sha256": source_sha256,
                "profile_id": profile_id,
                "proxy_sha256": cached.get("proxy_sha256"),
                "proxy_size_bytes": cached.get("proxy_size_bytes"),
                "duration_seconds": cached.get("duration_seconds"),
                "profile": cached.get("profile"),
                "capabilities": cached.get("capabilities"),
                "cache_hit": True,
                "processing_seconds": cached.get("processing_seconds"),
            }
        else:
            remote_prefix = f"jobs/{job_id}"
            remote_source_name = (
                f"source{source.suffix.lower()}"
                if source_is_render_source
                else f"analysis-source{source.suffix.lower()}"
            )
            manifest = {
                "job_id": job_id,
                "source_name": remote_source_name,
                "source_sha256": source_sha256,
                "profile_id": profile_id,
            }
            with tempfile.TemporaryDirectory(prefix="matrix-motion-normalize-stage-") as staging:
                manifest_path = Path(staging) / "normalization.json"
                manifest_path.write_text(
                    json.dumps(manifest, separators=(",", ":")),
                    encoding="utf-8",
                )
                if progress_callback:
                    progress_callback("uploading_normalization_source")
                with volume.batch_upload() as batch:
                    batch.put_file(str(source), f"/{remote_prefix}/{remote_source_name}")
                    batch.put_file(str(manifest_path), f"/{remote_prefix}/normalization.json")
                self._staged_jobs.add(job_id)
            if progress_callback:
                progress_callback("normalizing_on_modal")
            cache_result = self._worker.remote(job_id, "normalize")
            if source_is_render_source:
                self._staged_sources[job_id] = source_sha256

        if not isinstance(cache_result, dict) or cache_result.get("job_id") != job_id:
            raise RuntimeError("Modal devolvió una respuesta de normalización no reconocida.")
        remote_proxy_path = cache_result.get("normalized_proxy_path")
        expected_remote_path = f"{cache_prefix}/proxy.mp4"
        if remote_proxy_path != expected_remote_path:
            raise RuntimeError("Modal devolvió una ruta de proxy normalizado no válida.")
        try:
            expected_size = int(cache_result.get("proxy_size_bytes") or 0)
        except (TypeError, ValueError):
            expected_size = 0
        expected_hash = cache_result.get("proxy_sha256")
        if expected_size <= 0 or expected_size > profile_spec["max_bytes"]:
            raise RuntimeError("Modal devolvió un proxy normalizado fuera del límite del perfil.")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise RuntimeError("Modal devolvió un checksum no válido para el proxy normalizado.")
        normalized_profile = self._validate_normalized_profile(cache_result.get("profile"), profile_id)

        if progress_callback:
            progress_callback("downloading_normalized_proxy")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as output:
                for chunk in volume.read_file(remote_proxy_path):
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise RuntimeError("Modal envió un bloque de proxy no binario.")
                    size += len(chunk)
                    if size > profile_spec["max_bytes"]:
                        raise RuntimeError("La descarga del proxy superó el límite del perfil.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise RuntimeError("El proxy descargado no coincide con su tamaño o checksum remoto.")
            os.replace(partial, destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        self._normalized_jobs[job_id] = {
            "source_sha256": source_sha256,
            "profile_id": profile_id,
            "width": normalized_profile["width"],
            "height": normalized_profile["height"],
            "remote_proxy_path": remote_proxy_path,
            "proxy_sha256": expected_hash,
        }
        return ModalNormalizationResult(
            output_path=destination,
            duration_seconds=float(cache_result.get("duration_seconds") or 0),
            size_bytes=size,
            sha256=digest.hexdigest(),
            source_sha256=source_sha256,
            profile_id=profile_id,
            remote_proxy_path=remote_proxy_path,
            cache_hit=bool(cache_result.get("cache_hit")),
            remote_processing_seconds=(
                0.0 if cache_result.get("cache_hit") else
                float(cache_result["processing_seconds"])
                if cache_result.get("processing_seconds") is not None else None
            ),
            elapsed_seconds=time.monotonic() - started,
            profile=normalized_profile,
            capabilities=cache_result.get("capabilities") if isinstance(cache_result.get("capabilities"), dict) else None,
        )

    def submit_normalization_detached(
        self,
        *,
        job_id: str,
        input_path: str | Path,
        output_path: str | Path,
        profile_id: str = MASTER_4K_NORMALIZATION_PROFILE_ID,
        on_submitted: Callable[[str], None],
        on_phase: Callable[[str], None] | None = None,
        check_cache: bool = True,
    ) -> ModalNormalizationSubmission | ModalNormalizationResult:
        """Submit normalization without waiting on the caller's SSH/session lifetime."""
        if not JOB_ID_PATTERN.fullmatch(job_id or ""):
            raise ValueError("Identificador de trabajo Modal no válido.")
        source = Path(input_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("El vídeo fuente no existe o está vacío.")
        if source.stat().st_size > MAX_SOURCE_BYTES or source.suffix.lower() not in {".mov", ".mp4"}:
            raise ValueError("El origen Modal no cumple los límites de tamaño o formato.")
        if destination.exists():
            raise FileExistsError("Se rechaza sobrescribir un proxy normalizado existente.")
        profile_spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
        if profile_spec is None:
            raise ValueError("Perfil de normalización Modal no admitido.")

        if profile_id == MASTER_4K_NORMALIZATION_PROFILE_ID:
            from .media import probe_video
            from .cost_controls import enforce_4k_normalization_budget

            source_profile = probe_video(source)
            display_width, display_height = source_profile.width, source_profile.height
            if abs(int(round(source_profile.rotation or 0))) % 180 == 90:
                display_width, display_height = display_height, display_width
            if display_width < 2160 or display_height < 3840 or abs(source_profile.duration - 59.98) > 0.5:
                raise ValueError("La normalización máster requiere el original vertical 2160×3840 de 59,98 s.")
            is_hlg = (
                source_profile.color_range == "tv"
                and source_profile.color_primaries == "bt2020"
                and source_profile.color_transfer == "arib-std-b67"
                and source_profile.color_space == "bt2020nc"
            )
            is_sdr_bt709 = (
                source_profile.color_range in {"tv", "pc"}
                and source_profile.color_primaries == "bt709"
                and source_profile.color_transfer == "bt709"
                and source_profile.color_space == "bt709"
            )
            if not (is_hlg or is_sdr_bt709):
                raise ValueError("La entrada debe estar etiquetada HLG BT.2020 o SDR BT.709.")
            enforce_4k_normalization_budget()

        source_sha256 = _safe_sha256(source)
        if check_cache:
            cached_result = self.recover_cached_normalization(
                input_path=source,
                output_path=destination,
                profile_id=profile_id,
            )
            if cached_result is not None:
                return cached_result

        self.ensure_worker_available()
        sdk = self._sdk()
        volume = sdk.Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=True)
        remote_prefix = f"jobs/{job_id}"
        manifest = {
            "job_id": job_id,
            "source_name": f"source{source.suffix.lower()}",
            "source_sha256": source_sha256,
            "profile_id": profile_id,
        }
        with tempfile.TemporaryDirectory(prefix="matrix-motion-normalize-submit-") as staging:
            manifest_path = Path(staging) / "normalization.json"
            manifest_path.write_text(
                json.dumps(manifest, separators=(",", ":")),
                encoding="utf-8",
            )
            if on_phase:
                on_phase("uploading_source_to_modal_volume")
            with volume.batch_upload() as batch:
                batch.put_file(str(source), f"/{remote_prefix}/source{source.suffix.lower()}")
                batch.put_file(str(manifest_path), f"/{remote_prefix}/normalization.json")

        if on_phase:
            on_phase("submitting_detached_modal_call")
        function_call_id: str | None = None
        try:
            function_call = self._worker.spawn(job_id, "normalize")
            function_call_id = str(function_call.object_id)
            if not function_call_id:
                raise RuntimeError("Modal no devolvió un ID de ejecución.")
            on_submitted(function_call_id)
        except BaseException as exc:
            raise ModalSubmissionUnknownError(
                "El envío puede haberse aceptado, pero no se confirmó su persistencia; no se debe reenviar.",
                function_call_id=function_call_id,
            ) from exc
        return ModalNormalizationSubmission(
            job_id=job_id,
            function_call_id=function_call_id,
            source_sha256=source_sha256,
            profile_id=profile_id,
            submitted_at=time.time(),
        )

    def poll_normalization_call(self, function_call_id: str) -> dict[str, Any] | None:
        """Read a detached call result without submitting or cancelling work."""
        if not function_call_id.startswith("fc-") or len(function_call_id) > 128:
            raise ValueError("ID de ejecución Modal no válido.")
        sdk = self._sdk()
        function_call = sdk.FunctionCall.from_id(function_call_id)
        try:
            result = function_call.get(timeout=0)
        except TimeoutError:
            return None
        if not isinstance(result, dict):
            raise RuntimeError("Modal devolvió un resultado de normalización no reconocido.")
        return result

    def read_normalization_progress(self, job_id: str) -> dict[str, Any] | None:
        """Read the last committed worker phase without reloading or invoking the worker."""
        if not JOB_ID_PATTERN.fullmatch(job_id or ""):
            raise ValueError("Identificador de trabajo Modal no válido.")
        volume = self._sdk().Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=False)
        payload = self._read_volume_small(
            volume,
            f"jobs/{job_id}/worker-state.json",
            maximum_bytes=16 * 1024,
        )
        if payload is None:
            return None
        try:
            state = json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError("El estado persistente del worker está malformado.") from None
        if not isinstance(state, dict) or state.get("job_id") != job_id:
            raise RuntimeError("El estado remoto no corresponde al trabajo Modal solicitado.")
        return state

    def recover_cached_normalization(
        self,
        *,
        input_path: str | Path,
        output_path: str | Path,
        profile_id: str = MASTER_4K_NORMALIZATION_PROFILE_ID,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ModalNormalizationResult | None:
        """Read and verify an existing cached proxy without looking up or invoking a worker."""
        source = Path(input_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("El vídeo fuente no existe o está vacío.")
        if destination.exists():
            raise FileExistsError("Se rechaza sobrescribir un proxy normalizado existente.")
        profile_spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
        if profile_spec is None:
            raise ValueError("Perfil de normalización Modal no admitido.")

        started = time.monotonic()
        source_sha256 = _safe_sha256(source)
        cache_prefix = f"normalized-cache/{source_sha256}/{profile_id}"
        proxy_path = f"{cache_prefix}/proxy.mp4"
        if progress_callback:
            progress_callback("checking_modal_cache")
        volume = self._sdk().Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=False)
        manifest_bytes = self._read_volume_small(volume, f"{cache_prefix}/manifest.json")
        if manifest_bytes is None:
            return None
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError:
            raise RuntimeError("El manifiesto de recuperación de Modal está malformado.") from None
        if (
            manifest.get("source_sha256") != source_sha256
            or manifest.get("profile_id") != profile_id
            or manifest.get("proxy_path") != proxy_path
        ):
            raise RuntimeError("El manifiesto remoto no corresponde al vídeo y perfil solicitados.")
        profile = self._validate_normalized_profile(manifest.get("profile"), profile_id)
        try:
            expected_size = int(manifest.get("proxy_size_bytes") or 0)
        except (TypeError, ValueError):
            expected_size = 0
        expected_hash = manifest.get("proxy_sha256")
        if (
            expected_size <= 0
            or expected_size > profile_spec["max_bytes"]
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
        ):
            raise RuntimeError("El manifiesto remoto no contiene un tamaño o checksum recuperable.")
        try:
            duration_seconds = float(manifest.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            duration_seconds = 0
        if (
            duration_seconds <= 0
            or abs(duration_seconds - float(profile["duration_seconds"])) > 1 / profile_spec["fps"]
        ):
            raise RuntimeError("La duración del manifiesto no coincide con el perfil verificado.")

        if progress_callback:
            progress_callback("downloading_recovered_proxy")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as output:
                for chunk in volume.read_file(proxy_path):
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise RuntimeError("Modal envió un bloque de proxy no binario.")
                    size += len(chunk)
                    if size > profile_spec["max_bytes"]:
                        raise RuntimeError("El proxy recuperado supera el límite del perfil.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise RuntimeError("El proxy recuperado no coincide con el tamaño o checksum guardado.")
            os.replace(partial, destination)
        except FileNotFoundError:
            partial.unlink(missing_ok=True)
            raise RuntimeError("El manifiesto existe, pero el proxy no está disponible en el volumen Modal.") from None
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        return ModalNormalizationResult(
            output_path=destination,
            duration_seconds=duration_seconds,
            size_bytes=size,
            sha256=digest.hexdigest(),
            source_sha256=source_sha256,
            profile_id=profile_id,
            remote_proxy_path=proxy_path,
            cache_hit=True,
            remote_processing_seconds=(
                float(manifest["processing_seconds"])
                if manifest.get("processing_seconds") is not None else None
            ),
            elapsed_seconds=time.monotonic() - started,
            profile=profile,
            capabilities=manifest.get("capabilities")
            if isinstance(manifest.get("capabilities"), dict) else None,
        )

    def render_was_submitted(self, job_id: str) -> bool:
        return job_id in self._render_started_jobs

    def cleanup_staged_job(self, job_id: str) -> bool:
        """Remove only the per-job staging copy; retain the content-addressed proxy cache."""
        if (
            not JOB_ID_PATTERN.fullmatch(job_id or "")
            or job_id not in self._staged_jobs
            or self.render_was_submitted(job_id)
        ):
            return False
        try:
            volume = self._sdk().Volume.from_name(MODAL_VOLUME_NAME, create_if_missing=True)
            volume.remove_file(f"jobs/{job_id}", recursive=True)
        except Exception as exc:
            logger.warning(
                "modal_normalization_staging_cleanup_failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
            return False
        self._staged_sources.pop(job_id, None)
        self._normalized_jobs.pop(job_id, None)
        self._staged_jobs.discard(job_id)
        return True

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
        if (expected_width, expected_height) == (2160, 3840) and expected_fps != 30:
            raise ValueError("El perfil máster 2160p requiere exactamente 30 FPS.")
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
        source_sha256 = _safe_sha256(source)
        staged_source_sha256 = self._staged_sources.get(job_id)
        source_already_staged = staged_source_sha256 == source_sha256
        normalized_state = self._normalized_jobs.get(job_id)
        if normalized_state and normalized_state.get("source_sha256") != source_sha256:
            # An analysis clip may have its own cache entry; never attach it to a
            # render of the complete original.
            normalized_state = None
        if normalized_state and (
            normalized_state.get("width") != expected_width
            or normalized_state.get("height") != expected_height
        ):
            # A lower-resolution analysis proxy must never become the source for
            # a higher-resolution master; the renderer derives its scene source
            # from the untouched master instead.
            normalized_state = None
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
                        "operation": "render",
                        "source_name": source.name,
                        "source_sha256": source_sha256,
                        "show_subtitles": show_subtitles,
                        "briefing": briefing,
                        "caption_style": caption_style or {},
                        **(
                            {
                                "normalized_proxy_path": normalized_state["remote_proxy_path"],
                                "normalized_proxy_sha256": normalized_state["proxy_sha256"],
                                "normalized_proxy_source_sha256": normalized_state["source_sha256"],
                                "normalization_profile_id": normalized_state["profile_id"],
                            }
                            if normalized_state else {}
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            with volume.batch_upload() as batch:
                if not source_already_staged:
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
        self._render_started_jobs.add(job_id)
        result = self._worker.remote(job_id, "render")
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
            self._staged_sources.pop(job_id, None)
            self._normalized_jobs.pop(job_id, None)
            self._staged_jobs.discard(job_id)
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
