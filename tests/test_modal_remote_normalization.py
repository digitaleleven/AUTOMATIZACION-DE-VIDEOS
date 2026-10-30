import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from video_pipeline import modal_render_app


class FakeCommittedVolume:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class ModalRemoteNormalizationTests(unittest.TestCase):
    def test_remote_normalizer_creates_verified_cache_then_reuses_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            volume_root = Path(temp_dir) / "volume"
            job_one = volume_root / "jobs" / ("a" * 32)
            job_two = volume_root / "jobs" / ("b" * 32)
            job_one.mkdir(parents=True)
            job_two.mkdir(parents=True)
            source_bytes = b"mock HEVC source; not a real conversion"
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            for job in (job_one, job_two):
                (job / "source.mov").write_bytes(source_bytes)
                (job / "normalization.json").write_text(json.dumps({
                    "job_id": job.name,
                    "source_name": "source.mov",
                    "source_sha256": source_hash,
                    "profile_id": modal_render_app.NORMALIZATION_PROFILE_ID,
                }))
            profile = {
                "width": 1080, "height": 1920, "fps": 30.0,
                "codec": "h264", "pixel_format": "yuv420p",
                "color_range": "tv", "color_primaries": "bt709",
                "color_transfer": "bt709", "color_space": "bt709",
                "audio_codec": "aac", "audio_sample_rate": 48000,
                "audio_channels": 2, "duration_seconds": 1.0,
            }
            worker_volume = FakeCommittedVolume()
            subprocess_calls = []

            def fake_run(args, *, timeout):
                subprocess_calls.append((args, timeout))
                Path(args[3]).write_bytes(b"mock proxy bytes")
                return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

            with (
                patch.object(modal_render_app, "VOLUME_MOUNT", volume_root),
                patch.object(modal_render_app, "staging_volume", worker_volume),
                patch.object(modal_render_app, "_verify_ffmpeg_capabilities", return_value={
                    "ffmpeg_version": "ffmpeg mock", "filters": "zscale,tonemap",
                    "encoder": "libx264", "hevc_decoder": "available",
                }),
                patch.object(modal_render_app, "_probe_normalized_proxy", return_value=profile),
                patch.object(modal_render_app, "_run_checked", side_effect=fake_run),
            ):
                first = modal_render_app._normalize_staged_source(job_one, job_one.name)
                second = modal_render_app._normalize_staged_source(job_two, job_two.name)

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(subprocess_calls), 1)
        self.assertEqual(subprocess_calls[0][1], modal_render_app.NORMALIZATION_TIMEOUT_SECONDS)
        self.assertTrue(first["proxy_path"].startswith("normalized-cache/"))
        self.assertEqual(worker_volume.commits, 1)

