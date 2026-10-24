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
from pathlib import Path
from urllib.parse import quote, urlencode
from unittest.mock import patch

from video_pipeline.auth import OwnerAuth
from video_pipeline.webapp import VideoPanel, VideoPanelHandler, VideoPanelServer


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
class VideoPanelApiTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "PANEL_OWNER_PASSWORD": "unit-test-owner-passphrase",
                "SESSION_SECRET": "unit-test-session-secret-for-panel-auth-0123456789",
                "PANEL_COOKIE_SECURE": "true",
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
        self.assertIn(b"Tu biblioteca", body)
        self.assertIn(b"summary-pending", body)
        self.assertIn(b'name="emoji_by_keyword"', body)
        self.assertIn(b'name="logo_position"', body)

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

    def test_anonymous_requests_cannot_read_or_mutate_panel(self):
        for path in ("/", "/app.js", "/premium.css"):
            status, headers, _ = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 303, path)
            self.assertEqual(dict(headers).get("Location"), "/login")

        for path in ("/api/health", "/api/library", "/api/style", "/media/incoming/"):
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
        status, response = self.upload(path)
        self.assertEqual(status, 201)
        item = response["item"]
        self.assertEqual(item["format"], "MOV")
        self.assertEqual(item["video_codec"], "h264")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["width"], 360)
        self.assertEqual(item["height"], 640)
        self.assertIsInstance(item["created_at"], (int, float))
        self.assertTrue(item["thumbnail_url"])

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
        style["emoji_by_keyword"] = {"viaje": "✈️"}
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
        self.assertEqual(saved["logo_position"], "bottomLeft")
        self.assertEqual(saved["logo_scale"], 0.18)

        status, _, body = self.request("GET", "/api/style")
        self.assertEqual(status, 200)
        saved = json.loads(body.decode("utf-8"))
        self.assertEqual(saved["font_size"], 72)
        self.assertEqual(saved["emoji_by_keyword"], {"viaje": "✈️"})


if __name__ == "__main__":
    unittest.main()
