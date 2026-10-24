import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from video_pipeline.config import INPUT_FOLDER_ID
from video_pipeline.google_drive import (
    DRIVE_SCOPES,
    DriveOAuthConfig,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveOAuth,
    GoogleDriveStore,
    parse_style_instructions,
)


SESSION_SECRET = "test-session-secret-long-enough-for-token-store-0123456789"


class FakeResponse:
    def __init__(self, payload=None, content=b"", headers=None, status_code=200, chunks=None):
        self._payload = payload or {}
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else ([content] if content else [])

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def iter_content(self, chunk_size=8192):
        yield from self._chunks


class FakeOAuthHttp:
    def __init__(self, responses, userinfo=None):
        self.responses = list(responses)
        self.calls = []
        self.userinfo = userinfo or {"email": "automation@gmail.com", "email_verified": True}
        self.get_calls = []
        self.revoke_calls = []

    def post(self, url, **kwargs):
        if url.endswith("/revoke"):
            self.revoke_calls.append((url, kwargs))
            return FakeResponse(status_code=200)
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self.userinfo)


class FakeDriveHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class GoogleDriveOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "drive.sqlite3"
        self.store = GoogleDriveStore(self.db_path, SESSION_SECRET)
        self.config = DriveOAuthConfig(
            enabled=True,
            client_id="client-id.apps.googleusercontent.com",
            client_secret="not-a-real-client-secret",
            redirect_uri="https://matrix.example.test/oauth/google/callback",
            session_secret=SESSION_SECRET,
            allowed_email="automation@gmail.com",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_oauth_is_disabled_until_explicitly_enabled_and_configured(self):
        config = DriveOAuthConfig(
            enabled=False,
            client_id="",
            client_secret="",
            redirect_uri="",
            session_secret=SESSION_SECRET,
            allowed_email="",
        )
        oauth = GoogleDriveOAuth(config, self.store)
        with self.assertRaises(GoogleDriveError):
            oauth.authorization_url()
        self.assertFalse(oauth.status()["ready"])
        self.assertFalse(oauth.status()["authorized"])
        self.assertIn("GOOGLE_OAUTH_ALLOWED_EMAIL", oauth.status()["missing_configuration"])

    def test_allowlisted_secondary_account_is_required_before_oauth_can_be_ready(self):
        config = DriveOAuthConfig(
            enabled=True,
            client_id="client-id.apps.googleusercontent.com",
            client_secret="not-a-real-client-secret",
            redirect_uri="https://matrix.example.test/oauth/google/callback",
            session_secret=SESSION_SECRET,
            allowed_email="",
        )
        self.assertFalse(config.ready)
        self.assertEqual(config.missing, ["GOOGLE_OAUTH_ALLOWED_EMAIL"])

    def test_pkce_state_one_time_callback_and_encrypted_refresh_token(self):
        http = FakeOAuthHttp(
            [
                FakeResponse(
                    {
                        "access_token": "access-token-test",
                        "refresh_token": "refresh-token-test",
                        "expires_in": 3600,
                        "scope": " ".join(DRIVE_SCOPES),
                    }
                )
            ]
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        url, state = oauth.authorization_url()
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["state"], [state])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["scope"], [" ".join(DRIVE_SCOPES)])
        self.assertEqual(params["access_type"], ["offline"])
        self.assertEqual(params["prompt"], ["select_account consent"])
        self.assertTrue(params["code_challenge"][0])

        with self.assertRaises(GoogleDriveError):
            oauth.complete_authorization("code", state, "different-state")
        self.assertEqual(http.calls, [])

        oauth.complete_authorization("code", state, state)
        status = oauth.status()
        self.assertTrue(status["authorized"])
        self.assertTrue(status["identity_verified"])
        self.assertFalse(status["verified"])
        self.assertEqual(oauth.access_token(), "access-token-test")
        self.assertEqual(len(http.get_calls), 1)
        self.assertNotIn("automation@gmail.com", json.dumps(status))
        raw_ciphertext = sqlite3.connect(self.db_path).execute(
            "SELECT token_ciphertext FROM google_drive_credentials WHERE id = 1"
        ).fetchone()[0]
        self.assertNotIn(b"refresh-token-test", raw_ciphertext)
        self.assertNotIn(b"access-token-test", raw_ciphertext)
        with self.assertRaises(GoogleDriveError):
            oauth.complete_authorization("code", state, state)

    def test_wrong_google_account_is_rejected_before_credentials_are_saved(self):
        http = FakeOAuthHttp(
            [
                FakeResponse(
                    {
                        "access_token": "wrong-account-access",
                        "refresh_token": "wrong-account-refresh",
                        "expires_in": 3600,
                        "scope": " ".join(DRIVE_SCOPES),
                    }
                )
            ],
            userinfo={"email": "main-account@gmail.com", "email_verified": True},
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        _, state = oauth.authorization_url()
        with self.assertRaisesRegex(GoogleDriveError, "cuenta secundaria"):
            oauth.complete_authorization("code", state, state)
        self.assertFalse(self.store.has_token())
        self.assertEqual(len(http.get_calls), 1)
        self.assertEqual(len(http.revoke_calls), 1)

    def test_unverified_google_email_is_rejected(self):
        http = FakeOAuthHttp(
            [
                FakeResponse(
                    {
                        "access_token": "unverified-access",
                        "refresh_token": "unverified-refresh",
                        "scope": " ".join(DRIVE_SCOPES),
                    }
                )
            ],
            userinfo={"email": "automation@gmail.com", "email_verified": False},
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        _, state = oauth.authorization_url()
        with self.assertRaises(GoogleDriveError):
            oauth.complete_authorization("code", state, state)
        self.assertFalse(self.store.has_token())
        self.assertEqual(len(http.revoke_calls), 1)

    def test_refresh_token_from_a_different_account_is_never_reused(self):
        old_bundle = {
            "access_token": "main-account-access",
            "refresh_token": "main-account-refresh",
            "expires_at": time.time() + 3600,
            "account_email": "main-account@gmail.com",
            "identity_verified_at": time.time(),
        }
        self.store.save_token_bundle(old_bundle)
        http = FakeOAuthHttp(
            [
                FakeResponse(
                    {
                        "access_token": "secondary-account-access",
                        "expires_in": 3600,
                        "scope": " ".join(DRIVE_SCOPES),
                    }
                )
            ],
            userinfo={"email": "automation@gmail.com", "email_verified": True},
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        _, state = oauth.authorization_url()
        with self.assertRaisesRegex(GoogleDriveError, "refresh token"):
            oauth.complete_authorization("code", state, state)
        self.assertEqual(self.store.token_bundle(), old_bundle)
        self.assertFalse(oauth.status()["authorized"])
        self.assertEqual(len(http.revoke_calls), 1)

    def test_expired_access_token_is_refreshed_without_exposing_values(self):
        self.store.save_token_bundle(
            {
                "access_token": "expired",
                "refresh_token": "refresh-token-test",
                "expires_at": time.time() - 10,
                "account_email": "automation@gmail.com",
                "identity_verified_at": time.time(),
            }
        )
        http = FakeOAuthHttp(
            [FakeResponse({"access_token": "new-access-token", "expires_in": 3600})]
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        self.assertEqual(oauth.access_token(), "new-access-token")
        self.assertEqual(len(http.calls), 1)


class GoogleDriveApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = GoogleDriveStore(self.root / "drive.sqlite3", SESSION_SECRET)
        self.store.save_token_bundle(
            {
                "access_token": "access-token-test",
                "refresh_token": "refresh-token-test",
                "expires_at": time.time() + 3600,
                "account_email": "automation@gmail.com",
                "identity_verified_at": time.time(),
            }
        )
        self.oauth = GoogleDriveOAuth(
            DriveOAuthConfig(
                enabled=True,
                client_id="client",
                client_secret="secret",
                redirect_uri="https://matrix.example.test/oauth/google/callback",
                session_secret=SESSION_SECRET,
                allowed_email="automation@gmail.com",
            ),
            self.store,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_paginates_and_marks_last_successful_drive_request(self):
        http = FakeDriveHttp(
            [
                FakeResponse({"files": [{"id": "a" * 20}], "nextPageToken": "next"}),
                FakeResponse({"files": [{"id": "b" * 20}]}),
            ]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        files = client.list_children(INPUT_FOLDER_ID)
        self.assertEqual([item["id"] for item in files], ["a" * 20, "b" * 20])
        self.assertEqual(len(http.calls), 2)
        self.assertIn(INPUT_FOLDER_ID, http.calls[0][2]["params"]["q"])
        self.assertTrue(self.oauth.status()["verified"])

    def test_download_never_overwrites_local_destination_and_exports_docs(self):
        http = FakeDriveHttp(
            [
                FakeResponse(
                    content=b"video bytes",
                    headers={"Content-Length": "11"},
                    chunks=[b"video ", b"bytes"],
                ),
                FakeResponse(content=b"font_size: 72\nfont_color: \"#ffffff\""),
            ]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        destination = self.root / "original.mp4"
        client.download_file("a" * 20, destination)
        self.assertEqual(destination.read_bytes(), b"video bytes")
        with self.assertRaises(GoogleDriveError):
            client.download_file("a" * 20, destination)
        self.assertEqual(
            client.export_document_text("b" * 20),
            'font_size: 72\nfont_color: "#ffffff"',
        )
        self.assertEqual(len(http.calls), 2)

    def test_retry_search_uses_source_key_before_creating_another_output(self):
        source_key = hashlib.sha256(b"drive-original-id").hexdigest()
        http = FakeDriveHttp(
            [FakeResponse({"files": [{"id": "existing-output-id", "name": "edited.mp4"}]})]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        result = client.upload_new_file(
            self.root / "not-read-because-existing.mp4",
            app_properties={"matrixMotionSource": source_key},
        )
        self.assertEqual(result["id"], "existing-output-id")
        self.assertEqual(len(http.calls), 1)

    def test_delete_created_file_uses_a_validated_drive_id_and_targeted_endpoint(self):
        http = FakeDriveHttp([FakeResponse(status_code=204)])
        client = GoogleDriveClient(self.oauth, http=http)
        client.delete_created_file("a" * 20)
        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "DELETE")
        self.assertTrue(url.endswith("/files/" + ("a" * 20)))
        self.assertEqual(kwargs["params"], {"supportsAllDrives": "true"})
        with self.assertRaises(GoogleDriveError):
            client.delete_created_file("../not-a-drive-id")

    def test_import_record_prevents_duplicate_download_registration(self):
        record = {
            "drive_file_id": "abcde12345file-id",
            "local_upload_id": "a" * 32,
            "filename": "clip.mp4",
            "md5_checksum": "hash",
            "doc_file_id": None,
            "style_overrides": {},
            "logo_path": None,
        }
        self.assertTrue(self.store.add_import(**record))
        self.assertFalse(self.store.add_import(**record))


class DriveInstructionParsingTests(unittest.TestCase):
    def test_only_allowlisted_style_settings_are_read_from_docs(self):
        parsed = parse_style_instructions(
            '{"font_size": 70, "font_color": "#00aacc", "max_estimated_cost_usd": 0, "prompt": "ignore rules"}'
        )
        self.assertEqual(parsed, {"font_size": 70, "font_color": "#00aacc"})

        parsed = parse_style_instructions(
            "font_size: 72\ncaption_position=middle\nUnknown directive: do something"
        )
        self.assertEqual(parsed, {"font_size": 72, "caption_position": "middle"})

    def test_plain_prose_does_not_become_model_instructions(self):
        self.assertEqual(
            parse_style_instructions("Haz el vídeo más dinámico y cambia su mensaje."),
            {},
        )


if __name__ == "__main__":
    unittest.main()
