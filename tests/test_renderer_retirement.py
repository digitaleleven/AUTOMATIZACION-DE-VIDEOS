import dataclasses
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_pipeline.config import PipelineConfig
from video_pipeline.pipeline import process_local_video
from video_pipeline.state import ProcessingLedger


class RendererRetirementTests(unittest.TestCase):
    def test_retired_provider_module_is_not_installed_or_importable(self):
        self.assertIsNone(importlib.util.find_spec("video_pipeline.shotstack"))
        with self.assertRaises(ModuleNotFoundError):
            __import__("video_pipeline.shotstack")

    def test_runtime_configuration_has_no_provider_environment_or_credentials(self):
        with patch.dict(
            os.environ,
            {"SHOTSTACK_ENVIRONMENT": "v1", "SHOTSTACK_API_KEY": "must-not-be-read"},
            clear=True,
        ):
            config = PipelineConfig.from_environment()

        field_names = {field.name for field in dataclasses.fields(config)}
        self.assertFalse(any("shotstack" in name.lower() for name in field_names))
        self.assertFalse(hasattr(config, "shotstack_environment"))
        self.assertFalse(hasattr(config, "shotstack_api_key_present"))

    def test_legacy_cli_processing_fails_closed_without_external_calls(self):
        with (
            patch("requests.Session.request", side_effect=AssertionError("network call")),
            self.assertRaisesRegex(RuntimeError, "CLI.*deshabilitado"),
        ):
            process_local_video(
                "not-read-or-uploaded.mov",
                paid_api_authorized=True,
                config=PipelineConfig(),
            )

    def test_existing_legacy_ledger_fields_and_values_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "processing.sqlite3"
            ledger = ProcessingLedger(database)
            ledger.claim("a" * 64, "source.mov")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE processing_jobs SET shotstack_source_url=?, render_id=? WHERE source_sha256=?",
                    ("https://legacy.invalid/source", "legacy-render-id", "a" * 64),
                )

            reopened = ProcessingLedger(database)
            row = reopened.get("a" * 64)
            self.assertEqual(row["shotstack_source_url"], "https://legacy.invalid/source")
            self.assertEqual(row["render_id"], "legacy-render-id")


if __name__ == "__main__":
    unittest.main()
