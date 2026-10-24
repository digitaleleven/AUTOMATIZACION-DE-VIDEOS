"""One-shot local MM-004 composer; no network or paid-provider code."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .semantic_effects import (
    build_semantic_overlay_filter,
    retained_source_ranges,
    semantic_overlay_input_specs,
)


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "generated-artifacts/drive-inspection/mm004/mm004-plan.json"
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _retained_ranges(plan: dict[str, Any]) -> list[tuple[float, float]]:
    return retained_source_ranges(plan["cuts"], float(plan["source_duration_seconds"]))


def build_command(plan: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    source = (ROOT / plan["source"]).resolve()
    output = (ROOT / plan["new_output"]).resolve()
    subtitle_file = ROOT / "generated-artifacts/drive-inspection/mm003/mm003-dynamic.ass"
    asset_dir = ROOT / "generated-artifacts/drive-inspection/mm004/effects"
    if not source.is_file() or source.suffix.lower() != ".mov":
        raise FileNotFoundError("El original MOV aprobado no está disponible.")
    if not subtitle_file.is_file():
        raise FileNotFoundError("No está disponible el archivo ASS de MM-003.")
    if output.exists():
        raise FileExistsError(f"Se rechaza sobrescribir una salida anterior: {output}")
    cues = plan.get("effect_cues")
    if not isinstance(cues, list) or not cues:
        raise ValueError("El plan MM-004 no contiene efectos semánticos comprobables.")
    expected_duration = float(plan["expected_output_duration_seconds"])
    specs = semantic_overlay_input_specs(
        cues,
        str(asset_dir),
        loop_duration_seconds=expected_duration,
    )
    for spec in specs:
        if not Path(spec["path"]).is_file():
            raise FileNotFoundError(f"Falta el SVG local aprobado: {spec['path']}")

    cut_pairs = _retained_ranges(plan)
    computed_duration = sum(end - start for start, end in cut_pairs)
    if abs(computed_duration - expected_duration) > 0.02:
        raise ValueError(
            f"Las duraciones de los tramos retenidos ({computed_duration:.3f}s) "
            f"no coinciden con el plan ({expected_duration:.3f}s)."
        )
    segments = []
    audio_segments = []
    for index, (start, end) in enumerate(cut_pairs):
        segments.append(
            f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
        )
        audio_segments.append(
            f"[0:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
    video_ends = "".join(f"[v{index}]" for index in range(len(cut_pairs)))
    audio_ends = "".join(f"[a{index}]" for index in range(len(cut_pairs)))
    video_base = (
        ";".join(segments)
        + f";{video_ends}concat=n={len(cut_pairs)}:v=1:a=0,"
        "zscale=primariesin=bt2020:transferin=arib-std-b67:matrixin=bt2020nc:"
        "rangein=limited:primaries=bt2020:transfer=linear:matrix=gbr:range=full:npl=100,"
        "format=gbrpf32le,tonemap=tonemap=hable:desat=0,"
        "zscale=primariesin=bt2020:transferin=linear:matrixin=gbr:rangein=full:"
        "primaries=bt709:transfer=bt709:matrix=bt709:range=limited:"
        "w=1080:h=1920:filter=lanczos,format=yuv420p,fps=30,setsar=1,"
        f"subtitles=filename='{subtitle_file}':fontsdir='{FONT_DIR}'[vbase]"
    )
    overlay_graph, final_video_label, overlay_count = build_semantic_overlay_filter(
        cues, first_input_index=1, base_label="vbase"
    )
    if overlay_count != len(specs):
        raise RuntimeError("El número de overlays no coincide con las entradas SVG.")
    audio_graph = (
        ";".join(audio_segments)
        + f";{audio_ends}concat=n={len(cut_pairs)}:v=0:a=1,"
        "aresample=48000,asetpts=PTS-STARTPTS[aout]"
    )
    filter_graph = ";".join((video_base, overlay_graph, audio_graph))

    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-nostdin",
        "-n",
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats_period",
        "5",
        "-i",
        str(source),
    ]
    for spec in specs:
        command.extend(
            [
                "-f",
                "image2",
                "-loop",
                "1",
                "-framerate",
                "30",
                "-t",
                f"{spec['stream_duration_seconds']:.3f}",
                "-i",
                spec["path"],
            ]
        )
    command.extend(
        [
            "-filter_complex",
            filter_graph,
            "-map",
            f"[{final_video_label}]",
            "-map",
            "[aout]",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "17",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command, specs


def _rss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return 0
    return 0


def run_once(plan: dict[str, Any], *, attempt_number: int = 1) -> dict[str, Any]:
    if attempt_number not in {1, 2, 3}:
        raise ValueError("MM-004 permite como máximo tres pasadas manuales y nunca reintenta automáticamente.")
    command, specs = build_command(plan)
    output = (ROOT / plan["new_output"]).resolve()
    work_dir = ROOT / "generated-artifacts/drive-inspection/mm004"
    suffix = "" if attempt_number == 1 else f"-attempt-{attempt_number}"
    log_path = work_dir / f"mm004-render{suffix}.log"
    metrics_path = work_dir / f"mm004-render-metrics{suffix}.json"
    command_path = work_dir / f"mm004-render-command{suffix}.json"
    for previous_attempt in range(1, attempt_number):
        archive = work_dir / f"attempt-{previous_attempt}-invalid-duration-1.733s.mp4"
        validation_path = work_dir / f"mm004-validation-attempt-{previous_attempt}.json"
        if not archive.is_file() or not validation_path.is_file():
            raise PermissionError("La pasada correctiva exige conservar y registrar la salida incompleta anterior.")
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.get("status") != "invalid_duration":
            raise PermissionError("Solo se permite continuar después de una salida registrada como incompleta.")
    for path in (log_path, metrics_path, command_path):
        if path.exists():
            raise FileExistsError(f"Se rechaza volver a ejecutar MM-004 porque ya existe {path}.")

    source = (ROOT / plan["source"]).resolve()
    baseline = (ROOT / plan["previous_version"]).resolve()
    if not baseline.is_file():
        raise FileNotFoundError(f"No se encuentra el resultado MM-003 que debe conservarse: {baseline}")
    source_hash_before = _sha256(source)
    baseline_hash_before = _sha256(baseline)
    command_path.write_text(
        json.dumps({"command": command, "overlay_inputs": specs}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    started = time.perf_counter()
    peak_rss = 0
    samples = 0
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
        last_notice = started
        while process.poll() is None:
            peak_rss = max(peak_rss, _rss_bytes(process.pid))
            samples += 1
            now = time.perf_counter()
            if now - last_notice >= 15:
                print(f"MM-004 en curso: {now - started:.0f}s; RSS máximo observado {peak_rss / (1024 * 1024):.0f} MiB", flush=True)
                last_notice = now
            time.sleep(1)
        return_code = process.wait()
    elapsed = time.perf_counter() - started
    peak_rss = max(peak_rss, _rss_bytes(process.pid))
    metrics: dict[str, Any] = {
        "attempt_number": attempt_number,
        "attempts_total_for_task": attempt_number,
        "automatic_retries": 0,
        "exit_code": return_code,
        "render_seconds": round(elapsed, 3),
        "peak_ffmpeg_rss_mib_sampled": round(peak_rss / (1024 * 1024), 2),
        "rss_samples": samples,
        "output": str(output),
        "output_bytes": output.stat().st_size if output.is_file() else 0,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": _sha256(source),
        "mm003_sha256_before": baseline_hash_before,
        "mm003_sha256_after": _sha256(baseline),
        "effect_cues": plan["effect_cues"],
        "overlay_inputs": specs,
        "paid_api_calls": 0,
        "external_calls": 0,
        "render_retries": 0,
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    if return_code != 0:
        raise RuntimeError(f"FFmpeg terminó con exit code {return_code}; no se lanzará otro intento.")
    if metrics["output_bytes"] <= 0:
        raise RuntimeError("FFmpeg no creó un MP4 con contenido; no se lanzará otro intento.")
    if metrics["source_sha256_before"] != metrics["source_sha256_after"]:
        raise RuntimeError("El hash del original cambió inesperadamente.")
    if metrics["mm003_sha256_before"] != metrics["mm003_sha256_after"]:
        raise RuntimeError("El hash de MM-003 cambió inesperadamente.")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Render local de una sola pasada para MM-004.")
    parser.add_argument("--run", action="store_true", help="Ejecutar una única pasada FFmpeg (sin reintentos).")
    parser.add_argument("--attempt-2", action="store_true", help="Autorizar la única pasada correctiva tras preservar el primer resultado inválido.")
    parser.add_argument("--attempt-3", action="store_true", help="Autorizar la última pasada manual tras preservar dos resultados inválidos.")
    args = parser.parse_args()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    command, specs = build_command(plan)
    if not args.run:
        print(json.dumps({"dry_run": True, "command": command, "overlay_inputs": specs}, ensure_ascii=False, indent=2))
        return 0
    try:
        if args.attempt_2 and args.attempt_3:
            raise ValueError("Solo se puede seleccionar una pasada correctiva.")
        attempt_number = 3 if args.attempt_3 else (2 if args.attempt_2 else 1)
        run_once(plan, attempt_number=attempt_number)
    except Exception as exc:
        print(f"MM-004 detenido: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
