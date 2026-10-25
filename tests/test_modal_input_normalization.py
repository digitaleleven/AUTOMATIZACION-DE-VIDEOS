from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from video_pipeline.media import prepare_video_for_modal, probe_video


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("node"),
    "FFmpeg, FFprobe and Node.js are required for local normalization tests",
)
class ModalInputNormalizationTests(unittest.TestCase):
    def test_normalizes_a_local_sdr_clip_without_modifying_source(self):
        with tempfile.TemporaryDirectory(prefix="matrix-modal-normalize-test-") as temp:
            root = Path(temp)
            source = root / "sample.mp4"
            output_dir = root / "normalized"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                    "-c:a", "aac", "-ac", "2", "-ar", "48000", "-shortest",
                    str(source),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            normalized, report = prepare_video_for_modal(probe_video(source), output_dir)

            self.assertTrue(Path(normalized.path).is_file())
            self.assertEqual((normalized.width, normalized.height), (1080, 1920))
            self.assertEqual(normalized.video_codec, "h264")
            self.assertEqual(normalized.pixel_format, "yuv420p")
            self.assertAlmostEqual(normalized.fps, 30, places=2)
            self.assertTrue(normalized.has_audio)
            self.assertEqual(normalized.audio_codec, "aac")
            self.assertEqual(normalized.color_primaries, "bt709")
            self.assertEqual(normalized.color_transfer, "bt709")
            self.assertEqual(normalized.color_space, "bt709")
            self.assertEqual(normalized.color_range, "tv")
            self.assertTrue(report["converted"])
            self.assertEqual(report["method"], "Remotion color profile + FFmpeg")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)


if __name__ == "__main__":
    unittest.main()
