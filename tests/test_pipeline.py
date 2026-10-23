import tempfile
import unittest
from pathlib import Path

from video_pipeline.config import PipelineConfig
from video_pipeline.pipeline import build_dry_run_plan, find_local_mp4s


class PipelineDryRunTests(unittest.TestCase):
    def test_finds_mp4_case_insensitively_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "clip.MP4"
            note = root / "notes.txt"
            video.write_bytes(b"test fixture, not a real video")
            note.write_text("leave unchanged", encoding="utf-8")

            before = (video.read_bytes(), note.read_bytes())
            found = find_local_mp4s(root)

            self.assertEqual([item.name for item in found], ["clip.MP4"])
            self.assertEqual((video.read_bytes(), note.read_bytes()), before)

    def test_dry_run_makes_no_external_calls_and_includes_drive_destinations(self):
        config = PipelineConfig(
            input_folder_id="input-id",
            processed_folder_id="processed-id",
            logos_folder_id="logos-id",
            openai_api_key_present=False,
            google_oauth_token_present=False,
            shotstack_api_key_present=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = build_dry_run_plan(config, temp_dir)

        self.assertEqual(plan["external_services_called"], [])
        self.assertFalse(plan["source_files_modified"])
        self.assertEqual(plan["processed_folder_id"], "processed-id")
        self.assertEqual(plan["discovered_local_mp4_count"], 0)

    def test_missing_input_directory_is_an_empty_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "not-created"
            self.assertEqual(find_local_mp4s(missing), [])
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

