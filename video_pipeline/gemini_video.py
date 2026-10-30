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
from .cost_controls import (
    MAX_GEMINI_INPUT_TOKENS,
    MAX_GEMINI_OUTPUT_TOKENS,
    estimate_gemini_cost_usd,
)
from .models import WordTiming

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-3.8-flash"
FILES_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_ANALYSIS_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORDS = 12_000

_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "key_moments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start_seconds": {"type": "NUMBER"},
                    "end_seconds": {"type": "NUMBER"},
                    "label": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["start_seconds", "end_seconds", "label", "reason"],
            },
        },
        "suggested_zooms": {
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
        "copy_and_seo": {
            "type": "OBJECT",
            "properties": {
                "hook": {"type": "STRING"},
                "caption": {"type": "STRING"},
                "seo_title": {"type": "STRING"},
                "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["hook", "caption", "seo_title", "hashtags"],
        },
        "warnings": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary", "key_moments", "suggested_zooms", "suggested_cuts", "copy_and_seo", "warnings"],
}


class GeminiAnalysisError(RuntimeError):
    """Raised for missing Gemini configuration or invalid provider output."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "validation",
        phase: str = "request_validation",
        http_status: int | None = None,
        provider_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = {
            "provider": "gemini",
            "category": category,
            "phase": phase,
            "http_status": http_status,
            "provider_status": provider_status,
        }


def validate_gemini_analysis(raw: Any, duration_seconds: float) -> dict[str, Any]:
    """Validate visual moments, zoom windows, human-reviewed cuts and social copy."""
    if not isinstance(raw, dict):
        raise GeminiAnalysisError("Gemini devolvió una respuesta que no es un objeto JSON.")
    summary = raw.get("summary", "")
    moments_raw = raw.get("key_moments")
    zooms_raw = raw.get("suggested_zooms")
    cuts_raw = raw.get("suggested_cuts")
    copy_raw = raw.get("copy_and_seo")
    warnings_raw = raw.get("warnings")
    if (
        not isinstance(summary, str) or len(summary) > 1500
        or not isinstance(moments_raw, list) or len(moments_raw) > 12
        or not isinstance(zooms_raw, list) or len(zooms_raw) > 8
        or not isinstance(cuts_raw, list) or len(cuts_raw) > 12
        or not isinstance(copy_raw, dict)
        or not isinstance(warnings_raw, list) or len(warnings_raw) > 20
    ):
        raise GeminiAnalysisError("Gemini devolvió campos fuera de los límites permitidos.")

    def normalize_intervals(items: list[Any], field: str, max_items: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise GeminiAnalysisError(f"La sugerencia {field} {index + 1} no es válida.")
            start, end = item.get("start_seconds"), item.get("end_seconds")
            reason = item.get("reason")
            if (
                isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(float(start)) or not math.isfinite(float(end))
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 240
                or float(start) < 0 or float(end) <= float(start)
                or float(end) > duration_seconds + 0.05
            ):
                raise GeminiAnalysisError(f"La sugerencia {field} {index + 1} queda fuera de límites.")
            normalized.append({
                "start_seconds": round(float(start), 3),
                "end_seconds": round(min(float(end), duration_seconds), 3),
                "reason": reason.strip(),
                **({"label": str(item.get("label", "")).strip()[:80]} if field == "key_moments" else {}),
            })
        if len(normalized) > max_items:
            raise GeminiAnalysisError(f"Gemini excedió el máximo de sugerencias {field}.")
        normalized.sort(key=lambda item: item["start_seconds"])
        for left, right in zip(normalized, normalized[1:]):
            if right["start_seconds"] < left["end_seconds"]:
                raise GeminiAnalysisError(f"Las sugerencias {field} se solapan.")
        return normalized

    moments = normalize_intervals(moments_raw, "key_moments", 12)
    zooms_raw_normalized = normalize_intervals(zooms_raw, "suggested_zooms", 8)
    cuts_raw_normalized = normalize_intervals(cuts_raw, "suggested_cuts", 12)
    try:
        normalized_zooms = normalize_edit_plan(
            {
                "cuts": [],
                "zooms": [
                    {key: value for key, value in item.items() if key != "reason"} | {"effect": "zoomIn"}
                    for item in zooms_raw_normalized
                ],
                "stickers": [],
            },
            duration_seconds,
        )["zooms"]
        normalized_cuts = normalize_edit_plan(
            {"cuts": [{key: value for key, value in item.items() if key != "reason"} for item in cuts_raw_normalized],
             "zooms": [], "stickers": []},
            duration_seconds,
        )["cuts"]
    except (EditPlanError, TypeError, ValueError):
        raise GeminiAnalysisError("Las sugerencias temporales de Gemini no superaron la validación.") from None
    for item, source in zip(normalized_zooms, zooms_raw_normalized, strict=True):
        item["reason"] = source["reason"]
    for item, source in zip(normalized_cuts, cuts_raw_normalized, strict=True):
        item["reason"] = source["reason"]

    hook = copy_raw.get("hook")
    caption = copy_raw.get("caption")
    seo_title = copy_raw.get("seo_title")
    hashtags = copy_raw.get("hashtags")
    if (
        not isinstance(hook, str) or len(hook) > 160
        or not isinstance(caption, str) or len(caption) > 600
        or not isinstance(seo_title, str) or len(seo_title) > 180
        or not isinstance(hashtags, list) or len(hashtags) > 12
        or any(not isinstance(tag, str) or len(tag) > 50 or not tag.startswith("#") for tag in hashtags)
    ):
        raise GeminiAnalysisError("Gemini devolvió copy/SEO fuera de los límites permitidos.")

    warnings = []
    for warning in warnings_raw:
        if not isinstance(warning, str) or len(warning) > 400:
            raise GeminiAnalysisError("Gemini devolvió una advertencia no válida.")
        warnings.append(warning.strip())
    return {
        "summary": summary.strip(),
        "key_moments": moments,
        "suggested_zooms": normalized_zooms,
        "suggested_cuts": normalized_cuts,
        "copy_and_seo": {
            "hook": hook.strip(),
            "caption": caption.strip(),
            "seo_title": seo_title.strip(),
            "hashtags": [tag.strip() for tag in hashtags],
        },
        "warnings": warnings,
    }


def _provider_error(response: requests.Response, stage: str) -> GeminiAnalysisError:
    provider_status = None
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        candidate = error.get("status") if isinstance(error, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", candidate):
            provider_status = candidate
    except (ValueError, TypeError, AttributeError):
        pass
    category = {
        "UNAUTHENTICATED": "authentication",
        "PERMISSION_DENIED": "permission",
        "INVALID_ARGUMENT": "invalid_request",
        "NOT_FOUND": "not_found",
        "RESOURCE_EXHAUSTED": "quota_or_rate_limit",
        "DEADLINE_EXCEEDED": "timeout",
        "UNAVAILABLE": "provider_unavailable",
        "INTERNAL": "provider_error",
    }.get(provider_status)
    if category is None:
        status_code = int(response.status_code)
        category = {
            400: "invalid_request",
            401: "authentication",
            403: "permission",
            404: "not_found",
            408: "timeout",
            429: "quota_or_rate_limit",
        }.get(status_code, "provider_error" if status_code >= 500 else "provider_rejected")
    logger.warning(
        "gemini_request_rejected phase=%s http_status=%s category=%s provider_status=%s",
        stage,
        response.status_code,
        category,
        provider_status or "unavailable",
    )
    return GeminiAnalysisError(
        f"Gemini rechazó la solicitud durante {stage} "
        f"(HTTP {response.status_code}, categoría {category}); no se registró el cuerpo de respuesta.",
        category=category,
        phase=stage,
        http_status=int(response.status_code),
        provider_status=provider_status,
    )


def analyze_video(
    video_path: str | Path,
    *,
    duration_seconds: float,
    briefing: str = "",
    transcript_text: str = "",
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
    current_stage = "upload_start"
    try:
        current_stage = "upload_start"
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
            raise _provider_error(upload_start, "upload_start")
        upload_url = upload_start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise GeminiAnalysisError(
                "Google no devolvió una URL de carga temporal.",
                category="invalid_provider_response",
                phase=current_stage,
            )

        current_stage = "video_upload"
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
            raise _provider_error(uploaded, "video_upload")
        file_record = uploaded.json()
        file_info = file_record.get("file", file_record) if isinstance(file_record, dict) else {}
        remote_name = file_info.get("name")
        file_uri = file_info.get("uri")
        mime_type = file_info.get("mimeType", "video/mp4")
        if not isinstance(remote_name, str) or not re.fullmatch(r"files/[A-Za-z0-9_-]+", remote_name):
            raise GeminiAnalysisError(
                "Google no devolvió un identificador de archivo válido.",
                category="invalid_provider_response",
                phase=current_stage,
            )
        if not isinstance(file_uri, str) or not file_uri.startswith("https://"):
            raise GeminiAnalysisError(
                "Google no devolvió un URI de archivo válido.",
                category="invalid_provider_response",
                phase=current_stage,
            )

        deadline = time.monotonic() + min(max(timeout_seconds, 30), 300)
        current_stage = "file_processing"
        while file_info.get("state") == "PROCESSING" and time.monotonic() < deadline:
            time.sleep(2)
            metadata = http.get(f"{API_BASE_URL}/{remote_name}", headers=headers, timeout=(10, 20))
            if not metadata.ok:
                raise _provider_error(metadata, "file_processing")
            file_info = metadata.json()
        if file_info.get("state") == "FAILED":
            raise GeminiAnalysisError("Google no pudo procesar el proxy de vídeo.")
        if file_info.get("state") not in {None, "ACTIVE"}:
            raise GeminiAnalysisError("El proxy de vídeo no quedó disponible para análisis dentro del plazo.")

        transcript_context = transcript_text.strip()[:12000] or "No hay transcripción disponible."
        prompt = (
            "Analiza este vídeo vertical para una edición social profesional. Devuelve únicamente el JSON solicitado. "
            "NO transcribas el audio ni generes tiempos de palabras: la transcripción autorizada viene de Whisper. "
            "Usa la transcripción de referencia para contexto y copy, sin inventar nombres de productos, marcas, "
            "beneficios, precios ni promociones. Detecta hasta 12 momentos visuales relevantes con tiempos reales. "
            "Propón hasta 8 ventanas de zoom sutil (máximo 1.06x), sin movimientos bruscos y solo cuando el contenido "
            "lo justifique. Propón hasta 12 cortes candidatos de pausas muertas. El sistema solo aplicará los "
            "intervalos corroborados como silencio por el análisis local de audio y protegidos frente a palabras "
            "transcritas. Genera un hook, caption, título SEO y hasta 12 hashtags fieles al contenido. "
            f"Duración del proxy: {duration_seconds:.3f} s. "
            f"Transcripción Whisper de referencia: {transcript_context}. "
            f"Instrucciones del cliente: {briefing.strip() or 'sin instrucciones adicionales'}."
        )
        contents = [{
            "parts": [
                {"text": prompt},
                {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
            ],
        }]
        current_stage = "count_tokens"
        token_response = http.post(
            f"{API_BASE_URL}/models/{GEMINI_MODEL}:countTokens",
            headers={**headers, "Content-Type": "application/json"},
            json={"contents": contents},
            timeout=(20, 60),
        )
        if not token_response.ok:
            raise _provider_error(token_response, "count_tokens")
        token_body = token_response.json()
        try:
            input_tokens = int(token_body.get("totalTokens"))
        except (TypeError, ValueError):
            raise GeminiAnalysisError("Gemini no devolvió un recuento previo de tokens válido.") from None
        if not 0 < input_tokens <= MAX_GEMINI_INPUT_TOKENS:
            raise GeminiAnalysisError(
                "El vídeo o el contexto supera el límite de tokens autorizado; no se generó el análisis."
            )
        max_gemini_cost = estimate_gemini_cost_usd(
            input_tokens,
            MAX_GEMINI_OUTPUT_TOKENS,
        )
        generation_config = {
            "temperature": 0.1,
            "maxOutputTokens": MAX_GEMINI_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": _ANALYSIS_SCHEMA,
        }
        current_stage = "generate_content"
        response = http.post(
            f"{API_BASE_URL}/models/{GEMINI_MODEL}:generateContent",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "contents": contents,
                "generationConfig": generation_config,
            },
            timeout=(20, max(timeout_seconds, 180)),
        )
        if not response.ok:
            raise _provider_error(response, "generate_content")
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
        try:
            actual_input_tokens = int(usage.get("promptTokenCount", input_tokens))
            actual_output_tokens = int(usage.get("candidatesTokenCount", 0))
        except (TypeError, ValueError):
            raise GeminiAnalysisError("Gemini devolvió un consumo de tokens no válido.") from None
        if (
            actual_input_tokens > MAX_GEMINI_INPUT_TOKENS
            or actual_output_tokens > MAX_GEMINI_OUTPUT_TOKENS
        ):
            raise GeminiAnalysisError("El consumo informado de Gemini excedió el límite autorizado.")
        actual_gemini_cost = estimate_gemini_cost_usd(
            actual_input_tokens,
            actual_output_tokens,
        )
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
            "estimated_cost_usd": round(actual_gemini_cost, 6),
            "preflight_max_cost_usd": round(max_gemini_cost, 6),
            "input_video_bytes": size,
            "remote_file_deleted": None,
        }
        logger.info(
            "gemini_video_analysis_completed model=%s moments=%s zooms=%s cuts=%s duration_seconds=%s",
            GEMINI_MODEL,
            len(analysis["key_moments"]),
            len(analysis["suggested_zooms"]),
            len(analysis["suggested_cuts"]),
            elapsed,
        )
    except GeminiAnalysisError as exc:
        if exc.diagnostic.get("phase") == "request_validation":
            exc.diagnostic["phase"] = current_stage
            if current_stage == "generate_content":
                exc.diagnostic["category"] = "response_validation"
            elif current_stage == "count_tokens":
                exc.diagnostic["category"] = "token_preflight"
            elif current_stage == "file_processing":
                exc.diagnostic["category"] = "file_processing"
        raise
    except requests.RequestException as exc:
        logger.warning(
            "gemini_video_analysis_network_error phase=%s type=%s",
            current_stage,
            type(exc).__name__,
        )
        raise GeminiAnalysisError(
            "No se pudo contactar con Gemini; no se registraron credenciales ni contenido.",
            category="network_error",
            phase=current_stage,
        ) from None
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, GeminiAnalysisError):
            raise
        logger.warning(
            "gemini_video_analysis_response_invalid phase=%s type=%s",
            current_stage,
            type(exc).__name__,
        )
        raise GeminiAnalysisError(
            "La respuesta de Gemini no tuvo la estructura esperada.",
            category="invalid_provider_response",
            phase=current_stage,
        ) from None
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
