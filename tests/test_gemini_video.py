import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_pipeline.gemini_video import (
    GeminiAnalysisError,
    analyze_video,
    validate_gemini_analysis,
)


class FakeResponse:
    def __init__(self, payload=None, *, status=200, headers=None):
        self._payload = payload or {}
        self.status_code = status
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class FakeGeminiSession:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json=None, data=None, timeout=None):
        self.calls.append(("POST", url, headers, json, data, timeout))
        if "/upload/v1beta/files" in url:
            return FakeResponse(headers={"X-Goog-Upload-URL": "https://upload.invalid/session"})
        if url == "https://upload.invalid/session":
            self.uploaded_bytes = data.read()
            return FakeResponse({"file": {
                "name": "files/test_clip",
                "uri": "https://generativelanguage.googleapis.com/v1beta/files/test_clip",
                "mimeType": "video/mp4",
                "state": "ACTIVE",
            }})
        return FakeResponse({
            "candidates": [{
                "content": {"parts": [{"text": __import__("json").dumps({
                    "summary": "Vídeo de prueba.",
                    "key_moments": [
                        {"start_seconds": 0.1, "end_seconds": 1.0, "label": "Producto", "reason": "Plano visible."},
                    ],
                    "suggested_zooms": [
                        {"start_seconds": 0.2, "end_seconds": 0.8, "reason": "Énfasis visual."},
                    ],
                    "suggested_cuts": [],
                    "copy_and_seo": {
                        "hook": "Mira esto",
                        "caption": "Una demostración breve.",
                        "seo_title": "Demo de producto",
                        "hashtags": ["#demo", "#producto"],
                    },
                    "warnings": [],
                })}]}
            }],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 18,
                "totalTokenCount": 138,
            },
        })

    def get(self, *args, **kwargs):
        raise AssertionError("No se espera polling para un archivo ACTIVE.")

    def delete(self, url, *, headers, timeout):
        self.calls.append(("DELETE", url, headers, None, None, timeout))
        return FakeResponse({})


class GeminiVideoTests(unittest.TestCase):
    def test_analysis_validates_visual_ranges_zoom_and_seo_copy(self):
        result = validate_gemini_analysis({
            "summary": "Resumen.",
            "key_moments": [
                {"start_seconds": 0.1, "end_seconds": 0.9, "label": "Producto", "reason": "Se ve con claridad."},
            ],
            "suggested_zooms": [
                {"start_seconds": 1.0, "end_seconds": 1.5, "reason": "Énfasis."},
            ],
            "suggested_cuts": [
                {"start_seconds": 2.0, "end_seconds": 2.5, "reason": "Pausa larga."},
            ],
            "copy_and_seo": {
                "hook": "Mira esto",
                "caption": "Descripción fiel.",
                "seo_title": "Título SEO",
                "hashtags": ["#demo"],
            },
            "warnings": [],
        }, 5.0)
        self.assertEqual(len(result["key_moments"]), 1)
        self.assertEqual(result["suggested_zooms"][0]["effect"], "zoomIn")
        self.assertFalse("applied" in result)
        self.assertEqual(result["suggested_cuts"][0]["reason"], "Pausa larga.")

    def test_invalid_or_out_of_range_visual_suggestions_are_rejected(self):
        with self.assertRaises(GeminiAnalysisError):
            validate_gemini_analysis({
                "summary": "",
                "key_moments": [],
                "suggested_zooms": [{"start_seconds": 0, "end_seconds": 5.2, "reason": "fuera"}],
                "suggested_cuts": [],
                "copy_and_seo": {"hook": "", "caption": "", "seo_title": "", "hashtags": []},
                "warnings": [],
            }, 5.0)
        with self.assertRaises(GeminiAnalysisError):
            validate_gemini_analysis({
                "summary": "",
                "key_moments": [],
                "suggested_zooms": [],
                "suggested_cuts": [{"start_seconds": 4.9, "end_seconds": 5.2, "reason": "fuera"}],
                "copy_and_seo": {"hook": "", "caption": "", "seo_title": "", "hashtags": ["sin-hash"]},
                "warnings": [],
            }, 5.0)

    def test_upload_analysis_and_remote_file_cleanup_use_configured_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "analysis.mp4"
            path.write_bytes(b"small test video")
            fake = FakeGeminiSession()
            with patch.dict("os.environ", {"GEMINI_API_KEY": "test-only-not-real"}):
                result = analyze_video(
                    path,
                    duration_seconds=5.0,
                    briefing="Subtítulos pastel.",
                    session=fake,
                )
        self.assertEqual(result["model"], "gemini-3.8-flash")
        self.assertEqual(result["usage"]["total_tokens"], 138)
        self.assertTrue(result["remote_file_deleted"])
        self.assertEqual(fake.uploaded_bytes, b"small test video")
        generation = next(call for call in fake.calls if ":generateContent" in call[1])
        self.assertEqual(generation[1].split("/models/", 1)[1].split(":", 1)[0], "gemini-3.8-flash")
        self.assertTrue(any(call[0] == "DELETE" for call in fake.calls))
        self.assertNotIn("test-only-not-real", generation[1])

    def test_missing_key_fails_before_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "analysis.mp4"
            path.write_bytes(b"video")
            fake = FakeGeminiSession()
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(GeminiAnalysisError, "GEMINI_API_KEY"):
                    analyze_video(path, duration_seconds=5.0, session=fake)
        self.assertEqual(fake.calls, [])
