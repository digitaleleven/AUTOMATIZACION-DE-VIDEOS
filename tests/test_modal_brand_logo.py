from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from video_pipeline.modal_pipeline import _validated_brand_logo
from video_pipeline.style import VideoStyle


class ModalBrandLogoTests(unittest.TestCase):
    def test_no_configured_logo_is_an_explicit_empty_slot(self):
        self.assertEqual(_validated_brand_logo(VideoStyle()), (None, None))

    def test_local_raster_logo_is_staged_under_a_safe_name(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "agency brand.webp"
            source.write_bytes(b"image-placeholder")
            path, asset = _validated_brand_logo(
                VideoStyle(logo_path=str(source))
            )

        self.assertEqual(path, source.resolve())
        self.assertEqual(asset, "client-brand-logo.webp")

    def test_missing_unsupported_or_oversized_logo_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.png"
            unsupported = Path(directory) / "agency.gif"
            unsupported.write_bytes(b"gif")
            oversized = Path(directory) / "large.png"
            oversized.write_bytes(b"x" * (20 * 1024 * 1024 + 1))

            for path in (missing, unsupported, oversized):
                with self.subTest(path=path.suffix, exists=path.exists()):
                    with self.assertRaisesRegex(ValueError, "no es accesible"):
                        _validated_brand_logo(VideoStyle(logo_path=str(path)))


if __name__ == "__main__":
    unittest.main()
