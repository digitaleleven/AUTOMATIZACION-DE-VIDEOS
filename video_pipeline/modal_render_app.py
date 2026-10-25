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
    .run_commands("npm install --global pnpm@10.26.1")
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
def render_master(job_id: str) -> dict[str, Any]:
    """Render one staged job; no automatic retries and no provider API keys."""
    if not _JOB_ID.fullmatch(job_id or ""):
        raise ValueError("Identificador de render Modal no válido.")

    staging_volume.reload()
    remote_job = VOLUME_MOUNT / "jobs" / job_id
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
        ]
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
        if not any(stream.get("codec_type") == "video" for stream in output_streams):
            raise RuntimeError("El resultado de Remotion no contiene una pista de vídeo.")
        if not any(stream.get("codec_type") == "audio" for stream in output_streams):
            raise RuntimeError("El resultado de Remotion no contiene una pista de audio.")

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
        }
    finally:
        shutil.rmtree(local_job, ignore_errors=True)
