"""Command-line entry point for the safe, offline pipeline check."""

import argparse
import json
from pathlib import Path

from .config import PipelineConfig
from .pipeline import build_dry_run_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan a video pipeline run without calling paid APIs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan local MP4 filenames and print a plan; never calls external APIs.",
    )
    parser.add_argument(
        "--input-dir",
        default="data/incoming",
        help="Local directory to scan in dry-run mode (default: data/incoming).",
    )
    args = parser.parse_args()

    if not args.dry_run:
        parser.error("Only --dry-run is implemented. Live processing is intentionally disabled.")

    plan = build_dry_run_plan(PipelineConfig.from_environment(), Path(args.input_dir))
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

