"""Command-line entry point for dry-run and opt-in local processing."""

import argparse
import json
import logging
from pathlib import Path
import sys

from .config import PipelineConfig
from .pipeline import build_dry_run_plan, process_local_video
from .style import load_video_style


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect local MP4/MOV files. Rendering is available through the authenticated web panel."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan local MP4/MOV files and print a plan; never calls external APIs.",
    )
    mode.add_argument(
        "--process",
        metavar="VIDEO",
        help="Retired: local CLI rendering is disabled; use the authenticated panel and Modal worker.",
    )
    parser.add_argument(
        "--input-dir",
        default="data/incoming",
        help="Local directory to scan in dry-run mode (default: data/incoming).",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory for new rendered files (default: outputs).",
    )
    parser.add_argument(
        "--style",
        default="video_style.json",
        help="JSON visual style and processing limits (default: video_style.json).",
    )
    parser.add_argument(
        "--state-db",
        default="data/processing.sqlite3",
        help="SQLite idempotency ledger (default: data/processing.sqlite3).",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Explicitly retry one previously failed source, reusing any saved transcript/render ID.",
    )
    parser.add_argument(
        "--authorize-paid-apis",
        action="store_true",
        help=(
            "Compatibility flag retained for old scripts; it does not enable CLI rendering."
        ),
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Keep the rendered file in outputs/ and do not create a Google Drive file.",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.dry_run:
        if args.authorize_paid_apis:
            parser.error("--authorize-paid-apis cannot be used with --dry-run.")
        if args.retry_failed:
            parser.error("--retry-failed can only be used with --process.")
        if args.local_only:
            parser.error("--local-only can only be used with --process.")
        try:
            config = PipelineConfig.from_environment()
            style = load_video_style(args.style)
            print(
                json.dumps(
                    build_dry_run_plan(config, Path(args.input_dir), style),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception as exc:
            logging.getLogger(__name__).error("Dry run failed: %s", exc)
            return 2

    if not args.authorize_paid_apis:
        parser.error(
            "El procesamiento por CLI está retirado. Para editar, usa el panel autenticado "
            "con el worker Modal; no se ha enviado el vídeo a ningún servicio."
        )

    try:
        result = process_local_video(
            args.process,
            output_dir=args.output_dir,
            paid_api_authorized=True,
            upload_to_drive=not args.local_only,
            config=PipelineConfig.from_environment(),
            style=load_video_style(args.style),
            state_db_path=args.state_db,
            retry_failed=args.retry_failed,
        )
    except Exception as exc:
        # Keep console logs concise and avoid leaking request URLs or tokens
        # if an upstream library includes them in a chained exception.
        logging.getLogger(__name__).error("Video processing failed: %s", exc)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

