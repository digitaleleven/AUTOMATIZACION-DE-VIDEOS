"""Remotion/FFmpeg processing path executed on the configured Modal worker."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
from shutil import copyfile
import subprocess
from typing import Any

from .config import PipelineConfig
from .edit_plan import (
    EditPlanError,
    edited_duration,
    map_source_time_to_output,
    normalize_edit_plan,
    retained_source_ranges,
    sticker_svg,
)
from .media import probe_video, temporary_work_directory, validate_rendered_output
from .models import WordTiming
from .modal_renderer import ModalRenderClient
from .style import StyleConfigError, VideoStyle, validate_output_profile
from .subtitles import build_subtitle_segments
from .transcription import transcribe_video


def _validate_supplied_word_timings(words: list[WordTiming], duration: float) -> None:
    """Preserve zero-length markers only with the same nearby anchor used by Remotion."""
    for word in words:
        if (
            not isinstance(word, WordTiming)
            or not word.text.strip()
            or not math.isfinite(word.start)
            or not math.isfinite(word.end)
            or word.start < 0
            or word.end < word.start
            or word.end > duration + 0.05
        ):
            raise ValueError("Las palabras con timestamps no superaron la validación temporal.")
    for index, word in enumerate(words):
        if word.end == word.start:
            members = [word]
            for candidate in words[index + 1:]:
                members.append(candidate)
                if candidate.end > candidate.start:
                    break
            anchor = members[-1]
            if (
                anchor.end <= anchor.start
                or not 0 <= anchor.start - word.start <= 0.75
                or sum(len(member.text.split()) for member in members) > 3
            ):
                raise ValueError("Una palabra sin duración no tiene un ancla cercana fiable; alineación pendiente.")


def _render_transcript_payload(words: list[WordTiming]) -> dict[str, Any]:
    """Keep the canonical text and every original interval in the renderer contract."""
    return {
        "text": " ".join(word.text for word in words),
        "words": [
            {"word": word.text, "start": word.start, "end": word.end}
            for word in words
        ],
    }

def _validate_remote_input_profile(source: Path) -> None:
    """Reject unsupported color/audio before any OpenAI or Modal compute request."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    metadata = json.loads(result.stdout)
    streams = metadata.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("FFprobe no encontró una pista de vídeo.")
    hlg = (
        video.get("color_range") == "tv"
        and video.get("color_space") == "bt2020nc"
        and video.get("color_transfer") == "arib-std-b67"
        and video.get("color_primaries") == "bt2020"
    )
    sdr = (
        video.get("color_range") in {"tv", "pc"}
        and video.get("color_space") == "bt709"
        and video.get("color_transfer") == "bt709"
        and video.get("color_primaries") == "bt709"
    )
    if not (hlg or sdr):
        raise ValueError(
            "El motor Modal requiere etiquetas HLG BT.2020 limitado o SDR BT.709 coherentes; "
            "no se iniciaron llamadas ni renders."
        )
    rate = str(video.get("avg_frame_rate") or "0/1")
    try:
        numerator, denominator = (float(part) for part in rate.split("/", 1))
    except (TypeError, ValueError):
        numerator, denominator = 0.0, 1.0
    if denominator <= 0 or numerator <= 0:
        raise ValueError("FFprobe no pudo determinar una tasa de fotogramas válida.")
    if not any(
        item.get("codec_type") == "audio"
        and item.get("codec_name") not in {"apac", "unknown"}
        for item in streams
    ):
        raise ValueError("FFprobe no encontró una pista de audio compatible.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _modal_caption_style(style: VideoStyle) -> dict[str, Any]:
    values = {
        "font_family": getattr(style, "font_family", None),
        "font_color": getattr(style, "font_color", None),
        "highlight_color": getattr(style, "highlight_color", None),
        "caption_font_size": getattr(style, "font_size", None),
        "caption_position": getattr(style, "caption_position", None),
        "max_words_per_caption": getattr(style, "max_words_per_caption", None),
    }
    return {key: value for key, value in values.items() if value is not None}


def _validated_brand_logo(style: VideoStyle) -> tuple[Path | None, str | None]:
    if not style.logo_path:
        return None, None
    logo = Path(style.logo_path).expanduser().resolve()
    if (
        not logo.is_file()
        or logo.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
        or not 0 < logo.stat().st_size <= 20 * 1024 * 1024
    ):
        raise ValueError("El logo configurado no es accesible o supera los límites admitidos.")
    return logo, f"client-brand-logo{logo.suffix.lower()}"


def process_local_video_modal(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    job_id: str,
    paid_api_authorized: bool,
    style: VideoStyle,
    subtitle_mode: str = "none",
    render_mode: str = "full",
    render_settings: dict[str, Any] | None = None,
    creative_plan: dict[str, Any] | None = None,
    subtitle_words: list[WordTiming] | None = None,
    briefing: str = "",
    openai_client: Any = None,
    modal_client: ModalRenderClient | None = None,
    upload_to_drive: bool = False,
    drive_uploader: Any = None,
    drive_access_token: str | None = None,
    drive_source_key: str | None = None,
    config: PipelineConfig | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Render through the Modal worker without changing the local source."""
    if not paid_api_authorized:
        raise PermissionError("El render en Modal requiere autorización humana explícita.")
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        raise ValueError("Identificador de trabajo Modal no válido.")
    if subtitle_mode not in {"dynamic", "none"}:
        raise ValueError("El motor Remotion en Modal admite subtítulos dinámicos o ninguno.")
    if render_mode not in {"full", "quick_demo"}:
        raise ValueError("El modo de render debe ser full o quick_demo.")
    if render_settings is None:
        render_settings = {
            "auto_zoom": "smooth",
            "audio": {"original_volume": 1.0, "noise_reduction": False},
        }
    if (
        not isinstance(render_settings, dict)
        or render_settings.get("auto_zoom", "smooth") not in {"off", "smooth"}
        or not isinstance(render_settings.get("audio"), dict)
    ):
        raise ValueError("Las opciones de personalización no son válidas.")
    audio_settings = render_settings["audio"]
    if (
        isinstance(audio_settings.get("original_volume"), bool)
        or not isinstance(audio_settings.get("original_volume"), (int, float))
        or not 0 <= float(audio_settings["original_volume"]) <= 1
        or not isinstance(audio_settings.get("noise_reduction"), bool)
    ):
        raise ValueError("Los ajustes de audio no son válidos.")
    if not isinstance(briefing, str) or len(briefing) > 4000:
        raise ValueError("Las notas de estilo superan el límite de 4.000 caracteres.")
    try:
        validate_output_profile(
            style.output_width,
            style.output_height,
            style.output_fps,
        )
    except StyleConfigError as exc:
        raise ValueError(str(exc)) from None
    logo_source, logo_asset = _validated_brand_logo(style)

    source = Path(input_path).expanduser().resolve()
    destination_dir = Path(output_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    if progress_callback:
        progress_callback("validating")
    video = probe_video(source)
    _validate_remote_input_profile(source)
    try:
        validate_output_profile(
            style.output_width,
            style.output_height,
            style.output_fps,
            source_width=video.width,
            source_height=video.height,
        )
    except StyleConfigError as exc:
        raise ValueError(str(exc)) from None
    if video.video_codec not in {"h264", "hevc"}:
        raise ValueError("Modal solo admite vídeo H.264 o HEVC/H.265.")
    if video.duration > style.max_video_duration_seconds:
        raise ValueError(
            f"El vídeo supera el límite configurado de {style.max_video_duration_seconds} segundos."
        )
    if subtitle_mode == "dynamic" and not video.has_audio:
        raise ValueError("Para generar subtítulos dinámicos se necesita una pista de audio.")

    try:
        plan = normalize_edit_plan(creative_plan, video.duration)
    except EditPlanError as exc:
        raise ValueError(str(exc)) from None

    if (
        subtitle_mode == "dynamic"
        and subtitle_words is None
        and not os.getenv("OPENAI_API_KEY")
        and openai_client is None
    ):
        raise RuntimeError("OPENAI_API_KEY no está configurada; no se inició transcripción.")
    settings = config or PipelineConfig()
    if upload_to_drive and not settings.processed_folder_id:
        raise RuntimeError("No hay carpeta 02_PROCESADOS configurada.")
    drive_token = drive_access_token or os.getenv("GOOGLE_OAUTH_TOKEN")
    if upload_to_drive and not drive_token and drive_uploader is None:
        raise RuntimeError("La sesión de Google Drive no está autorizada para guardar el resultado.")

    source_hash = _sha256_file(source)
    expected_duration = edited_duration(video.duration, plan["cuts"])
    expected_render_duration = (
        min(expected_duration, 5.0) if render_mode == "quick_demo" else expected_duration
    )
    render_frames = (
        max(1, round(expected_render_duration * style.output_fps))
        if render_mode == "quick_demo"
        else None
    )
    expected_style = replace(style, output_quality="high")
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", source.stem).strip("-_")[:60] or "video"
    output_path = destination_dir / f"{safe_stem}-MATRIX-MOTION-{job_id[:8]}.mp4"
    if output_path.exists():
        raise FileExistsError("Se rechaza sobrescribir un resultado anterior.")

    renderer = modal_client or ModalRenderClient()
    # Fail before OpenAI transcription when the serverless worker is not deployed or accessible.
    renderer.ensure_worker_available()

    words: list[WordTiming] = []
    if subtitle_mode == "dynamic":
        if subtitle_words is not None:
            words = list(subtitle_words)
            _validate_supplied_word_timings(words, video.duration)
        else:
            if progress_callback:
                progress_callback("transcribing")
            with temporary_work_directory() as transcription_dir:
                words = transcribe_video(
                    video,
                    transcription_dir,
                    model=settings.openai_model,
                    client=openai_client,
                )
        if not words:
            raise RuntimeError("No se obtuvieron palabras con marcas temporales válidas para los subtítulos.")

    transcript = _render_transcript_payload(words)
    effect_cues = []
    for sticker in plan["stickers"]:
        source_end = sticker["start_seconds"] + sticker["duration_seconds"]
        retained = retained_source_ranges(
            sticker["start_seconds"],
            source_end,
            plan["cuts"],
        )
        if (
            len(retained) != 1
            or abs(retained[0][0] - sticker["start_seconds"]) > 0.01
            or abs(retained[0][1] - source_end) > 0.01
        ):
            continue
        name = sticker["sticker"]
        effect_cues.append(
            {
                "effect": name,
                "start_seconds": round(
                    map_source_time_to_output(sticker["start_seconds"], plan["cuts"]),
                    3,
                ),
                "duration_seconds": sticker["duration_seconds"],
                "position": sticker["position"],
                "assets": [name],
            }
        )
    modal_plan = {
        "cuts": plan["cuts"],
        "zooms": plan["zooms"],
        "effect_cues": effect_cues,
        "expected_output_duration_seconds": expected_duration,
        "output_width": expected_style.output_width,
        "output_height": expected_style.output_height,
        "output_fps": expected_style.output_fps,
        "overlays": (
            {"brand_logo": {"asset": logo_asset}}
            if logo_asset
            else {}
        ),
        "render_mode": render_mode,
        "render_settings": render_settings,
    }
    if render_frames is not None:
        modal_plan["render_frames"] = render_frames
    with temporary_work_directory() as work_dir:
        effects_dir = Path(work_dir) / "effects"
        effects_dir.mkdir(parents=True, exist_ok=True)
        if logo_source and logo_asset:
            copyfile(logo_source, effects_dir / logo_asset)
        for sticker in plan["stickers"]:
            (effects_dir / f"{sticker['sticker']}.svg").write_text(
                sticker_svg(sticker["sticker"], sticker["color"]),
                encoding="utf-8",
            )
        if progress_callback:
            progress_callback("rendering_on_modal")
        remote = renderer.render(
            job_id=job_id,
            input_path=source,
            transcript=transcript,
            plan=modal_plan,
            effects_dir=effects_dir,
            output_path=output_path,
            show_subtitles=subtitle_mode == "dynamic",
            briefing=briefing,
            caption_style=_modal_caption_style(expected_style),
            progress_callback=progress_callback,
        )
        if progress_callback:
            progress_callback("validating_output")
        validated = validate_rendered_output(
            video,
            remote.output_path,
            expected_style,
            expected_duration_seconds=expected_render_duration,
        )

    if _sha256_file(source) != source_hash:
        raise RuntimeError("El original cambió durante el procesamiento; permanece intacto.")

    drive_file = None
    if upload_to_drive:
        uploader = drive_uploader
        if uploader is None:
            from .drive import upload_new_file

            uploader = upload_new_file
        drive_file = uploader(
            remote.output_path,
            folder_id=settings.processed_folder_id,
            access_token=drive_token,
            app_properties={
                **({"matrixMotionSource": drive_source_key} if drive_source_key else {}),
                "matrixMotionExecution": f"modal:{job_id}",
            },
        )

    if progress_callback:
        progress_callback("completed")
    return {
        "source": str(source),
        "output": str(remote.output_path),
        "execution_key": f"modal:{job_id}",
        "drive_output": drive_file,
        "drive_upload_skipped": not upload_to_drive,
        "duration_seconds": video.duration,
        "source_dimensions": {"width": video.width, "height": video.height},
        "source_video_codec": video.video_codec,
        "source_audio_codec": video.audio_codec,
        "source_fps": video.fps,
        "output_dimensions": {"width": validated.width, "height": validated.height},
        "output_video_codec": validated.video_codec,
        "output_audio_codec": validated.audio_codec,
        "output_fps": validated.fps,
        "output_quality_preset": "Remotion H.264 CRF 15, preset medium",
        "output_video_bit_rate_bps": validated.video_bit_rate_bps,
        "output_format_bit_rate_bps": validated.format_bit_rate_bps,
        "output_duration_seconds": validated.duration,
        "expected_output_duration_seconds": expected_duration,
        "render_mode": render_mode,
        "rendered_duration_scope": (
            "primeros 5 segundos para revisión" if render_mode == "quick_demo" else "vídeo completo"
        ),
        "transcribed_word_count": len(words),
        "transcription_reused": subtitle_words is not None,
        "subtitle_mode": subtitle_mode,
        "subtitle_segment_count": len(build_subtitle_segments(words, video_duration=video.duration)),
        "creative_plan": plan,
        "render_id": remote.remote_job_id,
        "render_status_verified": True,
        "renderer_backend": "modal",
        "modal_elapsed_seconds": round(remote.elapsed_seconds, 3),
        "modal_output_sha256": remote.sha256,
        "modal_remote_size_bytes": remote.size_bytes,
        "modal_temporary_storage_cleanup": remote.temporary_storage_cleaned,
        "source_modified": False,
        "conversion": {
            "audio_normalization": "AAC stereo 48 kHz only when the source track requires it",
            "color_profiles": ["HLG BT.2020 limited", "SDR BT.709"],
            "output_color": "BT.709",
        },
        "estimated_cost_usd": None,
        "cost_estimate_note": (
            "Coste de cómputo Modal variable y no cotizado por el panel; "
            "requiere autorización humana antes de cada ejecución."
        ),
        "paid_operations": {
            "openai_transcription": subtitle_mode == "dynamic" and subtitle_words is None,
            "modal_render": True,
            "google_drive_upload": bool(upload_to_drive),
        },
    }
