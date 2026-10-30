"""Conservative per-job budget guards for the Modal/Gemini pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import math


MAX_JOB_BUDGET_USD = 1.50
MAX_GEMINI_INPUT_TOKENS = 150_000
MAX_GEMINI_OUTPUT_TOKENS = 4_096
# Static planning rates only; the app cannot query live provider/account billing here.
GEMINI_INPUT_USD_PER_MILLION = 0.75
GEMINI_OUTPUT_USD_PER_MILLION = 3.75
WHISPER_USD_PER_AUDIO_MINUTE = 0.006
MODAL_CPU_USD_PER_CORE_SECOND = 0.0000131
MODAL_MEMORY_USD_PER_GIB_SECOND = 0.00000222
MODAL_RENDER_CPU_CORES = 4
MODAL_RENDER_MEMORY_GIB = 16
MAX_NORMALIZATION_SECONDS = 20 * 60
MAX_RENDER_SECONDS = 60 * 60
MAX_NORMALIZED_PROXY_GIB = 2
MAX_4K_NORMALIZED_PROXY_GIB = 8
MAX_RENDER_OUTPUT_GIB = 4
MAX_BUDGETED_VIDEO_SECONDS = 1800
MODAL_EGRESS_USD_PER_GIB = 0.04
MODAL_VOLUME_USD_PER_GIB_MONTH = 0.09
R2_STORAGE_USD_PER_GIB_MONTH = 0.015
MISCELLANEOUS_BUDGET_BUFFER_USD = 0.20


@dataclass(frozen=True)
class JobBudgetEstimate:
    maximum_usd: float
    modal_compute_usd: float
    gemini_usd: float
    whisper_usd: float
    modal_egress_usd: float
    persistent_storage_usd: float
    buffer_usd: float


@dataclass(frozen=True)
class NormalizationBudgetEstimate:
    maximum_usd: float
    modal_compute_usd: float
    modal_egress_usd: float
    persistent_storage_usd: float
    buffer_usd: float


@dataclass(frozen=True)
class RecoveryBudgetEstimate:
    """Estimate only the remaining Gemini, Modal render, egress, and R2 work."""

    maximum_usd: float
    modal_render_usd: float
    gemini_usd: float
    modal_egress_usd: float
    persistent_storage_usd: float
    buffer_usd: float


def estimate_gemini_cost_usd(input_tokens: int, output_tokens: int) -> float:
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or not 0 <= input_tokens <= MAX_GEMINI_INPUT_TOKENS
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or not 0 <= output_tokens <= MAX_GEMINI_OUTPUT_TOKENS
    ):
        raise ValueError("El consumo de tokens supera el límite de presupuesto autorizado.")
    return (
        input_tokens * GEMINI_INPUT_USD_PER_MILLION
        + output_tokens * GEMINI_OUTPUT_USD_PER_MILLION
    ) / 1_000_000


def estimate_job_budget(duration_seconds: float) -> JobBudgetEstimate:
    if isinstance(duration_seconds, bool) or not math.isfinite(float(duration_seconds)) or duration_seconds <= 0:
        raise ValueError("La duración del vídeo no permite estimar el presupuesto.")
    if duration_seconds > MAX_BUDGETED_VIDEO_SECONDS:
        raise ValueError("El límite presupuestario admite vídeos de hasta 30 minutos.")
    compute_seconds = MAX_NORMALIZATION_SECONDS + MAX_RENDER_SECONDS
    modal_compute = compute_seconds * (
        MODAL_RENDER_CPU_CORES * MODAL_CPU_USD_PER_CORE_SECOND
        + MODAL_RENDER_MEMORY_GIB * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    gemini = estimate_gemini_cost_usd(
        MAX_GEMINI_INPUT_TOKENS,
        MAX_GEMINI_OUTPUT_TOKENS,
    )
    whisper = float(duration_seconds) / 60 * WHISPER_USD_PER_AUDIO_MINUTE
    egress = (
        MAX_NORMALIZED_PROXY_GIB + MAX_RENDER_OUTPUT_GIB
    ) * MODAL_EGRESS_USD_PER_GIB
    # Budget one full month for the retained normalization cache and final object.
    storage = (
        MAX_NORMALIZED_PROXY_GIB * MODAL_VOLUME_USD_PER_GIB_MONTH
        + MAX_RENDER_OUTPUT_GIB * R2_STORAGE_USD_PER_GIB_MONTH
    )
    maximum = modal_compute + gemini + whisper + egress + storage + MISCELLANEOUS_BUDGET_BUFFER_USD
    return JobBudgetEstimate(
        maximum_usd=round(maximum, 4),
        modal_compute_usd=round(modal_compute, 4),
        gemini_usd=round(gemini, 4),
        whisper_usd=round(whisper, 4),
        modal_egress_usd=round(egress, 4),
        persistent_storage_usd=round(storage, 4),
        buffer_usd=MISCELLANEOUS_BUDGET_BUFFER_USD,
    )


def estimate_recovery_budget() -> RecoveryBudgetEstimate:
    """Conservatively estimate a resumed render; Whisper and normalization are excluded."""
    modal_render = MAX_RENDER_SECONDS * (
        MODAL_RENDER_CPU_CORES * MODAL_CPU_USD_PER_CORE_SECOND
        + MODAL_RENDER_MEMORY_GIB * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    gemini = estimate_gemini_cost_usd(
        MAX_GEMINI_INPUT_TOKENS,
        MAX_GEMINI_OUTPUT_TOKENS,
    )
    egress = MAX_RENDER_OUTPUT_GIB * MODAL_EGRESS_USD_PER_GIB
    storage = MAX_RENDER_OUTPUT_GIB * R2_STORAGE_USD_PER_GIB_MONTH
    maximum = modal_render + gemini + egress + storage + MISCELLANEOUS_BUDGET_BUFFER_USD
    return RecoveryBudgetEstimate(
        maximum_usd=round(maximum, 4),
        modal_render_usd=round(modal_render, 4),
        gemini_usd=round(gemini, 4),
        modal_egress_usd=round(egress, 4),
        persistent_storage_usd=round(storage, 4),
        buffer_usd=MISCELLANEOUS_BUDGET_BUFFER_USD,
    )


def estimate_4k_normalization_budget() -> NormalizationBudgetEstimate:
    """Conservative static estimate for one 20-minute 4K normalization."""
    modal_compute = MAX_NORMALIZATION_SECONDS * (
        MODAL_RENDER_CPU_CORES * MODAL_CPU_USD_PER_CORE_SECOND
        + MODAL_RENDER_MEMORY_GIB * MODAL_MEMORY_USD_PER_GIB_SECOND
    )
    egress = MAX_4K_NORMALIZED_PROXY_GIB * MODAL_EGRESS_USD_PER_GIB
    storage = MAX_4K_NORMALIZED_PROXY_GIB * MODAL_VOLUME_USD_PER_GIB_MONTH
    maximum = modal_compute + egress + storage + MISCELLANEOUS_BUDGET_BUFFER_USD
    return NormalizationBudgetEstimate(
        maximum_usd=round(maximum, 4),
        modal_compute_usd=round(modal_compute, 4),
        modal_egress_usd=round(egress, 4),
        persistent_storage_usd=round(storage, 4),
        buffer_usd=MISCELLANEOUS_BUDGET_BUFFER_USD,
    )


def enforce_4k_normalization_budget() -> NormalizationBudgetEstimate:
    estimate = estimate_4k_normalization_budget()
    if estimate.maximum_usd > MAX_JOB_BUDGET_USD:
        raise RuntimeError(
            f"El coste estimado de normalización 4K (${estimate.maximum_usd:.4f}) "
            f"supera el límite interno de ${MAX_JOB_BUDGET_USD:.2f}; "
            "se bloqueó la conversión."
        )
    return estimate


def enforce_job_budget(duration_seconds: float) -> JobBudgetEstimate:
    estimate = estimate_job_budget(duration_seconds)
    if estimate.maximum_usd > MAX_JOB_BUDGET_USD:
        raise RuntimeError(
            f"El coste máximo conservador (${estimate.maximum_usd:.4f}) supera "
            f"el límite interno de ${MAX_JOB_BUDGET_USD:.2f}; se bloqueó el procesamiento."
        )
    return estimate


def enforce_recovery_budget() -> RecoveryBudgetEstimate:
    estimate = estimate_recovery_budget()
    if estimate.maximum_usd > MAX_JOB_BUDGET_USD:
        raise RuntimeError(
            f"El coste estimado de recuperación (${estimate.maximum_usd:.4f}) supera "
            f"el límite interno de ${MAX_JOB_BUDGET_USD:.2f}; se bloqueó la recuperación."
        )
    return estimate
