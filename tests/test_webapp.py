import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from video_pipeline.webapp import VideoPanel, VideoPanelHandler, VideoPanelServer


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
class VideoPanelApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        style = root / "video_style.json"
        shutil.copyfile(Path(__file__).resolve().parents[1] / "video_style.json", style)
        self.panel = VideoPanel(
            input_dir=root / "incoming",
            output_dir=root / "outputs",
            database_path=root / "panel.sqlite3",
            style_path=style,
        )
        self.server = VideoPanelServer(("127.0.0.1", 0), VideoPanelHandler, self.panel)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.panel.executor.shutdown(wait=True, cancel_futures=True)
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = response.getheaders()
        connection.close()
        return status, response_headers, payload

    def create_movie(self, extension=".MOV"):
        path = self.root / f"iphone{extension}"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return path

    def upload(self, path):
        data = path.read_bytes()
        status, _, body = self.request(
            "POST",
            "/api/upload",
            body=data,
            headers={
                "Content-Type": "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4",
                "Content-Length": str(len(data)),
                "X-Upload-Filename": quote(path.name),
            },
        )
        return status, json.loads(body.decode("utf-8"))

    def test_panel_serves_real_empty_state_and_secret_presence_only(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Biblioteca local", body)

        status, _, body = self.request("GET", "/api/health")
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIsInstance(health["openai_key_configured"], bool)
        self.assertIsInstance(health["shotstack_key_configured"], bool)
        self.assertFalse(health["drive_connected"])
        self.assertNotIn("api_key", health)

        status, _, body = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"items": []})

    def test_uploads_mov_validates_metadata_and_supports_video_ranges(self):
        path = self.create_movie()
        status, response = self.upload(path)
        self.assertEqual(status, 201)
        item = response["item"]
        self.assertEqual(item["format"], "MOV")
        self.assertEqual(item["video_codec"], "h264")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["width"], 360)
        self.assertEqual(item["height"], 640)

        status, headers, payload = self.request(
            "GET", f"/media/incoming/{item['id']}", headers={"Range": "bytes=0-15"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(payload), 16)
        self.assertTrue(dict(headers)["Content-Range"].startswith("bytes 0-15/"))

        status, _, body = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["items"][0]["id"], item["id"])

    def test_paid_processing_requires_both_explicit_authorizations(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        source_id = response["item"]["id"]
        status, _, body = self.request(
            "POST",
            "/api/jobs",
            body=json.dumps({"source_id": source_id, "authorize_paid_apis": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("autorización explícita", json.loads(body.decode("utf-8"))["detail"])
        self.assertEqual(self.panel.library()[0]["status"], "ready")

    def test_invalid_style_is_rejected_without_overwriting_configuration(self):
        original = (self.root / "video_style.json").read_bytes()
        style = self.panel.style()
        style["animation_style"] = "fade"
        status, _, body = self.request(
            "PUT",
            "/api/style",
            body=json.dumps(style).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("animation_style", json.loads(body.decode("utf-8"))["detail"])
        self.assertEqual((self.root / "video_style.json").read_bytes(), original)

    def test_valid_style_changes_are_saved_and_returned(self):
        style = self.panel.style()
        style["font_size"] = 72
        status, _, body = self.request(
            "PUT",
            "/api/style",
            body=json.dumps(style).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["font_size"], 72)

        status, _, body = self.request("GET", "/api/style")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["font_size"], 72)


if __name__ == "__main__":
    unittest.main()
