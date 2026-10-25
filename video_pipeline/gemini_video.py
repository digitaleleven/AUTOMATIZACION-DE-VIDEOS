"""Private Gemini video analysis with validated timestamps and no raw API logging."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any

import requests

from .edit_plan import EditPlanError, normalize_edit_plan
from .models import WordTiming

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.8-flash"
FILES_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_ANALYSIS_BYTES = 1024 * 1024 * 1024
MAX_WORDS = 12_000

_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "spoken_language": {"type": "STRING"},
        "words": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "start_seconds": {"type": "NUMBER"},
                    "end_seconds": {"type": "NUMBER"},
                },
                "required": ["text", "start_seconds", "end_seconds"],
            },
        },
        "suggested_cuts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start_seconds": {"type": "NUMBER"},
                    "end_seconds": {"type": "NUMBER"},
                    "reason": {"type": "STRING"},
                },
                "required": ["start_seconds", "end_seconds", "reason"],
            },
        },
        "warnings": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary", "spoken_language", "words", "suggested_cuts", "warnings"],
}


class GeminiAnalysisError(RuntimeError):
    """Raised for missing Gemini configuration or invalid provider output."""


def validate_gemini_analysis(raw: Any, duration_seconds: float) -> dict[str, Any]:
    """Validate untrusted model JSON; never treat its cut ideas as applied edits."""
    if not isinstance(raw, dict):
        raise GeminiAnalysisError("Gemini devolvió una respuesta que no es un objeto JSON.")
    summary = raw.get("summary", "")
    language = raw.get("spoken_language", "")
    words_raw = raw.get("words")
    cuts_raw = raw.get("suggested_cuts")
    warnings_raw = raw.get("warnings")
    if (
        not isinstance(summary, str) or len(summary) > 1500
        or not isinstance(language, str) or len(language) > 80
        or not isinstance(words_raw, list) or len(words_raw) > MAX_WORDS
        or not isinstance(cuts_raw, list) or len(cuts_raw) > 12
        or not isinstance(warnings_raw, list) or len(warnings_raw) > 20
    ):
        raise GeminiAnalysisError("Gemini devolvió campos fuera de los límites permitidos.")

    words: list[dict[str, Any]] = []
    previous_start = -1.0
    for index, item in enumerate(words_raw):
        if not isinstance(item, dict):
            raise GeminiAnalysisError(f"El timestamp de palabra {index + 1} no es válido.")
        text = item.get("text")
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.strip()) > 80
            or any(character.isspace() for character in text.strip())
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
        ):
            raise GeminiAnalysisError(f"El timestamp de palabra {index + 1} no es válido.")
        start_value, end_value = float(start), float(end)
        if (
            start_value < 0
            or end_value <= start_value
            or end_value > duration_seconds + 0.05
            or start_value < previous_start
        ):
            raise GeminiAnalysisError(f"El timestamp de palabra {index + 1} queda fuera del vídeo o desordenado.")
        words.append({
            "text": text.strip(),
            "start": round(start_value, 3),
            "end": round(min(end_value, duration_seconds), 3),
        })
        previous_start = start_value

    suggested_cuts: list[dict[str, Any]] = []
    for index, item in enumerate(cuts_raw):
        if not isinstance(item, dict):
            raise GeminiAnalysisError(f"La sugerencia de corte {index + 1} no es válida.")
        reason = item.get("reason")
        if not isinstance(reason, str) or len(reason) > 240:
            raise GeminiAnalysisError(f"La justificación del corte {index + 1} no es válida.")
        suggested_cuts.append({
            "start_seconds": item.get("start_seconds"),
            "end_seconds": item.get("end_seconds"),
        })
    try:
        normalized_cuts = normalize_edit_plan(
            {"cuts": suggested_cuts, "zooms": [], "stickers": []},
            duration_seconds,
        )["cuts"]
    except (EditPlanError, TypeError, ValueError):
        raise GeminiAnalysisError("Las sugerencias de corte de Gemini no superaron la validación temporal.") from None
    for cut, source in zip(normalized_cuts, cuts_raw, strict=True):
        cut["reason"] = source["reason"].strip()

    warnings = []
    for warning in warnings_raw:
        if not isinstance(warning, str) or len(warning) > 400:
            raise GeminiAnalysisError("Gemini devolvió una advertencia no válida.")
        warnings.append(warning.strip())
    return {
        "summary": summary.strip(),
        "spoken_language": language.strip(),
        "words": words,
        "suggested_cuts": normalized_cuts,
        "warnings": warnings,
    }


def _provider_error(response: requests.Response, stage: str) -> GeminiAnalysisError:
    logger.warning("gemini_request_rejected stage=%s status=%s", stage, response.status_code)
    return GeminiAnalysisError(
        f"Gemini rechazó la solicitud durante {stage} (HTTP {response.status_code}); no se registró el cuerpo de respuesta."
    )


def analyze_video(
    video_path: str | Path,
    *,
    duration_seconds: float,
    briefing: str = "",
    timeout_seconds: int = 180,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Upload a normalized proxy, request structured analysis, and delete the remote file."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise GeminiAnalysisError("GEMINI_API_KEY no está configurada; no se envió el vídeo.")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise GeminiAnalysisError("La duración del vídeo para Gemini no es válida.")
    if not isinstance(briefing, str) or len(briefing) > 4000:
        raise GeminiAnalysisError("Las instrucciones para Gemini superan el límite permitido.")

    video = Path(video_path).expanduser().resolve()
    if not video.is_file() or video.suffix.lower() not in {".mp4", ".mov"}:
        raise GeminiAnalysisError("La copia normalizada de análisis no existe o no es un vídeo compatible.")
    size = video.stat().st_size
    if size <= 0 or size > MAX_ANALYSIS_BYTES:
        raise GeminiAnalysisError("El vídeo normalizado supera el límite seguro de análisis.")

    http = session or requests.Session()
    headers = {"x-goog-api-key": api_key}
    display_name = f"matrix-motion-{int(time.time())}-{video.stat().st_size}.mp4"
    remote_name: str | None = None
    started = time.monotonic()
    result: dict[str, Any] | None = None
    try:
        upload_start = http.post(
            FILES_UPLOAD_URL,
            headers={
                **headers,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": display_name}},
            timeout=(15, 45),
        )
        if not upload_start.ok:
            raise _provider_error(upload_start, "la carga del vídeo")
        upload_url = upload_start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise GeminiAnalysisError("Google no devolvió una URL de carga temporal.")

        with video.open("rb") as source:
            uploaded = http.post(
                upload_url,
                headers={
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                    "Content-Type": "video/mp4",
                },
                data=source,
                timeout=(30, max(timeout_seconds, 300)),
            )
        if not uploaded.ok:
            raise _provider_error(uploaded, "la finalización de la carga")
        file_record = uploaded.json()
        file_info = file_record.get("file", file_record) if isinstance(file_record, dict) else {}
        remote_name = file_info.get("name")
        file_uri = file_info.get("uri")
        mime_type = file_info.get("mimeType", "video/mp4")
        if not isinstance(remote_name, str) or not re.fullmatch(r"files/[A-Za-z0-9_-]+", remote_name):
            raise GeminiAnalysisError("Google no devolvió un identificador de archivo válido.")
        if not isinstance(file_uri, str) or not file_uri.startswith("https://"):
            raise GeminiAnalysisError("Google no devolvió un URI de archivo válido.")

        deadline = time.monotonic() + min(max(timeout_seconds, 30), 300)
        while file_info.get("state") == "PROCESSING" and time.monotonic() < deadline:
            time.sleep(2)
            metadata = http.get(f"{API_BASE_URL}/{remote_name}", headers=headers, timeout=(10, 20))
            if not metadata.ok:
                raise _provider_error(metadata, "la comprobación del archivo")
            file_info = metadata.json()
        if file_info.get("state") == "FAILED":
            raise GeminiAnalysisError("Google no pudo procesar el proxy de vídeo.")
        if file_info.get("state") not in {None, "ACTIVE"}:
            raise GeminiAnalysisError("El proxy de vídeo no quedó disponible para análisis dentro del plazo.")

        prompt = (
            "Analiza este vídeo vertical corto en español. Devuelve solo el JSON solicitado. "
            "Transcribe las palabras audibles con timestamps palabra por palabra, en segundos "
            "desde el inicio del archivo; no inventes ni completes palabras que no se entiendan. "
            "Propón como máximo 12 intervalos eliminables, exclusivamente pausas muertas o repeticiones "
            "prescindibles de al menos 0,05 s; no elimines habla importante. Las sugerencias son para "
            "revisión humana y no deben tratarse como cortes ya aprobados. Conserva el contenido y sentido. "
            f"Duración aproximada del archivo: {duration_seconds:.3f} s. "
            f"Instrucciones del cliente: {briefing.strip() or 'sin instrucciones adicionales'}."
        )
        response = http.post(
            f"{API_BASE_URL}/models/{GEMINI_MODEL}:generateContent",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
                    ],
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "responseSchema": _ANALYSIS_SCHEMA,
                },
            },
            timeout=(20, max(timeout_seconds, 180)),
        )
        if not response.ok:
            raise _provider_error(response, "el análisis multimodal")
        body = response.json()
        candidates = body.get("candidates", [])
        parts = (
            candidates[0].get("content", {}).get("parts", [])
            if candidates and isinstance(candidates[0], dict)
            else []
        )
        content = next(
            (part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)),
            None,
        )
        if not content:
            raise GeminiAnalysisError("Gemini no devolvió texto JSON utilizable.")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            raise GeminiAnalysisError("Gemini devolvió JSON malformado; no se usará para el render.") from None
        analysis = validate_gemini_analysis(parsed, duration_seconds)
        usage = body.get("usageMetadata", {})
        elapsed = round(time.monotonic() - started, 3)
        result = {
            **analysis,
            "model": GEMINI_MODEL,
            "usage": {
                "prompt_tokens": usage.get("promptTokenCount"),
                "output_tokens": usage.get("candidatesTokenCount"),
                "total_tokens": usage.get("totalTokenCount"),
            },
            "processing_seconds": elapsed,
            "input_video_bytes": size,
            "remote_file_deleted": None,
        }
        logger.info(
            "gemini_video_analysis_completed model=%s words=%s cuts=%s duration_seconds=%s",
            GEMINI_MODEL,
            len(analysis["words"]),
            len(analysis["suggested_cuts"]),
            elapsed,
        )
    except requests.RequestException as exc:
        logger.warning("gemini_video_analysis_network_error type=%s", type(exc).__name__)
        raise GeminiAnalysisError("No se pudo contactar con Gemini; no se registraron credenciales ni contenido.") from None
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GeminiAnalysisError):
            raise
        logger.warning("gemini_video_analysis_response_invalid type=%s", type(exc).__name__)
        raise GeminiAnalysisError("La respuesta de Gemini no tuvo la estructura esperada.") from None
    finally:
        if remote_name:
            try:
                deletion = http.delete(
                    f"{API_BASE_URL}/{remote_name}",
                    headers=headers,
                    timeout=(10, 30),
                )
                if deletion.ok:
                    logger.info("gemini_temporary_file_deleted")
                    if result is not None:
                        result["remote_file_deleted"] = True
                else:
                    logger.warning("gemini_temporary_file_delete_failed status=%s", deletion.status_code)
                    if result is not None:
                        result["remote_file_deleted"] = False
            except requests.RequestException as exc:
                logger.warning("gemini_temporary_file_delete_error type=%s", type(exc).__name__)
                if result is not None:
                    result["remote_file_deleted"] = False
    if result is None:
        raise GeminiAnalysisError("Gemini no devolvió un resultado utilizable.")
    return result
