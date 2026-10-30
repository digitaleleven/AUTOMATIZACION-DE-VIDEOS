"""Modal serverless worker for MATRIX MOTION's Remotion master renderer.

Deploy from the workspace root with:
    modal deploy video_pipeline/modal_render_app.py

The worker accepts only a random job ID. Video, transcript, plan, and assets are
staged in a per-job Modal Volume directory by the authenticated MATRIX MOTION
server; the finished MP4 is copied back to the same directory for retrieval.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from fractions import Fraction
from typing import Any

import modal


APP_NAME = "matrix-motion-remotion"
VOLUME_NAME = "matrix-motion-render-staging"
VOLUME_MOUNT = Path("/mnt/matrix-motion")
REMOTE_PROJECT = Path("/opt/matrix-motion")
REMOTE_RENDERER = REMOTE_PROJECT / "remotion-renderer"
REMOTE_STYLE = REMOTE_PROJECT / "video_style.json"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_CAPTION_STYLE_KEYS = {
    "font_family",
    "font_color",
    "highlight_color",
    "caption_font_size",
    "caption_position",
    "max_words_per_caption",
}
_WORKER_OUTPUT_PROFILES = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
    "2160p": (2160, 3840),
}
_WORKER_OUTPUT_FPS = {25, 30, 50, 60}
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
NORMALIZATION_TIMEOUT_SECONDS = 20 * 60
MAX_NORMALIZED_PROXY_BYTES = 8 * 1024**3

app = modal.App(name=APP_NAME)
staging_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

renderer_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "ca-certificates",
        "chromium",
        "ffmpeg",
        "fonts-liberation",
        "nodejs",
        "npm",
    )
    .run_commands(
        "npm install --global pnpm@10.26.1",
        "ffmpeg -hide_banner -filters 2>&1 | grep -q zscale",
        "ffmpeg -hide_banner -filters 2>&1 | grep -q tonemap",
        "ffmpeg -hide_banner -encoders 2>&1 | grep -q libx264",
        "ffmpeg -hide_banner -decoders 2>&1 | grep -q hevc",
        "ffprobe -version >/dev/null",
    )
    .add_local_dir(
        str(Path(__file__).resolve().parents[1] / "remotion-renderer"),
        remote_path=str(REMOTE_RENDERER),
        copy=True,
        ignore=["node_modules", ".git", ".cache", "generated-artifacts"],
    )
    .add_local_file(
        str(Path(__file__).resolve().parents[1] / "video_style.json"),
        remote_path=str(REMOTE_STYLE),
        copy=True,
    )
    .run_commands(
        f"cd {REMOTE_RENDERER} && pnpm install --frozen-lockfile",
    )
    .env(
        {
            "MATRIX_MOTION_CHROMIUM_BIN": "/usr/bin/chromium",
            "NODE_ENV": "production",
        }
    )
)


def _run_checked(args: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )


def _safe_diagnostic_text(value: Any, limit: int = 8000) -> str:
    """Bound subprocess logs and remove URLs and common credential formats."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization|x-goog-api-key|x-api-key|api[_-]?key|"
        r"token(?:[_-](?:id|secret))?|password|secret)[\"']?\s*[:=]\s*[\"']?"
        r"(?:bearer\s+)?[^\s,;\"']+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[KEY REDACTED]", text)
    return text[-limit:]


def _worker_failure_diagnostic(job_id: str, exc: Exception) -> dict[str, Any]:
    is_process = isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired))
    return {
        "job_id": job_id,
        "error_type": type(exc).__name__,
        "returncode": getattr(exc, "returncode", None),
        "timeout_seconds": getattr(exc, "timeout", None),
        # Do not log command arguments: they can contain customer briefing text.
        "message": (
            "El subproceso del motor falló; consulta stderr."
            if is_process else _safe_diagnostic_text(str(exc), 1000)
        ),
        "stdout": _safe_diagnostic_text(getattr(exc, "stdout", "")),
        "stderr": _safe_diagnostic_text(getattr(exc, "stderr", "")),
    }


def _persist_worker_failure(remote_job: Path, job_id: str, exc: Exception) -> dict[str, Any]:
    diagnostic = _worker_failure_diagnostic(job_id, exc)
    # Exclusive creation preserves the first failure rather than overwriting history.
    with (remote_job / "failure.json").open("x", encoding="utf-8") as stream:
        json.dump(diagnostic, stream, ensure_ascii=False, indent=2)
    return diagnostic


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_video_profile(path: Path) -> dict[str, Any]:
    result = _run_checked(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_streams", "-of", "json", str(path),
        ],
        timeout=120,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("No se pudo leer la pista de vídeo de la fuente.")
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    rotation = 0
    for side_data in stream.get("side_data_list", []):
        if side_data.get("rotation") is not None:
            rotation = int(round(float(side_data["rotation"])))
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "width": width,
        "height": height,
        "avg_frame_rate": stream.get("avg_frame_rate") or "0/1",
    }


def _probe_normalized_proxy(
    path: Path,
    *,
    expected_width: int = 1080,
    expected_height: int = 1920,
    expected_fps: int = 30,
) -> dict[str, Any]:
    result = _run_checked(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        timeout=120,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise ValueError("El proxy normalizado debe conservar una pista de vídeo y otra de audio.")
    duration = float(payload.get("format", {}).get("duration") or 0)
    profile = {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _rate_value(video.get("avg_frame_rate") or "0/1"),
        "codec": video.get("codec_name"),
        "pixel_format": video.get("pix_fmt"),
        "color_range": video.get("color_range"),
        "color_primaries": video.get("color_primaries"),
        "color_transfer": video.get("color_transfer"),
        "color_space": video.get("color_space"),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio.get("sample_rate") or 0),
        "audio_channels": int(audio.get("channels") or 0),
        "duration_seconds": duration,
    }
    if (
        profile["codec"] != "h264"
        or (profile["width"], profile["height"]) != (expected_width, expected_height)
        or abs(profile["fps"] - expected_fps) > 0.05
        or profile["pixel_format"] != "yuv420p"
        or profile["color_range"] != "tv"
        or profile["color_primaries"] != "bt709"
        or profile["color_transfer"] != "bt709"
        or profile["color_space"] != "bt709"
        or profile["audio_codec"] != "aac"
        or profile["audio_sample_rate"] != 48000
        or profile["audio_channels"] != 2
        or duration <= 0
    ):
        raise ValueError("FFprobe rechazó el perfil H.264/AAC BT.709 de normalización.")
    return profile


def _verify_ffmpeg_capabilities() -> dict[str, str]:
    version = _run_checked(["ffmpeg", "-version"], timeout=30).stdout.splitlines()[0]
    filters = _run_checked(["ffmpeg", "-hide_banner", "-filters"], timeout=60)
    encoders = _run_checked(["ffmpeg", "-hide_banner", "-encoders"], timeout=60)
    decoders = _run_checked(["ffmpeg", "-hide_banner", "-decoders"], timeout=60)
    filter_text = filters.stdout + filters.stderr
    encoder_text = encoders.stdout + encoders.stderr
    decoder_text = decoders.stdout + decoders.stderr
    missing = [
        name for name, present in (
            ("zscale", "zscale" in filter_text),
            ("tonemap", "tonemap" in filter_text),
            ("libx264", "libx264" in encoder_text),
            ("HEVC", "hevc" in decoder_text.lower()),
        ) if not present
    ]
    if missing:
        raise RuntimeError(
            "La imagen de Modal carece de capacidades FFmpeg requeridas: "
            + ", ".join(missing)
        )
    return {
        "ffmpeg_version": version[:240],
        "filters": "zscale,tonemap",
        "encoder": "libx264",
        "hevc_decoder": "available",
    }


def _normalization_cache_paths(
    source_sha256: str,
    profile_id: str = NORMALIZATION_PROFILE_ID,
) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise ValueError("Checksum de origen no válido para la caché de normalización.")
    if profile_id not in NORMALIZATION_PROFILE_SPECS:
        raise ValueError("Perfil de normalización Modal no admitido.")
    base = VOLUME_MOUNT / "normalized-cache" / source_sha256 / profile_id
    return base / "proxy.mp4", base / "manifest.json"


def _validate_cached_normalized_proxy(
    proxy_path: Path,
    manifest_path: Path,
    *,
    source_sha256: str,
    profile_id: str = NORMALIZATION_PROFILE_ID,
) -> dict[str, Any]:
    spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
    if spec is None:
        raise ValueError("Perfil de caché de normalización no admitido.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("source_sha256") != source_sha256
        or manifest.get("profile_id") != profile_id
        or manifest.get("proxy_size_bytes") != proxy_path.stat().st_size
        or manifest.get("proxy_sha256") != _file_sha256(proxy_path)
    ):
        raise RuntimeError("La caché normalizada existe, pero su manifiesto o checksum no coincide.")
    profile = _probe_normalized_proxy(
        proxy_path,
        expected_width=spec["width"],
        expected_height=spec["height"],
        expected_fps=spec["fps"],
    )
    if abs(float(manifest.get("duration_seconds") or 0) - profile["duration_seconds"]) > 1 / spec["fps"]:
        raise RuntimeError("La duración guardada en la caché normalizada no coincide con FFprobe.")
    return {
        **manifest,
        "proxy_path": proxy_path.relative_to(VOLUME_MOUNT).as_posix(),
        "profile": profile,
        "cache_hit": True,
    }


def _normalize_staged_source(remote_job: Path, job_id: str) -> dict[str, Any]:
    started = time.monotonic()
    request_path = remote_job / "normalization.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    profile_id = str(request.get("profile_id") or "")
    spec = NORMALIZATION_PROFILE_SPECS.get(profile_id)
    if request.get("job_id") != job_id or spec is None:
        raise ValueError("La solicitud de normalización no coincide con el perfil aprobado.")
    source_sha256 = str(request.get("source_sha256") or "")
    source_name = str(request.get("source_name") or "")
    if (
        not re.fullmatch(r"[a-f0-9]{64}", source_sha256)
        or Path(source_name).name != source_name
        or Path(source_name).suffix.lower() not in {".mov", ".mp4"}
    ):
        raise ValueError("La solicitud de normalización contiene un origen no válido.")
    staged_source = remote_job / source_name
    if not staged_source.is_file() or staged_source.stat().st_size <= 0:
        raise FileNotFoundError("No se encontró el origen temporal de normalización.")
    if _file_sha256(staged_source) != source_sha256:
        raise RuntimeError("El checksum del origen subido a Modal no coincide.")

    proxy_path, cache_manifest_path = _normalization_cache_paths(source_sha256, profile_id)
    capabilities = _verify_ffmpeg_capabilities()
    if proxy_path.exists() or cache_manifest_path.exists():
        if not proxy_path.is_file() or not cache_manifest_path.is_file():
            raise RuntimeError("La caché de normalización está incompleta; se bloqueó una conversión duplicada.")
        return _validate_cached_normalized_proxy(
            proxy_path,
            cache_manifest_path,
            source_sha256=source_sha256,
            profile_id=profile_id,
        )

    local_dir = Path(tempfile.mkdtemp(prefix=f"matrix-normalize-{job_id}-"))
    try:
        local_source = local_dir / source_name
        local_proxy = local_dir / "normalized-proxy.mp4"
        shutil.copyfile(staged_source, local_source)
        _run_checked(
            [
                "node",
                str(REMOTE_RENDERER / "src/normalize-input.mjs"),
                str(local_source),
                str(local_proxy),
                spec["cli_flag"],
            ],
            timeout=NORMALIZATION_TIMEOUT_SECONDS,
        )
        if not local_proxy.is_file() or not 0 < local_proxy.stat().st_size <= spec["max_bytes"]:
            raise RuntimeError("El proxy de normalización está vacío o supera el límite del perfil.")
        profile = _probe_normalized_proxy(
            local_proxy,
            expected_width=spec["width"],
            expected_height=spec["height"],
            expected_fps=spec["fps"],
        )
        cache_manifest = {
            "source_sha256": source_sha256,
            "profile_id": profile_id,
            "proxy_path": proxy_path.relative_to(VOLUME_MOUNT).as_posix(),
            "proxy_sha256": _file_sha256(local_proxy),
            "proxy_size_bytes": local_proxy.stat().st_size,
            "duration_seconds": profile["duration_seconds"],
            "profile": profile,
            "capabilities": capabilities,
            "processing_seconds": round(time.monotonic() - started, 3),
            "cache_hit": False,
        }
        proxy_path.parent.mkdir(parents=True, exist_ok=True)
        proxy_tmp = proxy_path.with_name("proxy.mp4.part")
        manifest_tmp = cache_manifest_path.with_name("manifest.json.part")
        shutil.copyfile(local_proxy, proxy_tmp)
        manifest_tmp.write_text(
            json.dumps(cache_manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(proxy_tmp, proxy_path)
        os.replace(manifest_tmp, cache_manifest_path)
        staging_volume.commit()
        return cache_manifest
    finally:
        shutil.rmtree(local_dir, ignore_errors=True)


def validate_worker_render_profile(
    plan: dict[str, Any],
    *,
    source_width: int,
    source_height: int,
) -> tuple[int, int, int]:
    """Validate staged output settings before invoking the renderer."""
    width = plan.get("output_width")
    height = plan.get("output_height")
    fps = plan.get("output_fps")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (width, height, fps)):
        raise ValueError("El plan no contiene una resolución y una tasa de salida válidas.")
    if (width, height) not in _WORKER_OUTPUT_PROFILES.values():
        raise ValueError("La salida debe ser vertical 720p, 1080p o 2160p.")
    if fps not in _WORKER_OUTPUT_FPS:
        raise ValueError("La tasa de salida debe ser 25, 30, 50 o 60 FPS.")
    if (width, height) == _WORKER_OUTPUT_PROFILES["2160p"] and (
        source_width < 2160 or source_height < 3840
    ):
        raise ValueError("La salida 2160p requiere una fuente maestra vertical de 2160 × 3840 o superior.")
    return width, height, fps


def _rate_value(value: str) -> float:
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _normalize_audio_if_needed(source: Path, work_dir: Path) -> Path:
    """Keep compatible source untouched; otherwise remux video and normalize audio."""
    probe = json.loads(
        _run_checked(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(source),
            ],
            timeout=120,
        ).stdout
    )
    audio = [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == "audio"
        and stream.get("codec_name") not in {"apac", "unknown"}
    ]
    selected = next(
        (
            stream
            for stream in audio
            if stream.get("codec_name") == "aac"
            and str(stream.get("sample_rate")) == "48000"
            and stream.get("channels") == 2
        ),
        audio[0] if audio else None,
    )
    if selected is None:
        raise RuntimeError("El vídeo no contiene una pista de audio compatible para la composición Remotion.")
    if (
        selected.get("codec_name") == "aac"
        and str(selected.get("sample_rate")) == "48000"
        and selected.get("channels") == 2
    ):
        return source

    normalized = work_dir / "source-audio-normalized.mp4"
    _run_checked(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            f"0:{int(selected['index'])}",
            "-map_metadata",
            "0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(normalized),
        ],
        timeout=1800,
    )
    return normalized


def _safe_caption_style(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _SAFE_CAPTION_STYLE_KEYS:
        item = value.get(key)
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            if len(str(item)) <= 120:
                result[key] = item
    return result


@app.function(
    image=renderer_image,
    volumes={str(VOLUME_MOUNT): staging_volume},
    cpu=4,
    memory=16 * 1024,
    timeout=60 * 60,
    retries=0,
    max_containers=1,
)
def render_master(job_id: str, operation: str = "render") -> dict[str, Any]:
    """Normalize/cache or render one staged job; no automatic retries or API keys."""
    if not _JOB_ID.fullmatch(job_id or ""):
        raise ValueError("Identificador de render Modal no válido.")
    if operation not in {"normalize", "render"}:
        raise ValueError("Operación Modal no admitida.")

    staging_volume.reload()
    remote_job = VOLUME_MOUNT / "jobs" / job_id
    if not remote_job.is_dir():
        raise FileNotFoundError("No se encontró el directorio temporal del trabajo Modal.")
    if (remote_job / "failure.json").exists():
        raise RuntimeError("El trabajo Modal ya falló; no se permite reenviar este identificador.")
    if operation == "normalize":
        try:
            manifest = _normalize_staged_source(remote_job, job_id)
            return {
                "job_id": job_id,
                "normalized_proxy_path": manifest["proxy_path"],
                "source_sha256": manifest["source_sha256"],
                "profile_id": manifest["profile_id"],
                "proxy_sha256": manifest["proxy_sha256"],
                "proxy_size_bytes": manifest["proxy_size_bytes"],
                "duration_seconds": manifest["duration_seconds"],
                "profile": manifest["profile"],
                "capabilities": manifest.get("capabilities"),
                "cache_hit": manifest["cache_hit"],
                "processing_seconds": manifest.get("processing_seconds"),
            }
        except Exception as exc:
            diagnostic = _worker_failure_diagnostic(job_id, exc)
            try:
                _persist_worker_failure(remote_job, job_id, exc)
                staging_volume.commit()
            except Exception as persist_error:
                print(f"matrix_failure_persistence_failed type={type(persist_error).__name__}", flush=True)
            print(f"matrix_normalization_failed {json.dumps(diagnostic, ensure_ascii=False)}", flush=True)
            details = diagnostic["stderr"][-2000:] or diagnostic["message"]
            raise RuntimeError(
                f"Normalización Modal: {diagnostic['error_type']}; "
                f"código {diagnostic['returncode']}. {details}"
            ) from None

    manifest_path = remote_job / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("No se encontró el manifiesto del trabajo Modal.")
    if (remote_job / "output.mp4").exists():
        raise FileExistsError("El trabajo Modal ya generó un MP4; se impide un segundo render.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("job_id") != job_id:
        raise ValueError("El manifiesto no coincide con el trabajo Modal solicitado.")
    show_subtitles = manifest.get("show_subtitles")
    if not isinstance(show_subtitles, bool):
        raise ValueError("La opción de subtítulos del trabajo Modal no es válida.")
    briefing = manifest.get("briefing", "")
    if not isinstance(briefing, str) or len(briefing) > 4000:
        raise ValueError("Las notas del trabajo Modal superan el límite admitido.")
    caption_style = _safe_caption_style(manifest.get("caption_style"))

    local_job = Path(tempfile.mkdtemp(prefix=f"matrix-motion-{job_id}-"))
    try:
        source_name = str(manifest.get("source_name") or "")
        source_suffix = Path(source_name).suffix.lower()
        if source_suffix not in {".mov", ".mp4"}:
            raise ValueError("Modal solo admite fuentes MOV o MP4.")
        staged_source = remote_job / f"source{source_suffix}"
        if not staged_source.is_file() or staged_source.stat().st_size <= 0:
            raise FileNotFoundError("No se encontró el vídeo fuente del trabajo Modal.")

        local_source = local_job / f"source{source_suffix}"
        shutil.copyfile(staged_source, local_source)
        normalized_proxy_local: Path | None = None
        normalized_proxy_path = manifest.get("normalized_proxy_path")
        if normalized_proxy_path is not None:
            if (
                not isinstance(normalized_proxy_path, str)
                or not normalized_proxy_path.startswith("normalized-cache/")
                or ".." in Path(normalized_proxy_path).parts
                or manifest.get("normalized_proxy_source_sha256") != manifest.get("source_sha256")
                or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("normalized_proxy_sha256") or ""))
                or manifest.get("normalization_profile_id") not in NORMALIZATION_PROFILE_SPECS
            ):
                raise ValueError("La referencia al proxy reutilizable no es válida.")
            staged_proxy = VOLUME_MOUNT / normalized_proxy_path
            if (
                not staged_proxy.is_file()
                or _file_sha256(staged_proxy) != manifest["normalized_proxy_sha256"]
            ):
                raise RuntimeError("El proxy normalizado no coincide con el checksum aprobado.")
            normalized_proxy_local = local_job / "normalized-proxy.mp4"
            shutil.copyfile(staged_proxy, normalized_proxy_local)
        local_transcript = local_job / "transcript.json"
        local_plan = local_job / "plan.json"
        local_effects = local_job / "effects"
        local_effects.mkdir()
        for name in ("transcript.json", "plan.json"):
            staged = remote_job / name
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise FileNotFoundError(f"No se encontró el archivo de trabajo Modal: {name}.")
        shutil.copyfile(remote_job / "transcript.json", local_transcript)
        shutil.copyfile(remote_job / "plan.json", local_plan)
        render_plan = json.loads(local_plan.read_text(encoding="utf-8"))
        source_profile = _probe_video_profile(local_source)
        output_width, output_height, output_fps = validate_worker_render_profile(
            render_plan,
            source_width=source_profile["width"],
            source_height=source_profile["height"],
        )
        remote_effects = remote_job / "effects"
        if remote_effects.is_dir():
            for asset in remote_effects.iterdir():
                if (
                    asset.is_file()
                    and asset.suffix.lower() in {".svg", ".png", ".jpg", ".jpeg", ".webp"}
                    and asset.stat().st_size <= 25 * 1024 * 1024
                ):
                    shutil.copyfile(asset, local_effects / asset.name)

        if not isinstance(json.loads(REMOTE_STYLE.read_text(encoding="utf-8")), dict):
            raise ValueError("La configuración de estilos remota no es válida.")
        normalized_source = _normalize_audio_if_needed(local_source, local_job)
        local_output = local_job / "rendered.mp4"
        command = [
            "node",
            str(REMOTE_RENDERER / "render-master.mjs"),
            "--input",
            str(normalized_source),
            "--transcript",
            str(local_transcript),
            "--plan",
            str(local_plan),
            "--effects-dir",
            str(local_effects),
            "--output",
            str(local_output),
            "--show-subtitles",
            "true" if show_subtitles else "false",
            "--briefing",
            briefing,
            "--caption-style-json",
            json.dumps(caption_style, ensure_ascii=False),
            "--width",
            str(output_width),
            "--height",
            str(output_height),
            "--fps",
            str(output_fps),
        ]
        if normalized_proxy_local is not None:
            command.extend(["--normalized-video", str(normalized_proxy_local)])
        _run_checked(command, timeout=55 * 60)
        report_path = local_job / "rendered.report.json"
        if not local_output.is_file() or local_output.stat().st_size <= 0:
            raise RuntimeError("Remotion terminó sin producir un MP4 no vacío.")

        output_probe = json.loads(
            _run_checked(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(local_output),
                ],
                timeout=120,
            ).stdout
        )
        output_streams = output_probe.get("streams", [])
        output_video = next(
            (stream for stream in output_streams if stream.get("codec_type") == "video"),
            None,
        )
        if not output_video:
            raise RuntimeError("El resultado de Remotion no contiene una pista de vídeo.")
        if not any(stream.get("codec_type") == "audio" for stream in output_streams):
            raise RuntimeError("El resultado de Remotion no contiene una pista de audio.")
        if (
            int(output_video.get("width") or 0) != output_width
            or int(output_video.get("height") or 0) != output_height
            or abs(_rate_value(output_video.get("avg_frame_rate") or "0/1") - output_fps) > 0.01
        ):
            raise RuntimeError("El MP4 no coincide con la resolución y los FPS aprobados en el plan.")

        output_size = local_output.stat().st_size
        if output_size > 4 * 1024**3:
            raise RuntimeError("El MP4 generado supera el límite de transferencia permitido.")
        final_remote = remote_job / "output.mp4"
        if final_remote.exists():
            raise FileExistsError("El trabajo Modal ya tiene un MP4; no se iniciará otro render.")
        shutil.copyfile(local_output, final_remote)
        if report_path.is_file():
            shutil.copyfile(report_path, remote_job / "report.json")
        else:
            (remote_job / "report.json").write_text(
                json.dumps(
                    {
                        "format": output_probe.get("format", {}),
                        "streams": output_streams,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        staging_volume.commit()
        return {
            "job_id": job_id,
            "output_path": f"jobs/{job_id}/output.mp4",
            "output_size_bytes": output_size,
            "output_sha256": _file_sha256(local_output),
            "duration_seconds": float(output_probe.get("format", {}).get("duration") or 0),
            "width": next(
                int(stream.get("width") or 0)
                for stream in output_streams
                if stream.get("codec_type") == "video"
            ),
            "height": next(
                int(stream.get("height") or 0)
                for stream in output_streams
                if stream.get("codec_type") == "video"
            ),
            "fps": output_fps,
        }
    except Exception as exc:
        diagnostic = _worker_failure_diagnostic(job_id, exc)
        try:
            _persist_worker_failure(remote_job, job_id, exc)
            staging_volume.commit()
        except Exception as persist_error:
            print(f"matrix_failure_persistence_failed type={type(persist_error).__name__}", flush=True)
        print(f"matrix_render_failed {json.dumps(diagnostic, ensure_ascii=False)}", flush=True)
        details = diagnostic["stderr"][-2000:] or diagnostic["message"]
        raise RuntimeError(
            f"Motor: {diagnostic['error_type']}; código {diagnostic['returncode']}. {details}"
        ) from None
    finally:
        shutil.rmtree(local_job, ignore_errors=True)
