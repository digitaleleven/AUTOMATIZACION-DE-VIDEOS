import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from video_pipeline.media import make_analysis_clip, probe_video


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class AnalysisClipTests(unittest.TestCase):
    def test_five_second_analysis_clip_preserves_source_and_audio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=216x384:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-color_range", "tv",
                    "-c:a", "aac", "-shortest", str(source),
                ],
                check=True,
                capture_output=True,
            )
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            info = probe_video(source)
            clip = make_analysis_clip(info, 5, root / "temporary")
            self.assertTrue(clip.has_audio)
            self.assertLessEqual(clip.duration, 5.35)
            self.assertGreaterEqual(clip.duration, 4.65)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertNotEqual(Path(clip.path).resolve(), source.resolve())
