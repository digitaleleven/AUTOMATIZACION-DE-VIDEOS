import http.client
import json
import os
import re
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from unittest.mock import patch

from video_pipeline.auth import OwnerAuth
from video_pipeline.google_drive import DriveOAuthConfig, GoogleDriveError, GoogleDriveOAuth
from video_pipeline.media import probe_video
from video_pipeline.webapp import (
    DashboardStore,
    PanelError,
    VideoPanel,
    VideoPanelHandler,
    VideoPanelServer,
    _prepare_modal_render_plan,
)


class ModalZoomCompatibilityTests(unittest.TestCase):
    def test_gemini_zoom_windows_remain_suggestions_for_modal_auto_zoom(self):
        suggestions = [{
            "start_seconds": 1.0,
            "end_seconds": 2.5,
            "reason": "momento clave",
        }]

        plan, retained_suggestions, applied = _prepare_modal_render_plan(
            {},
            5.0,
            suggestions,
        )

        self.assertEqual(plan["zooms"], [])
        self.assertEqual(retained_suggestions, suggestions)
        self.assertEqual(applied, [])

    def test_explicit_timed_zoom_stays_blocked_for_modal(self):
        with self.assertRaisesRegex(PanelError, "solo admite el auto-zoom suave"):
            _prepare_modal_render_plan(
                {"zooms": [{"start_seconds": 1.0, "end_seconds": 2.0}]},
                5.0,
                [],
            )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
class VideoPanelApiTests(unittest.TestCase):
    def test_restart_marks_local_processing_as_interrupted_for_safe_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "panel.sqlite3"
            store = DashboardStore(database)
            source_hash = "a" * 64
            source_id = "b" * 32
            store.add_upload(
                source_id,
                "source.mp4",
                "source.mp4",
                source_hash,
                10,
                {"duration": 1},
            )
            store.create_job("c" * 32, source_id, 0.25, {"subtitle_mode": "none"})
            store.ledger.claim(
                source_hash,
                "source.mp4",
                edit_options={"subtitle_mode": "none"},
            )
            store.ledger.update(source_hash, render_id="persisted-stage-render")

            recovered = DashboardStore(database)

            self.assertEqual(recovered.ledger.get(source_hash)["status"], "failed")
            self.assertEqual(recovered.ledger.get(source_hash)["render_id"], "persisted-stage-render")
            job = recovered.get_job("c" * 32)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["phase"], "interrupted")
            self.assertIn("El servicio se reinició", job["error"])

    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "PANEL_OWNER_PASSWORD": "unit-test-owner-passphrase",
                "SESSION_SECRET": "unit-test-session-secret-for-panel-auth-0123456789",
                "PANEL_COOKIE_SECURE": "true",
                "GOOGLE_DRIVE_OAUTH_ENABLED": "false",
                "GOOGLE_OAUTH_CLIENT_ID": "",
                "GOOGLE_OAUTH_CLIENT_SECRET": "",
                "GOOGLE_OAUTH_REDIRECT_URI": "",
                "GOOGLE_OAUTH_ALLOWED_EMAIL": "",
            },
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
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
        self.session_cookie = ""
        self.csrf_token = ""
        self.login()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.panel.executor.shutdown(wait=True, cancel_futures=True)
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None, *, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = dict(headers or {})
        if authenticated and self.session_cookie:
            request_headers.setdefault("Cookie", self.session_cookie)
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and self.csrf_token:
                request_headers.setdefault("X-CSRF-Token", self.csrf_token)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        response_headers = response.getheaders()
        connection.close()
        return status, response_headers, payload

    @staticmethod
    def cookie_from_headers(headers, name):
        for header_name, value in headers:
            if header_name.lower() == "set-cookie" and value.startswith(f"{name}="):
                return value.split(";", 1)[0]
        return ""

    def login(self):
        status, headers, body = self.request("GET", "/login", authenticated=False)
        self.assertEqual(status, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        challenge_token = match.group(1).decode("ascii")
        challenge_cookie = self.cookie_from_headers(headers, "matrix_motion_login_csrf")
        self.assertTrue(challenge_cookie)
        challenge_header = next(
            value for name, value in headers
            if name.lower() == "set-cookie" and value.startswith("matrix_motion_login_csrf=")
        )
        self.assertIn("HttpOnly", challenge_header)
        self.assertIn("Secure", challenge_header)
        self.assertIn("SameSite=Strict", challenge_header)
        status, response_headers, _ = self.request(
            "POST",
            "/login",
            body=urlencode({"password": "unit-test-owner-passphrase", "csrf_token": challenge_token}),
            headers={
                "Cookie": challenge_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            authenticated=False,
        )
        self.assertEqual(status, 303)
        self.session_cookie = self.cookie_from_headers(response_headers, "matrix_motion_session")
        self.assertTrue(self.session_cookie)
        session_header = next(
            value for name, value in response_headers
            if name.lower() == "set-cookie" and value.startswith("matrix_motion_session=")
        )
        self.assertIn("HttpOnly", session_header)
        self.assertIn("Secure", session_header)
        self.assertIn("SameSite=Strict", session_header)
        self.csrf_token = self.request("GET", "/api/session")[2]
        self.csrf_token = json.loads(self.csrf_token.decode("utf-8"))["csrf_token"]

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

    def upload(self, path, *, niche="generic_viral", style_notes="", editing_style="dynamic"):
        data = path.read_bytes()
        status, _, body = self.request(
            "POST",
            "/api/upload",
            body=data,
            headers={
                "Content-Type": "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4",
                "Content-Length": str(len(data)),
                "X-Upload-Filename": quote(path.name),
                "X-Video-Niche": niche,
                "X-Editing-Style": editing_style,
                "X-Style-Notes": quote(style_notes, safe=""),
            },
        )
        return status, json.loads(body.decode("utf-8"))

    def create_failed_0415_attempt(self, render_id=None):
        temp_video = self.create_movie(".mov")
        source = self.root / "0415.mov"
        shutil.copyfile(temp_video, source)
        status, response = self.upload(source)
        self.assertEqual(status, 201)
        source_id = response["item"]["id"]
        upload = self.panel.store.get_upload(source_id)
        style = replace(
            self.panel._style_for_upload(source_id),
            output_width=1080,
            output_height=1920,
            output_fps=50,
            output_quality="high",
        )
        creative_plan = {
            "cuts": [],
            "zooms": [{"start_seconds": 10.0, "end_seconds": 13.0, "effect": "zoomIn"}],
            "stickers": [{
                "sticker": "sparkle",
                "start_seconds": 4.0,
                "duration_seconds": 1.5,
                "position": "topRight",
                "effect": "zoomIn",
                "color": "#FFD84D",
            }],
        }
        processing_options = {
            "subtitle_mode": "none",
            "style": asdict(style),
            "creative_plan": creative_plan,
        }
        source_path = self.panel._source_path(upload)
        self.panel.store.ledger.claim(
            upload["source_sha256"],
            str(source_path),
            edit_options=processing_options,
        )
        self.panel.store.ledger.mark_failed(
            upload["source_sha256"],
            "Shotstack render submission failed (HTTP 400).",
        )
        if render_id:
            self.panel.store.ledger.update(upload["source_sha256"], render_id=render_id)
        job_id = "a" * 32
        self.panel.store.create_job(
            job_id,
            source_id,
            None,
            {
                "subtitle_mode": "none",
                "output_profile": "1080p",
                "output_quality": "high",
                "output_fps": 50,
                "creative_plan": creative_plan,
            },
        )
        self.panel.store.update_job(
            job_id,
            status="failed",
            phase="failed",
            error="Shotstack render submission failed (HTTP 400).",
        )
        return source_id, job_id, upload, processing_options

    def test_panel_serves_real_empty_state_and_secret_presence_only(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Tu biblioteca", body)
        self.assertIn(b"summary-pending", body)
        self.assertIn(b'name="emoji_by_keyword"', body)
        self.assertIn(b'name="logo_position"', body)
        for old_sample_id in (b"q03c-sample-video", b"muestra-q03d", b"q03e-sample-video"):
            self.assertNotIn(old_sample_id, body)
        self.assertIn(b'id="upload-niche"', body)
        self.assertIn(b'Beauty &amp; Skincare', body)
        self.assertIn(b'Fashion &amp; Style', body)
        self.assertIn(b'id="upload-style-notes"', body)
        self.assertIn(b'Notas o instrucciones de estilo especiales para este v\xc3\xaddeo (Opcional)', body)
        self.assertIn(b'id="resultado-fase1"', body)
        self.assertIn(b'id="resultado-fase1"', body)
        self.assertIn(b'id="phase1-final-video"', body)
        self.assertIn(b'/media/outputs/IMG_1254-MATRIX-MOTION-FASE1.mp4', body)
        self.assertIn(b'PENDIENTE DE REVISI\xc3\x93N HUMANA', body)
        self.assertIn(b'id="resultado-mm003"', body)
        self.assertIn(b'id="mm003-final-video"', body)
        self.assertIn(b'/media/outputs/IMG_1254-MATRIX-MOTION-MM003.mp4', body)
        self.assertIn(b'id="resultado-mm004"', body)
        self.assertIn(b'id="mm004-final-video"', body)
        self.assertIn(b'/media/outputs/IMG_1254-MATRIX-MOTION-MM004.mp4', body)
        self.assertIn(b'id="resultado-remotion"', body)
        self.assertIn(b'id="remotion-final-video"', body)
        self.assertIn(b'data-src="/media/outputs/IMG_1254-MATRIX-MOTION-v3-PRO-MASTER.mp4"', body)
        self.assertIn(b'name="show_subtitles" type="checkbox"', body)
        self.assertIn(b'00:56,51', body)
        self.assertIn(b'<script src="/upload-controller.js" defer></script>', body)

        status, _, upload_controller = self.request("GET", "/upload-controller.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function bindFileInput", upload_controller)

    def test_mm004_result_is_authenticated_range_media(self):
        output_path = self.panel.output_dir / "IMG_1254-MATRIX-MOTION-MM004.mp4"
        output_bytes = b"mm004-video-bytes-for-authenticated-range-test"
        output_path.write_bytes(output_bytes)

        status, headers, body = self.request(
            "GET",
            "/media/outputs/IMG_1254-MATRIX-MOTION-MM004.mp4",
            headers={"Range": "bytes=0-9"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, output_bytes[:10])
        self.assertEqual(dict(headers)["Content-Type"], "video/mp4")
        self.assertEqual(
            dict(headers)["Content-Range"],
            f"bytes 0-9/{len(output_bytes)}",
        )

        status, _, body = self.request(
            "GET",
            "/media/outputs/IMG_1254-MATRIX-MOTION-MM004.mp4",
            authenticated=False,
        )
        self.assertEqual(status, 401)

        status, headers, body = self.request("GET", "/premium.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", dict(headers)["Content-Type"])
        self.assertIn(b"app-shell", body)

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

    def test_q03c_sample_and_comparison_are_authenticated_range_media(self):
        sample = self.root / "q03c-sample.mp4"
        comparison = self.root / "q03c-comparison.png"
        sample_bytes = b"sample-media-bytes-for-range-test"
        comparison_bytes = b"comparison-image-bytes"
        sample.write_bytes(sample_bytes)
        comparison.write_bytes(comparison_bytes)
        self.panel.q03c_sample_path = sample
        self.panel.q03c_comparison_path = comparison

        status, headers, body = self.request(
            "GET", "/media/q03c-sample", headers={"Range": "bytes=0-9"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, sample_bytes[:10])
        self.assertEqual(dict(headers)["Content-Type"], "video/mp4")
        self.assertEqual(dict(headers)["Content-Range"], f"bytes 0-9/{len(sample_bytes)}")

        status, headers, body = self.request(
            "GET", "/media/q03c-comparison", headers={"Range": "bytes=0-9"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, comparison_bytes[:10])
        self.assertEqual(dict(headers)["Content-Type"], "image/png")

        for path in ("/media/q03c-sample", "/media/q03c-comparison"):
            status, _, body = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 401, path)
            self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

    def test_q03d_comparisons_are_authenticated_range_media(self):
        comparisons = {
            "/media/q03d/summary": ("q03d-summary.png", "q03d_summary_path"),
            "/media/q03d/candidates-8s": ("q03d-8s.png", "q03d_candidates_8s_path"),
            "/media/q03d/candidates-25s": ("q03d-25s.png", "q03d_candidates_25s_path"),
            "/media/q03d/candidates-49s": ("q03d-49s.png", "q03d_candidates_49s_path"),
        }
        for index, (route, (filename, attribute)) in enumerate(comparisons.items()):
            with self.subTest(route=route):
                media = self.root / filename
                media_bytes = f"q03d-visual-comparison-{index}".encode()
                media.write_bytes(media_bytes)
                setattr(self.panel, attribute, media)

                status, headers, body = self.request(
                    "GET", route, headers={"Range": "bytes=0-7"}
                )
                self.assertEqual(status, 206)
                self.assertEqual(body, media_bytes[:8])
                self.assertEqual(dict(headers)["Content-Type"], "image/png")

                status, _, body = self.request("GET", route, authenticated=False)
                self.assertEqual(status, 401)
                self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

    def test_q03e_sample_and_comparison_are_authenticated_range_media(self):
        sample = self.root / "q03e-sample.mp4"
        comparison = self.root / "q03e-comparison.png"
        sample_bytes = b"q03e-video-bytes-for-range-test"
        comparison_bytes = b"q03e-comparison-image-bytes"
        sample.write_bytes(sample_bytes)
        comparison.write_bytes(comparison_bytes)
        self.panel.q03e_sample_path = sample
        self.panel.q03e_comparison_path = comparison

        status, headers, body = self.request(
            "GET", "/media/q03e-sample", headers={"Range": "bytes=0-9"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, sample_bytes[:10])
        self.assertEqual(dict(headers)["Content-Type"], "video/mp4")

        status, headers, body = self.request(
            "GET", "/media/q03e-comparison", headers={"Range": "bytes=0-9"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, comparison_bytes[:10])
        self.assertEqual(dict(headers)["Content-Type"], "image/png")

        for route in ("/media/q03e-sample", "/media/q03e-comparison"):
            status, _, body = self.request("GET", route, authenticated=False)
            self.assertEqual(status, 401, route)
            self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

    def test_anonymous_requests_cannot_read_or_mutate_panel(self):
        for path in ("/", "/app.js", "/premium.css"):
            status, headers, _ = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 303, path)
            self.assertEqual(dict(headers).get("Location"), "/login")

        for path in (
            "/api/health",
            "/api/library",
            "/api/style",
            f"/api/jobs/{'a' * 32}",
            "/media/incoming/",
            "/media/q03c-sample",
            "/media/q03c-comparison",
            "/media/q03d/summary",
            "/media/q03d/candidates-25s",
            "/media/q03e-sample",
            "/media/q03e-comparison",
            "/media/outputs/phase1-private.mp4",
        ):
            status, _, body = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 401, path)
            self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

        status, _, body = self.request(
            "POST",
            "/api/upload",
            body=b"not-a-video",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": "11",
                "X-Upload-Filename": "anonymous.mp4",
            },
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

        status, _, body = self.request(
            "POST",
            "/api/proposals",
            body=json.dumps({
                "source_id": "a" * 32,
                "subtitle_mode": "none",
                "output_fps": 50,
            }).encode(),
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

        with patch.object(self.panel, "start_job") as start_job:
            status, _, body = self.request(
                "POST",
                "/api/jobs",
                body=json.dumps({
                    "source_id": "a" * 32,
                    "authorize_paid_apis": True,
                    "unknown_cost_acknowledged": True,
                }).encode(),
                headers={"Content-Type": "application/json"},
                authenticated=False,
            )
        self.assertEqual(status, 401)
        start_job.assert_not_called()
        self.assertEqual(self.panel.store.list_uploads(), [])
        self.assertIn("Inicia sesión", json.loads(body.decode("utf-8"))["detail"])

        status, _, _ = self.request(
            "PUT",
            "/api/style",
            body=json.dumps({"font_size": 80}).encode(),
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        self.assertEqual(status, 401)

    def test_owner_session_can_start_job_through_authenticated_api(self):
        job_id = "a" * 32
        with patch.object(
            self.panel,
            "start_job",
            return_value={"id": job_id, "status": "queued"},
        ) as start_job:
            status, _, body = self.request(
                "POST",
                "/api/jobs",
                body=json.dumps({
                    "source_id": "b" * 32,
                    "authorize_paid_apis": True,
                    "unknown_cost_acknowledged": True,
                }).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body.decode("utf-8"))["id"], job_id)
        start_job.assert_called_once()

    def test_cross_origin_requests_are_rejected_with_valid_owner_session(self):
        foreign_origin = "https://attacker.example"
        style_before = self.panel.style()
        cases = (
            (
                "GET",
                "/api/library",
                None,
                {"Origin": foreign_origin},
            ),
            (
                "POST",
                "/api/upload",
                b"not-a-video",
                {
                    "Origin": foreign_origin,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "11",
                    "X-Upload-Filename": "cross-origin.mp4",
                },
            ),
            (
                "PUT",
                "/api/style",
                json.dumps({**style_before, "font_size": style_before["font_size"] + 1}).encode(),
                {
                    "Origin": foreign_origin,
                    "Content-Type": "application/json",
                },
            ),
            (
                "POST",
                "/api/jobs",
                json.dumps({
                    "source_id": "c" * 32,
                    "authorize_paid_apis": True,
                    "unknown_cost_acknowledged": True,
                }).encode(),
                {
                    "Origin": foreign_origin,
                    "Content-Type": "application/json",
                },
            ),
        )
        with (
            patch.object(self.panel, "upload") as upload,
            patch.object(self.panel, "start_job") as start_job,
        ):
            for method, path, body, headers in cases:
                with self.subTest(method=method, path=path):
                    status, _, response_body = self.request(
                        method,
                        path,
                        body=body,
                        headers=headers,
                    )
                    self.assertEqual(status, 403)
                    self.assertIn(
                        "origen no permitido",
                        json.loads(response_body.decode("utf-8"))["detail"],
                    )

        upload.assert_not_called()
        start_job.assert_not_called()
        self.assertEqual(self.panel.store.list_uploads(), [])
        self.assertEqual(self.panel.style(), style_before)

    def test_login_challenge_is_one_time_and_wrong_password_is_generic(self):
        status, headers, body = self.request("GET", "/login", authenticated=False)
        self.assertEqual(status, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        challenge = match.group(1).decode("ascii")
        challenge_cookie = self.cookie_from_headers(headers, "matrix_motion_login_csrf")
        self.assertTrue(challenge_cookie)

        status, _, body = self.request(
            "POST",
            "/login",
            body=urlencode({
                "password": "incorrect-owner-password",
                "csrf_token": challenge,
            }),
            headers={
                "Cookie": challenge_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertIn("No se pudo iniciar sesión", body.decode("utf-8"))

        status, _, _ = self.request(
            "POST",
            "/login",
            body=urlencode({
                "password": "unit-test-owner-passphrase",
                "csrf_token": challenge,
            }),
            headers={
                "Cookie": challenge_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            authenticated=False,
        )
        self.assertEqual(status, 403)

    def test_login_rate_limit_blocks_repeated_bad_passwords(self):
        for attempt in range(5):
            status, headers, body = self.request("GET", "/login", authenticated=False)
            self.assertEqual(status, 200)
            challenge = re.search(rb'name="csrf_token" value="([^"]+)"', body).group(1).decode()
            challenge_cookie = self.cookie_from_headers(headers, "matrix_motion_login_csrf")
            status, _, _ = self.request(
                "POST",
                "/login",
                body=urlencode({"password": "incorrect-owner-password", "csrf_token": challenge}),
                headers={
                    "Cookie": challenge_cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                authenticated=False,
            )
            self.assertEqual(status, 401, f"failed login attempt {attempt + 1}")

        status, headers, body = self.request("GET", "/login", authenticated=False)
        challenge = re.search(rb'name="csrf_token" value="([^"]+)"', body).group(1).decode()
        challenge_cookie = self.cookie_from_headers(headers, "matrix_motion_login_csrf")
        status, _, body = self.request(
            "POST",
            "/login",
            body=urlencode({"password": "incorrect-owner-password", "csrf_token": challenge}),
            headers={
                "Cookie": challenge_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            authenticated=False,
        )
        self.assertEqual(status, 429)

    def test_preview_origin_uses_exact_replit_domains_from_environment(self):
        preview_host = "temp-preview.replit.dev"
        with patch.dict(
            os.environ,
            {
                "REPLIT_DEV_DOMAIN": preview_host,
                "REPLIT_DOMAINS": f"{preview_host},custom.example",
            },
        ):
            status, headers, body = self.request(
                "GET",
                "/login",
                headers={
                    "Host": f"127.0.0.1:{self.port}",
                    "Origin": "https://replit.com",
                    "X-Forwarded-Host": preview_host,
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 200)
            self.assertIn("text/html", dict(headers)["Content-Type"])
            self.assertIn(b'<form method="post" action="/login"', body)

            for request_host in (preview_host, "replit.com"):
                status, _, body = self.request(
                    "GET",
                    "/login",
                    headers={
                        "Host": request_host,
                        "Origin": "https://replit.com",
                        "X-Forwarded-Host": preview_host,
                        "X-Forwarded-Proto": "https",
                    },
                    authenticated=False,
                )
                self.assertEqual(status, 200)
                self.assertIn(b'<form method="post" action="/login"', body)

            status, challenge_headers, login_page = self.request(
                "GET",
                "/login",
                headers={
                    "Host": preview_host,
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 200)
            challenge = re.search(
                rb'name="csrf_token" value="([^"]+)"',
                login_page,
            ).group(1).decode("ascii")
            challenge_cookie = self.cookie_from_headers(
                challenge_headers,
                "matrix_motion_login_csrf",
            )
            status, favicon_headers, _ = self.request(
                "GET",
                "/favicon.ico",
                headers={
                    "Host": preview_host,
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 204)
            self.assertNotIn("Set-Cookie", dict(favicon_headers))

            status, _, body = self.request(
                "POST",
                "/login",
                body=urlencode({
                    "password": "incorrect-owner-password",
                    "csrf_token": challenge,
                }),
                headers={
                    "Host": preview_host,
                    "Origin": "null",
                    "X-Forwarded-Proto": "https",
                    "Cookie": challenge_cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                authenticated=False,
            )
            self.assertEqual(status, 401)
            self.assertIn("No se pudo iniciar sesión", body.decode("utf-8"))

            status, _, body = self.request(
                "POST",
                "/login",
                body=urlencode({
                    "password": "incorrect-owner-password",
                    "csrf_token": challenge,
                }),
                headers={
                    "Host": preview_host,
                    "Origin": "null",
                    "X-Forwarded-Proto": "https",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                authenticated=False,
            )
            self.assertEqual(status, 403)
            self.assertIn("solicitud de acceso caducó", body.decode("utf-8").lower())

            status, _, _ = self.request(
                "GET",
                "/api/library",
                headers={
                    "Host": preview_host,
                    "Origin": "null",
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 403)

            status, challenge_headers, login_page = self.request(
                "GET",
                "/login",
                headers={
                    "Host": preview_host,
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 200)
            challenge = re.search(
                rb'name="csrf_token" value="([^"]+)"',
                login_page,
            ).group(1).decode("ascii")
            challenge_cookie = self.cookie_from_headers(
                challenge_headers,
                "matrix_motion_login_csrf",
            )
            status, _, _ = self.request(
                "GET",
                "/favicon.ico",
                headers={
                    "Host": preview_host,
                    "X-Forwarded-Proto": "https",
                },
                authenticated=False,
            )
            self.assertEqual(status, 204)
            status, session_headers, _ = self.request(
                "POST",
                "/login",
                body=urlencode({
                    "password": "unit-test-owner-passphrase",
                    "csrf_token": challenge,
                }),
                headers={
                    "Host": preview_host,
                    "Origin": "null",
                    "X-Forwarded-Proto": "https",
                    "Cookie": challenge_cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                authenticated=False,
            )
            self.assertEqual(status, 303)
            session_cookie = self.cookie_from_headers(
                session_headers,
                "matrix_motion_session",
            )
            self.assertTrue(session_cookie)
            status, _, body = self.request(
                "GET",
                "/api/library",
                headers={
                    "Host": preview_host,
                    "X-Forwarded-Proto": "https",
                    "Cookie": session_cookie,
                },
                authenticated=False,
            )
            self.assertEqual(status, 200)
            self.assertIn("items", json.loads(body.decode("utf-8")))

            status, _, _ = self.request(
                "POST",
                "/login",
                body=b"probe",
                headers={
                    "Host": preview_host,
                    "Origin": f"https://{preview_host}",
                    "Content-Type": "text/plain",
                },
                authenticated=False,
            )
            self.assertEqual(status, 400)

            internal_preview_headers = {
                "Host": f"127.0.0.1:{self.port}",
                "Origin": "https://replit.com",
                "X-Forwarded-Host": preview_host,
                "X-Forwarded-Proto": "https",
                "Content-Type": "text/plain",
            }
            status, _, _ = self.request(
                "POST",
                "/login",
                body=b"probe",
                headers=internal_preview_headers,
                authenticated=False,
            )
            self.assertEqual(status, 400)

            for invalid_headers in (
                {**internal_preview_headers, "X-Forwarded-Host": "attacker.example"},
                {**internal_preview_headers, "X-Forwarded-Proto": "http"},
                {key: value for key, value in internal_preview_headers.items() if key != "X-Forwarded-Host"},
            ):
                status, _, _ = self.request(
                    "POST",
                    "/login",
                    body=b"probe",
                    headers=invalid_headers,
                    authenticated=False,
                )
                self.assertEqual(status, 403)

            status, _, _ = self.request(
                "POST",
                "/login",
                body=b"probe",
                headers={
                    "Host": preview_host,
                    "Origin": "https://attacker.example",
                    "Content-Type": "text/plain",
                },
                authenticated=False,
            )
            self.assertEqual(status, 403)

            status, _, _ = self.request(
                "POST",
                "/login",
                body=b"probe",
                headers={
                    "Host": "attacker.example",
                    "Origin": "https://attacker.example",
                    "Content-Type": "text/plain",
                },
                authenticated=False,
            )
            self.assertEqual(status, 403)

            status, _, _ = self.request(
                "POST",
                "/login",
                body=b"probe",
                headers={
                    "Host": f"127.0.0.1:{self.port}",
                    "X-Forwarded-Host": preview_host,
                    "Origin": f"https://{preview_host}",
                    "Content-Type": "text/plain",
                },
                authenticated=False,
            )
            self.assertEqual(status, 400)

    def test_idle_and_absolute_session_expiration_are_enforced(self):
        def expire_session(column):
            raw_token = self.session_cookie.split("=", 1)[1]
            token_hash = self.server.auth._hash("session", raw_token)
            with sqlite3.connect(self.panel.store.database_path) as db:
                db.execute(
                    f"UPDATE panel_sessions SET {column} = 0 WHERE token_hash = ?",
                    (token_hash,),
                )

        expire_session("last_seen_at")
        status, _, _ = self.request(
            "GET",
            "/api/library",
            headers={"Cookie": self.session_cookie},
            authenticated=False,
        )
        self.assertEqual(status, 401)

        self.login()
        expire_session("expires_at")
        status, _, _ = self.request(
            "GET",
            "/api/library",
            headers={"Cookie": self.session_cookie},
            authenticated=False,
        )
        self.assertEqual(status, 401)

    def test_changing_owner_password_revokes_existing_sessions(self):
        with patch.dict(os.environ, {"PANEL_OWNER_PASSWORD": "rotated-owner-passphrase-2026"}):
            OwnerAuth(self.panel.store.database_path)
        status, _, _ = self.request(
            "GET",
            "/api/library",
            headers={"Cookie": self.session_cookie},
            authenticated=False,
        )
        self.assertEqual(status, 401)

    def test_csrf_is_required_and_logout_revokes_the_session(self):
        status, _, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["csrf_token"])

        style_before = self.panel.style()
        status, _, _ = self.request(
            "POST",
            "/api/proposals",
            body=json.dumps({
                "source_id": "a" * 32,
                "subtitle_mode": "none",
                "output_fps": 50,
            }).encode(),
            headers={
                "Cookie": self.session_cookie,
                "X-CSRF-Token": "invalid",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 403)

        status, _, body = self.request(
            "PUT",
            "/api/style",
            body=json.dumps({**style_before, "font_size": style_before["font_size"] + 1}).encode(),
            headers={
                "Cookie": self.session_cookie,
                "Content-Type": "application/json",
            },
            authenticated=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.panel.style(), style_before)

        status, _, _ = self.request("POST", "/logout", authenticated=True)
        self.assertEqual(status, 204)
        status, _, _ = self.request(
            "GET", "/api/library",
            headers={"Cookie": self.session_cookie},
            authenticated=False,
        )
        self.assertEqual(status, 401)

    def test_uploads_mov_validates_metadata_and_supports_video_ranges(self):
        path = self.create_movie()
        style_notes = "Luz natural y planos de producto; revisar cierre."
        status, response = self.upload(
            path,
            niche="beauty_skincare",
            style_notes=style_notes,
        )
        self.assertEqual(status, 201)
        item = response["item"]
        self.assertEqual(item["format"], "MOV")
        self.assertEqual(item["video_codec"], "h264")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["width"], 360)
        self.assertEqual(item["height"], 640)
        self.assertEqual(item["niche"], "beauty_skincare")
        self.assertEqual(item["niche_label"], "Beauty & Skincare")
        self.assertEqual(item["style_notes"], style_notes)
        self.assertEqual(item["editing_style"], "dynamic")
        self.assertIsInstance(item["created_at"], (int, float))
        self.assertTrue(item["thumbnail_url"])
        stored_upload = self.panel.store.get_upload(item["id"])
        self.assertEqual(json.loads(stored_upload["metadata_json"])["style_notes"], style_notes)

        status, headers, payload = self.request("GET", item["thumbnail_url"])
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["Content-Type"], "image/jpeg")
        self.assertTrue(payload.startswith(b"\xff\xd8"))

        status, headers, payload = self.request(
            "GET", f"/media/incoming/{item['id']}", headers={"Range": "bytes=0-15"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(payload), 16)
        self.assertTrue(dict(headers)["Content-Range"].startswith("bytes 0-15/"))

        status, _, body = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["items"][0]["id"], item["id"])

    def test_upload_rejects_unknown_niches_styles_and_oversized_style_notes(self):
        path = self.create_movie()
        status, response = self.upload(path, niche="unknown")
        self.assertEqual(status, 400)
        self.assertIn("categoría", response["detail"])

        status, response = self.upload(path, editing_style="unsupported")
        self.assertEqual(status, 400)
        self.assertIn("estilo de edición", response["detail"])

        status, response = self.upload(path, editing_style="natural_premium")
        self.assertEqual(status, 201)
        self.assertEqual(response["item"]["editing_style"], "natural_premium")

        status, response = self.upload(path, style_notes="x" * 501)
        self.assertEqual(status, 400)
        self.assertIn("500 caracteres", response["detail"])

        status, response = self.upload(path, style_notes="highlightColor=rosa")
        self.assertEqual(status, 400)
        self.assertIn("color hexadecimal", response["detail"])

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

    def test_retry_endpoint_requires_owner_session(self):
        source_id, job_id, _, _ = self.create_failed_0415_attempt()
        status, _, body = self.request(
            "POST",
            f"/api/jobs/{job_id}/retry",
            body=json.dumps({
                "authorize_paid_apis": True,
                "confirm_single_retry": True,
                "confirm_no_render_id": True,
                "unknown_cost_acknowledged": True,
            }).encode(),
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.panel.store.latest_job_for_source(source_id)["id"], job_id)

    def test_authenticated_dashboard_has_four_pages_and_no_stage_retry_controls(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn('data-page="home"', html)
        self.assertIn('data-page="create"', html)
        self.assertIn('data-page="library"', html)
        self.assertIn('data-page="settings"', html)
        self.assertNotIn("Shotstack", html)
        self.assertNotIn('id="retry-render-button"', html)
        self.assertNotIn('id="retry-final-render-button"', html)
        status, _, script = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertNotIn("authorizeRenderRetry", script.decode("utf-8"))

    def test_render_retries_are_blocked_in_modal_only_mode(self):
        source_id, job_id, _, _ = self.create_failed_0415_attempt()
        payload = {
            "authorize_modal_compute": True,
            "authorize_paid_apis": True,
            "confirm_single_retry": True,
            "confirm_no_render_id": True,
            "unknown_cost_acknowledged": True,
        }
        with (
            patch.object(self.panel.executor, "submit") as submit,
            patch("video_pipeline.webapp.ShotstackClient", side_effect=AssertionError("no provider calls")),
        ):
            with self.assertRaises(PanelError) as blocked:
                self.panel.retry_job(job_id, payload)
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertIn("deshabilitados", str(blocked.exception))
            status, _, body = self.request(
                "POST",
                f"/api/jobs/{job_id}/retry",
                body=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 409)
            self.assertIn("deshabilitados", json.loads(body.decode())["detail"])
            status, _, body = self.request(
                "POST",
                f"/api/jobs/{job_id}/retry-third",
                body=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 409)
            self.assertIn("deshabilitados", json.loads(body.decode())["detail"])
            submit.assert_not_called()
        self.assertEqual(len(self.panel.store.job_history_for_source(source_id)), 1)

    def test_controlled_third_attempt_is_target_bound_one_shot_and_owner_authorized(self):
        self.skipTest("El tercer intento de Stage se ha retirado; el bloqueo Modal-only se prueba aparte.")
        source_id, first_job_id, upload, saved = self.create_failed_0415_attempt()
        retry_options = {
            "subtitle_mode": "none",
            "output_profile": "1080p",
            "output_quality": "high",
            "output_fps": 50,
            "creative_plan": saved["creative_plan"],
        }
        second_job_id = "b" * 32
        self.panel.store.create_retry_job(first_job_id, second_job_id, None, retry_options)
        second_error = "Shotstack render submission failed (HTTP 400): Validation failed for timeline"
        self.panel.store.update_job(
            second_job_id,
            status="failed",
            phase="failed",
            error=second_error,
        )
        source_path = self.panel._source_path(upload)
        self.panel.store.ledger.claim(
            upload["source_sha256"],
            str(source_path),
            retry_failed=True,
            edit_options=saved,
        )
        self.panel.store.ledger.mark_failed(upload["source_sha256"], second_error)

        target = {
            "job_id": second_job_id,
            "previous_job_id": first_job_id,
            "source_id": source_id,
            "source_sha256": upload["source_sha256"],
            "filename": "0415.mov",
        }
        self.assertFalse(next(
            entry for entry in self.panel.library() if entry["id"] == source_id
        )["can_run_controlled_third_render"])
        base_payload = {
            "authorize_paid_apis": True,
            "confirm_single_retry": True,
            "confirm_final_third_attempt": True,
            "confirm_no_render_id": True,
            "confirm_drive_output_write": True,
            "unknown_cost_acknowledged": True,
        }
        with (
            patch("video_pipeline.webapp.RENDER001_THIRD_RETRY_TARGET", target),
            patch.object(self.panel, "health", return_value={
                "shotstack_key_configured": True,
                "openai_key_configured": False,
                "shotstack_environment": "stage",
            }),
            patch.object(self.panel.executor, "submit") as submit,
            patch("video_pipeline.webapp.ShotstackClient", side_effect=AssertionError("no provider calls in preparation")),
        ):
            item = next(entry for entry in self.panel.library() if entry["id"] == source_id)
            self.assertTrue(item["can_run_controlled_third_render"])
            status, _, _ = self.request(
                "POST",
                f"/api/jobs/{second_job_id}/retry-third",
                body=json.dumps(base_payload).encode(),
                headers={"Content-Type": "application/json"},
                authenticated=False,
            )
            self.assertEqual(status, 401)
            self.assertEqual(len(self.panel.store.job_history_for_source(source_id)), 2)
            submit.assert_not_called()

            status, _, body = self.request(
                "POST",
                f"/api/jobs/{first_job_id}/retry-third",
                body=json.dumps(base_payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
            submit.assert_not_called()

            status, _, body = self.request(
                "POST",
                f"/api/jobs/{second_job_id}/retry-third",
                body=json.dumps({**base_payload, "confirm_final_third_attempt": False}).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)
            self.assertIn("autorización explícita", json.loads(body.decode())["detail"])
            submit.assert_not_called()

            status, _, body = self.request(
                "POST",
                f"/api/jobs/{second_job_id}/retry-third",
                body=json.dumps(base_payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 202)
            third = json.loads(body.decode())
            self.assertEqual(third["attempt_number"], 3)
            self.assertEqual(third["retry_of_job_id"], second_job_id)
            self.assertTrue(third["controlled_third_attempt"])
            self.assertEqual(third["edit_options"], retry_options)
            self.assertTrue(submit.called)
            self.assertTrue(submit.call_args.kwargs["allow_third_attempt"])
            self.assertTrue(submit.call_args.kwargs["retry_failed"])

            history = self.panel.store.job_history_for_source(source_id)
            self.assertEqual([row["attempt_number"] for row in history], [1, 2, 3])
            self.assertEqual(history[0]["id"], first_job_id)
            self.assertEqual(history[0]["error"], "Shotstack render submission failed (HTTP 400).")
            self.assertEqual(history[1]["id"], second_job_id)
            self.assertEqual(history[1]["error"], second_error)
            self.assertEqual(history[2]["retry_of_job_id"], second_job_id)
            self.assertFalse(next(
                entry for entry in self.panel.library() if entry["id"] == source_id
            )["can_run_controlled_third_render"])

            status, _, _ = self.request(
                "POST",
                f"/api/jobs/{second_job_id}/retry-third",
                body=json.dumps(base_payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 409)
            self.assertEqual(len(self.panel.store.job_history_for_source(source_id)), 3)
            self.assertEqual(submit.call_count, 1)

    def test_completed_render_keeps_mp4_and_audio_metadata_available_for_review(self):
        source_id, job_id, _, _ = self.create_failed_0415_attempt()
        generated = self.create_movie(".mp4")
        output_path = self.panel.output_dir / "0415_vertical_edited.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated, output_path)
        measured_output = probe_video(output_path)
        result_metadata = {
            "output_dimensions": {"width": measured_output.width, "height": measured_output.height},
            "output_fps": measured_output.fps,
            "output_video_codec": measured_output.video_codec,
            "output_audio_codec": measured_output.audio_codec,
            "output_quality_preset": "high",
            "output_video_bit_rate_bps": measured_output.video_bit_rate_bps,
            "output_format_bit_rate_bps": measured_output.format_bit_rate_bps,
            "output_duration_seconds": measured_output.duration,
        }
        self.panel.store.update_job(
            job_id,
            status="completed",
            phase="completed",
            progress=100,
            output_id=output_path.name,
            result_json=json.dumps(result_metadata),
        )

        job = self.panel.get_job(job_id)
        self.assertEqual(job["output_id"], output_path.name)
        self.assertEqual(job["output_metadata"]["audio_codec"], measured_output.audio_codec)
        self.assertEqual(
            job["output_metadata"]["dimensions"],
            {"width": measured_output.width, "height": measured_output.height},
        )
        self.assertTrue(output_path.is_file())
        status, headers, body = self.request("GET", f"/media/outputs/{output_path.name}")
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["Content-Type"], "video/mp4")
        self.assertEqual(body, output_path.read_bytes())
        status, headers, body = self.request(
            "GET",
            f"/media/outputs/{output_path.name}",
            headers={"Range": "bytes=0-9"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, output_path.read_bytes()[:10])
        self.assertEqual(dict(headers)["Content-Type"], "video/mp4")

    def test_retry_is_locked_to_stage(self):
        self.skipTest("No hay render backend Stage en el panel; la ruta de reintento está bloqueada.")
        _, job_id, _, _ = self.create_failed_0415_attempt()
        with (
            patch.dict(os.environ, {"SHOTSTACK_ENVIRONMENT": "v1"}),
            patch.object(self.panel, "health", return_value={
                "shotstack_key_configured": True,
                "openai_key_configured": False,
                "shotstack_environment": "v1",
            }),
            patch("video_pipeline.webapp.ShotstackClient") as client_class,
            patch.object(self.panel.executor, "submit") as submit,
        ):
            with self.assertRaises(PanelError) as blocked:
                self.panel.retry_job(job_id, {
                    "authorize_paid_apis": True,
                    "confirm_single_retry": True,
                    "confirm_no_render_id": True,
                    "unknown_cost_acknowledged": True,
                })
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("Stage", str(blocked.exception))
        client_class.assert_not_called()
        submit.assert_not_called()

    def test_drive_retry_requires_explicit_new_output_confirmation(self):
        self.skipTest("El reintento del render Stage se ha retirado; los reintentos de subida Drive se prueban por separado.")
        source_id, job_id, _, _ = self.create_failed_0415_attempt()
        drive_import = {
            "drive_file_id": "drive-file-id",
            "status": "render_failed",
        }
        with (
            patch.object(
                self.panel.drive_store,
                "get_import_for_local_upload",
                return_value=drive_import,
            ),
            patch.object(
                self.panel,
                "drive_status",
                return_value={"ready": True, "authorized": True},
            ),
            patch.object(self.panel, "drive_client", new=SimpleNamespace()),
            patch(
                "video_pipeline.webapp.PipelineConfig.from_environment",
                return_value=SimpleNamespace(
                    shotstack_environment="stage",
                    processed_folder_id="processed-folder",
                ),
            ),
            patch.object(self.panel, "health", return_value={
                "shotstack_key_configured": True,
                "openai_key_configured": False,
                "shotstack_environment": "stage",
            }),
            patch.object(self.panel.executor, "submit") as submit,
            patch("video_pipeline.webapp.ShotstackClient", side_effect=AssertionError("no remote ID")),
        ):
            item = next(item for item in self.panel.library() if item["id"] == source_id)
            self.assertTrue(item["can_retry_render"])
            self.assertTrue(item["retry_will_upload_to_drive"])
            consent = {
                "authorize_paid_apis": True,
                "confirm_single_retry": True,
                "confirm_no_render_id": True,
                "unknown_cost_acknowledged": True,
            }
            with self.assertRaises(PanelError) as missing_consent:
                self.panel.retry_job(job_id, consent)
            self.assertEqual(missing_consent.exception.status_code, 403)
            self.assertIn("MP4 nuevo", str(missing_consent.exception))
            self.assertEqual(len(self.panel.store.job_history_for_source(source_id)), 1)
            submit.assert_not_called()

            retry = self.panel.retry_job(
                job_id,
                {**consent, "confirm_drive_output_write": True},
            )
            self.assertEqual(retry["attempt_number"], 2)
            submit.assert_called_once()

    def test_retry_blocks_when_shotstack_reports_an_active_remote_render(self):
        self.skipTest("El panel ya no consulta ni reenvía renders Stage; todos los reintentos están bloqueados.")
        source_id, job_id, _, _ = self.create_failed_0415_attempt(render_id="stage-render-id")
        with (
            patch.object(self.panel, "health", return_value={
                "shotstack_key_configured": True,
                "openai_key_configured": False,
                "shotstack_environment": "stage",
            }),
            patch("video_pipeline.webapp.ShotstackClient") as client_class,
            patch.object(self.panel.executor, "submit") as submit,
        ):
            client_class.return_value.render_status.return_value = ("rendering", None)
            with self.assertRaises(PanelError) as blocked:
                self.panel.retry_job(job_id, {
                    "authorize_paid_apis": True,
                    "confirm_single_retry": True,
                    "unknown_cost_acknowledged": True,
                })
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertIn("activo", str(blocked.exception))
        self.assertEqual(len(self.panel.store.job_history_for_source(source_id)), 1)
        submit.assert_not_called()

    def test_local_edit_proposal_defaults_to_no_subtitles_without_provider_calls(self):
        style_notes = (
            "Resaltar el producto y conservar un tono limpio.\n"
            "fontFamily=Montserrat\n"
            "highlightColor=#FF2A85\n"
            "textColor=#000000"
        )
        status, response = self.upload(
            self.create_movie(".mov"),
            niche="beauty_skincare",
            style_notes=style_notes,
        )
        self.assertEqual(status, 201)
        source_id = response["item"]["id"]
        with patch.object(self.panel, "health", side_effect=AssertionError("preflight must not inspect providers")):
            status, _, body = self.request(
                "POST",
                "/api/proposals",
                body=json.dumps({
                    "source_id": source_id,
                    "subtitle_mode": "none",
                    "output_fps": 30,
                }).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        proposal = json.loads(body.decode("utf-8"))
        self.assertEqual(proposal["edit"]["subtitle_mode"], "none")
        self.assertEqual(proposal["edit"]["niche"], "beauty_skincare")
        self.assertEqual(proposal["edit"]["style_notes"], style_notes)
        self.assertEqual(proposal["edit"]["caption_style"], {
            "fontFamily": "Montserrat",
            "highlightColor": "#FFC0CB",
            "textColor": "#000000",
            "fontSize": 54,
            "position": "bottom",
            "maxWordsPerCaption": 2,
        })
        proposal_options = self.panel.pending_proposals[proposal["proposal_id"]]["options"]
        self.assertEqual(proposal_options["style_notes"], style_notes)
        self.assertEqual(proposal_options["caption_style_overrides"], {
            "font_family": "Montserrat",
            "highlight_color": "#FF2A85",
            "font_color": "#000000",
        })
        self.assertEqual(proposal["edit"]["output_fps"], 30)
        self.assertEqual((proposal["edit"]["output_width"], proposal["edit"]["output_height"]), (1080, 1920))
        self.assertEqual(proposal["edit"]["output_quality"], "high")
        self.assertEqual(proposal["edit"]["expected_output_duration_seconds"], proposal["source"]["duration_seconds"])
        self.assertEqual(proposal["edit"]["cut_count"], 0)
        self.assertEqual(proposal["source"]["origin"], "Subida local")
        self.assertFalse(proposal["api_plan"]["openai_transcription"])
        self.assertEqual(proposal["api_plan"]["openai_transcription_chunks"], 0)
        self.assertEqual(proposal["api_plan"]["external_services_called_during_proposal"], [])

    def test_modal_proposal_is_local_and_discloses_unknown_compute_cost(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        source_id = response["item"]["id"]
        with patch(
            "video_pipeline.webapp.modal_configuration_status",
            return_value={
                "sdk_installed": True,
                "credentials_configured": True,
                "app_name": "matrix-motion-remotion",
                "function_name": "render_master",
                "deployment_status": "not_checked",
            },
        ):
            status, _, body = self.request(
                "POST",
                "/api/proposals",
                body=json.dumps({
                    "source_id": source_id,
                    "subtitle_mode": "none",
                    "output_fps": 30,
                    "output_profile": "1080p",
                    "output_quality": "high",
                    "render_backend": "modal",
                }).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200)
        proposal = json.loads(body.decode("utf-8"))
        self.assertEqual(proposal["api_plan"]["renderer_backend"], "modal")
        self.assertFalse(proposal["api_plan"]["shotstack_key_configured"])
        self.assertEqual(proposal["api_plan"]["shotstack_render_jobs"], 0)
        self.assertIsNone(proposal["costs"]["modal_compute_usd"])
        self.assertIsNone(proposal["costs"]["total_estimate_usd"])
        self.assertIn("tarifa cotizada", proposal["costs"]["estimate_note"])
        self.assertEqual(proposal["edit"]["output_fps"], 30)
        self.assertEqual(proposal["api_plan"]["external_services_called_during_proposal"], [])

    def test_modal_render_requires_separate_compute_and_unknown_cost_approval(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        source_id = response["item"]["id"]
        status, _, body = self.request(
            "POST",
            "/api/proposals",
            body=json.dumps({
                "source_id": source_id,
                "subtitle_mode": "none",
                "output_fps": 30,
                "output_profile": "1080p",
                "output_quality": "high",
                "render_backend": "modal",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        proposal = json.loads(body.decode())
        proposal_id = proposal["proposal_id"]
        status, _, body = self.request(
            "POST",
            "/api/jobs",
            body=json.dumps({
                "proposal_id": proposal_id,
                "authorize_paid_apis": False,
                "authorize_modal_compute": False,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("autorización explícita", json.loads(body.decode())["detail"])
        status, _, body = self.request(
            "POST",
            "/api/jobs",
            body=json.dumps({
                "proposal_id": proposal_id,
                "authorize_paid_apis": False,
                "authorize_modal_compute": True,
                "unknown_cost_acknowledged": False,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertIn("coste estimado es desconocido", json.loads(body.decode())["detail"])
        self.assertEqual(self.panel.store.job_history_for_source(source_id), [])
        self.assertEqual(proposal["costs"]["openai_usd"], 0.0)
        self.assertIsNone(proposal["costs"]["total_estimate_usd"])
        self.assertIn(proposal["proposal_id"], self.panel.pending_proposals)

    def test_local_proposal_uses_fixed_modal_profile_without_external_calls(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        proposal = self.panel.prepare_proposal({
            "source_id": response["item"]["id"],
            "subtitle_mode": "none",
            "output_fps": 30,
            "output_profile": "1080p",
            "output_quality": "high",
            "creative_plan": {"cuts": [], "zooms": [], "stickers": []},
        })
        self.assertEqual((proposal["edit"]["output_width"], proposal["edit"]["output_height"]), (1080, 1920))
        self.assertEqual(proposal["edit"]["output_quality"], "high")
        self.assertEqual(proposal["api_plan"]["external_services_called_during_proposal"], [])
        with self.assertRaises(PanelError):
            self.panel.prepare_proposal({
                "source_id": response["item"]["id"],
                "subtitle_mode": "none",
                "output_fps": 50,
                "output_profile": "720p",
                "output_quality": "medium",
                "creative_plan": {"cuts": [], "zooms": [], "stickers": []},
            })

    def test_4k_profile_requires_a_4k_source(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        with self.assertRaises(PanelError) as blocked:
            self.panel.prepare_proposal({
                "source_id": response["item"]["id"],
                "subtitle_mode": "none",
                "output_fps": 50,
                "output_profile": "2160p",
            })
        self.assertIn("2160 × 3840", str(blocked.exception))

    def test_4k_source_defaults_to_4k_and_60_fps_without_external_calls(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        source = SimpleNamespace(
            width=2160,
            height=3840,
            duration=18.82,
            video_codec="hevc",
            audio_codec="aac",
            fps=60.0,
            video_bit_rate_bps=20_000_000,
            format_bit_rate_bps=22_000_000,
            has_audio=True,
            rotation=0,
        )
        with patch("video_pipeline.webapp.probe_video", return_value=source):
            proposal = self.panel.prepare_proposal({
                "source_id": response["item"]["id"],
                "subtitle_mode": "none",
            })
        self.assertEqual(proposal["edit"]["output_profile"], "2160p")
        self.assertEqual(
            (proposal["edit"]["output_width"], proposal["edit"]["output_height"]),
            (2160, 3840),
        )
        self.assertEqual(proposal["edit"]["output_fps"], 60)
        self.assertIn("2160 × 3840", proposal["edit"]["higher_resolution_note"])
        self.assertTrue(any("2160 × 3840 a 60 FPS" in item for item in proposal["warnings"]))
        self.assertEqual(proposal["api_plan"]["external_services_called_during_proposal"], [])

    def test_render_requires_approved_single_use_local_proposal(self):
        status, response = self.upload(self.create_movie(".mp4"))
        self.assertEqual(status, 201)
        proposal = self.panel.prepare_proposal({
            "source_id": response["item"]["id"],
            "subtitle_mode": "none",
            "output_fps": 30,
        })
        base_payload = {
            "proposal_id": proposal["proposal_id"],
            "unknown_cost_acknowledged": True,
        }
        with self.assertRaises(PanelError) as missing_approval:
            self.panel.start_job({
                **base_payload,
                "authorize_paid_apis": False,
                "authorize_gemini_analysis": True,
            })
        self.assertEqual(missing_approval.exception.status_code, 403)

        with (
            patch.object(
                self.panel,
                "health",
                return_value={
                    "openai_key_configured": False,
                    "gemini_key_configured": True,
                    "shotstack_key_configured": True,
                    "shotstack_environment": "stage",
                    "modal": {
                        "sdk_installed": True,
                        "credentials_configured": True,
                    },
                    "r2_storage": {"ready": True},
                },
            ),
            patch.object(self.panel, "drive_status", return_value={"ready": False, "authorized": False}),
            patch.object(self.panel.executor, "submit") as submit,
        ):
            job = self.panel.start_job({
                **base_payload,
                "authorize_paid_apis": True,
                "authorize_modal_compute": True,
                "authorize_gemini_analysis": True,
            })
            self.assertEqual(job["edit_options"]["subtitle_mode"], "none")
            self.assertEqual(job["edit_options"]["output_fps"], 30)
            submit.assert_called_once()
            with self.assertRaises(PanelError) as repeated:
                self.panel.start_job({
                    **base_payload,
                    "authorize_paid_apis": True,
                    "authorize_modal_compute": True,
                    "authorize_gemini_analysis": True,
                })

        self.assertEqual(repeated.exception.status_code, 409)

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
        style["emoji_by_keyword"] = {"viaje": "✈️"}
        style["show_subtitles"] = False
        style["logo_position"] = "bottomLeft"
        style["logo_scale"] = 0.18
        style["logo_offset_x"] = 0.1
        style["logo_offset_y"] = -0.12
        status, _, body = self.request(
            "PUT",
            "/api/style",
            body=json.dumps(style).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        saved = json.loads(body.decode("utf-8"))
        self.assertEqual(saved["font_size"], 72)
        self.assertEqual(saved["emoji_by_keyword"], {"viaje": "✈️"})
        self.assertFalse(saved["show_subtitles"])
        self.assertEqual(saved["logo_position"], "bottomLeft")
        self.assertEqual(saved["logo_scale"], 0.18)

        status, _, body = self.request("GET", "/api/style")
        self.assertEqual(status, 200)
        saved = json.loads(body.decode("utf-8"))
        self.assertEqual(saved["font_size"], 72)
        self.assertEqual(saved["emoji_by_keyword"], {"viaje": "✈️"})
        self.assertFalse(saved["show_subtitles"])

    def test_drive_routes_require_owner_and_oauth_is_disabled_without_consent(self):
        status, _, body = self.request(
            "GET", "/api/drive/status", authenticated=False
        )
        self.assertEqual(status, 401)

        status, _, body = self.request("GET", "/api/drive/status")
        self.assertEqual(status, 200)
        drive_status = json.loads(body.decode("utf-8"))
        self.assertFalse(drive_status["enabled"])
        self.assertFalse(drive_status["authorized"])
        self.assertFalse(drive_status["verified"])
        self.assertNotIn("client_secret", body.decode("utf-8"))

        status, _, body = self.request(
            "POST",
            "/api/drive/oauth/start",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 409)
        self.assertIn("desactivada", json.loads(body.decode("utf-8"))["detail"])

        status, _, body = self.request(
            "POST",
            "/api/drive/scan",
            body=b"{}",
            headers={"Content-Type": "application/json", "X-CSRF-Token": ""},
        )
        self.assertEqual(status, 403)

        status, _, body = self.request(
            "POST",
            "/api/drive/scan",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 409)
        self.assertIn("no está conectado", json.loads(body.decode("utf-8"))["detail"])

    def test_oauth_start_requires_owner_csrf_and_callback_requires_one_time_state(self):
        self.panel.drive_oauth = GoogleDriveOAuth(
            DriveOAuthConfig(
                enabled=True,
                client_id="test-client.apps.googleusercontent.com",
                client_secret="test-client-secret",
                redirect_uri="https://matrix.example.test/oauth/google/callback",
                session_secret=os.environ["SESSION_SECRET"],
                allowed_email="automation@gmail.com",
            ),
            self.panel.drive_store,
        )

        status, _, _ = self.request(
            "POST",
            "/api/drive/oauth/start",
            body=b"{}",
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        self.assertEqual(status, 401)

        status, headers, body = self.request(
            "POST",
            "/api/drive/oauth/start",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        authorization_url = json.loads(body.decode("utf-8"))["authorization_url"]
        params = parse_qs(urlsplit(authorization_url).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(
            params["scope"],
            [
                "https://www.googleapis.com/auth/drive.readonly "
                "https://www.googleapis.com/auth/drive.file openid email"
            ],
        )
        state_cookie = self.cookie_from_headers(
            headers, "matrix_motion_google_oauth_state"
        )
        cookie_header = next(
            value for name, value in headers
            if name.lower() == "set-cookie"
            and value.startswith("matrix_motion_google_oauth_state=")
        )
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("Secure", cookie_header)
        self.assertIn("SameSite=Lax", cookie_header)
        self.assertIn("Path=/oauth/google/callback", cookie_header)

        status, _, body = self.request(
            "GET",
            "/oauth/google/callback?state=wrong&code=unused",
            headers={"Cookie": state_cookie},
            authenticated=False,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("unused", body.decode("utf-8"))

    def test_oauth_connect_button_is_wired_without_picker_configuration(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'id="drive-connect-button" type="button" disabled>Conectar Google Drive (OAuth)',
            html,
        )
        self.assertIn(
            'byId("drive-connect-button").addEventListener("click"',
            javascript,
        )
        self.assertIn("const result = await startDriveOAuth();", javascript)
        self.assertIn("window.location.assign(result.authorization_url);", javascript)
        self.assertIn("connect.disabled = drive.ready !== true;", javascript)

    def test_write_probe_uses_server_oauth_without_picker_token_flow(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "web" / "app.js").read_text(encoding="utf-8")
        start = javascript.index("async function testDriveWrite()")
        end = javascript.index("async function retryDriveUpload", start)
        helper = javascript[start:end]
        self.assertIn("requestJson(API.driveWriteTest", helper)
        self.assertNotIn("postDrive(", helper)
        self.assertNotIn("getDriveAccessToken", helper)

        start = javascript.index('byId("drive-write-test-button").addEventListener("click"')
        end = javascript.index('byId("retry-drive-upload-button").addEventListener("click"', start)
        handler = javascript[start:end]
        self.assertIn("await testDriveWrite()", handler)
        self.assertNotIn("startDriveOAuth", handler)
        self.assertNotIn("openDrivePicker", handler)

    def test_drive_write_test_route_requires_owner_csrf_and_explicit_confirmation(self):
        with patch.object(
            self.panel,
            "test_drive_output_write",
            return_value={
                "created_and_deleted": True,
                "mime_type": "video/mp4",
                "destination": "02_PROCESADOS",
            },
        ) as write_probe:
            status, _, _ = self.request(
                "POST",
                "/api/drive/test-write",
                body=b'{"confirm":true}',
                headers={"Content-Type": "application/json"},
                authenticated=False,
            )
            self.assertEqual(status, 401)

            status, _, _ = self.request(
                "POST",
                "/api/drive/test-write",
                body=b'{"confirm":false}',
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 400)
            write_probe.assert_not_called()

            status, _, body = self.request(
                "POST",
                "/api/drive/test-write",
                body=b'{"confirm":true}',
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body.decode("utf-8"))["created_and_deleted"])
            write_probe.assert_called_once_with({"confirm": True})

    def test_write_probe_uploads_and_deletes_only_its_synthetic_mp4_with_fake_drive(self):
        class FakeDriveClient:
            def __init__(self):
                self.uploaded_name = None
                self.deleted_id = None

            def upload_new_file(self, path, *, folder_id, app_properties):
                path = Path(path)
                self.uploaded_name = path.name
                self.assert_valid = path.is_file() and path.stat().st_size > 0
                self.assert_folder = folder_id
                self.assert_source = app_properties["matrixMotionSource"]
                return {
                    "id": "f" * 20,
                    "name": path.name,
                    "mimeType": "video/mp4",
                    "parents": [folder_id],
                }

            def delete_created_file(self, file_id):
                self.deleted_id = file_id

        fake = FakeDriveClient()
        self.panel.drive_oauth = SimpleNamespace()
        self.panel.drive_client = fake
        with patch.object(
            self.panel,
            "drive_status",
            return_value={
                "ready": True,
                "authorized": True,
                "identity_verified": True,
                "needs_reauthorization": False,
            },
        ):
            result = self.panel.test_drive_output_write(
                {"confirm": True}
            )
        self.assertTrue(result["created_and_deleted"])
        self.assertEqual(result["mime_type"], "video/mp4")
        self.assertEqual(result["destination"], "02_PROCESADOS")
        self.assertTrue(fake.uploaded_name.startswith("matrix-motion-write-test-"))
        self.assertTrue(fake.uploaded_name.endswith(".mp4"))
        self.assertTrue(fake.assert_valid)
        self.assertEqual(fake.assert_folder, "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN")
        self.assertRegex(fake.assert_source, r"^[a-f0-9]{64}$")
        self.assertEqual(fake.deleted_id, "f" * 20)

    def test_picker_drive_actions_fail_closed_before_using_unverified_tokens(self):
        token_payload = {"access_token": "temporary-token"}
        with (
            patch.object(
                self.panel.drive_oauth,
                "verify_access_token_identity",
                side_effect=GoogleDriveError("secondary account check failed"),
            ) as verify,
            patch("video_pipeline.webapp.get_folder") as get_folder,
            patch("video_pipeline.webapp.get_drive_file") as get_drive_file,
            patch("video_pipeline.webapp.sync_drive_media") as sync_media,
            patch("video_pipeline.webapp.create_test_file") as create_test_file,
            patch(
                "video_pipeline.webapp.load_folder_selections",
                return_value={"procesados": {"id": "a" * 20, "name": "Procesados"}},
            ),
        ):
            operations = [
                lambda: self.panel.select_drive_folder(
                    {"role": "procesados", "folder_id": "a" * 20, **token_payload}
                ),
                lambda: self.panel.select_drive_files(
                    {"role": "entrada", "files": [{"id": "b" * 20}], **token_payload}
                ),
                lambda: self.panel.list_drive_files(
                    {"role": "entrada", **token_payload}
                ),
                lambda: self.panel.sync_drive(token_payload),
                lambda: self.panel.create_drive_test_file(token_payload),
            ]
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(PanelError):
                        operation()
            self.assertEqual(verify.call_count, 3)
            get_folder.assert_not_called()
            get_drive_file.assert_not_called()
            sync_media.assert_not_called()
            create_test_file.assert_not_called()

    def test_drive_upload_retry_reuses_render_without_starting_pipeline(self):
        source_id = "a" * 32
        source_hash = "b" * 64
        drive_file_id = "abcdefghijABCDEFGHIJ"
        source_path = self.panel.input_dir / "source.mp4"
        source_path.write_bytes(b"untouched-original")
        self.panel.store.add_upload(
            source_id,
            source_path.name,
            "source.mp4",
            source_hash,
            source_path.stat().st_size,
            {
                "duration": 4.0,
                "width": 1080,
                "height": 1920,
                "video_codec": "h264",
                "audio_codec": "aac",
                "rotation": 0,
                "fps": 30,
            },
        )
        output_path = self.panel.output_dir / "rendered.mp4"
        output_path.write_bytes(b"previously-rendered-video")
        self.panel.store.create_job("c" * 32, source_id, 0.02)
        self.panel.store.update_job(
            "c" * 32,
            status="failed",
            phase="failed",
            error="Fallo simulado de subida Drive",
        )
        self.panel.drive_store.add_import(
            drive_file_id=drive_file_id,
            local_upload_id=source_id,
            filename="source.mp4",
            md5_checksum=None,
            doc_file_id=None,
            style_overrides={},
            logo_path=None,
        )
        self.panel.drive_store.update_import(
            drive_file_id, status="upload_failed", error="Fallo simulado"
        )

        class FakeDriveClient:
            def __init__(self):
                self.calls = []

            def upload_new_file(self, path, **kwargs):
                self.calls.append((path, kwargs))
                return {"id": "new-drive-output"}

        fake_drive = FakeDriveClient()
        self.panel.drive_client = fake_drive
        with (
            patch.object(
                self.panel,
                "drive_status",
                return_value={"ready": True, "authorized": True},
            ),
            patch("video_pipeline.webapp.probe_video", return_value=SimpleNamespace(width=1080, height=1920)),
            patch.object(self.panel.store.ledger, "get", return_value={"output_path": str(output_path)}),
            patch.object(self.panel.store.ledger, "mark_completed") as mark_completed,
            patch("video_pipeline.webapp.process_local_video") as process_local_video,
            patch(
                "video_pipeline.webapp.PipelineConfig.from_environment",
                return_value=SimpleNamespace(
                    processed_folder_id="1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN"
                ),
            ),
            patch(
                "video_pipeline.webapp.load_folder_selections",
                return_value={
                    "procesados": {
                        "id": "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN",
                        "name": "Procesados",
                    }
                },
            ),
        ):
            result = self.panel.retry_drive_upload(source_id)

        self.assertTrue(result["render_reused"])
        self.assertEqual(result["output_drive_file_id"], "new-drive-output")
        self.assertEqual(len(fake_drive.calls), 1)
        self.assertEqual(source_path.read_bytes(), b"untouched-original")
        mark_completed.assert_called_once()
        process_local_video.assert_not_called()
        self.assertEqual(
            self.panel.drive_store.get_import(drive_file_id)["status"], "uploaded"
        )
        self.assertEqual(self.panel.store.get_job("c" * 32)["status"], "completed")

    def test_drive_scan_imports_new_video_matches_doc_and_skips_duplicate(self):
        original = self.root / "drive-original.mp4"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=24:d=0.5",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(original),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        original_bytes = original.read_bytes()
        video_remote_id = "drivevideo12345"
        doc_remote_id = "docfile1234567"
        input_folder_id = "13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg"
        logos_folder_id = "1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW"

        class FakeDriveClient:
            def __init__(self):
                self.downloads = 0
                self.listed_folders = []

            def list_children(self, folder_id):
                self.listed_folders.append(folder_id)
                if folder_id == input_folder_id:
                    return [
                        {
                        "id": video_remote_id,
                        "name": "clip.mp4",
                        "mimeType": "video/mp4",
                        "size": str(original.stat().st_size),
                        "parents": [input_folder_id],
                        },
                        {
                        "id": doc_remote_id,
                        "name": "clip",
                        "mimeType": "application/vnd.google-apps.document",
                        "parents": [input_folder_id],
                        },
                    ]
                if folder_id == logos_folder_id:
                    return []
                raise AssertionError("The scan tried to inspect an unconfigured Drive folder.")

            def export_document_text(self, file_id):
                self.assert_doc_id = file_id
                return '{"font_size": 72}'

            def download_file(self, file_id, destination):
                self.downloads += 1
                shutil.copyfile(original, destination)
                return Path(destination)

        fake_drive = FakeDriveClient()
        self.panel.drive_client = fake_drive
        with (
            patch.object(
                self.panel,
                "drive_status",
                return_value={
                    "ready": True,
                    "authorized": True,
                    "identity_verified": True,
                },
            ),
        ):
            first = self.panel.scan_drive()
            self.assertEqual(len(first["imported"]), 1)
            self.assertEqual(first["skipped"], 0)
            imported_id = first["imported"][0]["id"]
            self.assertEqual(self.panel._style_for_upload(imported_id).font_size, 72)
            self.assertEqual(fake_drive.listed_folders, [input_folder_id, logos_folder_id])

            second = self.panel.scan_drive()

        self.assertEqual(second["imported"], [])
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(fake_drive.downloads, 1)
        self.assertEqual(fake_drive.listed_folders, [input_folder_id, logos_folder_id] * 2)
        self.assertEqual(fake_drive.assert_doc_id, doc_remote_id)
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(second["items"][0]["source"], "google_drive")


if __name__ == "__main__":
    unittest.main()
