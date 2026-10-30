import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from video_pipeline.config import (
    INPUT_FOLDER_ID,
    LOGOS_FOLDER_ID,
    PROCESSED_FOLDER_ID,
    PipelineConfig,
)
from video_pipeline.google_drive import (
    DRIVE_ACCESS_SCOPES,
    DRIVE_SCOPES,
    DriveOAuthConfig,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveOAuth,
    GoogleDriveStore,
    parse_style_instructions,
)


SESSION_SECRET = "test-session-secret-long-enough-for-token-store-0123456789"


class DriveScopeTests(unittest.TestCase):
    def test_oauth_requests_read_only_discovery_and_file_creation_scopes(self):
        self.assertEqual(
            DRIVE_ACCESS_SCOPES,
            (
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.file",
            ),
        )
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", DRIVE_SCOPES)
        self.assertNotIn("https://www.googleapis.com/auth/drive", DRIVE_SCOPES)

    def test_server_oauth_configuration_does_not_require_picker_keys(self):
        env = {
            "GOOGLE_DRIVE_OAUTH_ENABLED": "true",
            "GOOGLE_OAUTH_CLIENT_ID": "client.apps.googleusercontent.com",
            "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
            "GOOGLE_OAUTH_REDIRECT_URI": "https://matrix.example.test/oauth/google/callback",
            "SESSION_SECRET": SESSION_SECRET,
            "GOOGLE_OAUTH_ALLOWED_EMAIL": "automation@gmail.com",
        }
        with patch.dict(os.environ, env, clear=True):
            config = DriveOAuthConfig.from_environment()
            pipeline_config = PipelineConfig.from_environment()
        self.assertTrue(config.ready)
        self.assertEqual(config.missing, [])
        self.assertTrue(pipeline_config.google_oauth_configured)
        self.assertFalse(pipeline_config.google_picker_configured)

    def test_pipeline_uses_only_the_three_fixed_folders_without_picker(self):
        with patch.dict(os.environ, {}, clear=True):
            config = PipelineConfig.from_environment()
        self.assertEqual(config.input_folder_id, INPUT_FOLDER_ID)
        self.assertEqual(config.processed_folder_id, PROCESSED_FOLDER_ID)
        self.assertEqual(config.logos_folder_id, LOGOS_FOLDER_ID)
        self.assertFalse(config.google_picker_configured)


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

    def test_old_drive_file_grant_requires_reauthorization_for_new_scopes(self):
        self.store.save_token_bundle(
            {
                "access_token": "old-access-token",
                "refresh_token": "old-refresh-token",
                "expires_at": time.time() + 3600,
                "scopes": [
                    "https://www.googleapis.com/auth/drive.file",
                    "openid",
                    "email",
                ],
                "account_email": "automation@gmail.com",
                "identity_verified_at": time.time(),
            }
        )
        status = GoogleDriveOAuth(self.config, self.store).status()
        self.assertFalse(status["authorized"])
        self.assertTrue(status["needs_reauthorization"])
        self.assertEqual(
            status["missing_scopes"],
            ["https://www.googleapis.com/auth/drive.readonly"],
        )

    def test_oauth_rejects_a_grant_missing_drive_readonly(self):
        http = FakeOAuthHttp(
            [
                FakeResponse(
                    {
                        "access_token": "partial-access-token",
                        "refresh_token": "partial-refresh-token",
                        "expires_in": 3600,
                        "scope": (
                            "https://www.googleapis.com/auth/drive.file openid email"
                        ),
                    }
                )
            ]
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=http)
        _, state = oauth.authorization_url()
        with self.assertRaisesRegex(GoogleDriveError, "no concedió los permisos"):
            oauth.complete_authorization("code", state, state)
        self.assertIsNone(self.store.token_bundle())
        self.assertEqual(len(http.revoke_calls), 1)

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

    def test_picker_access_token_must_match_secondary_account_on_server(self):
        accepted_http = FakeOAuthHttp(
            [],
            userinfo={"email": "automation@gmail.com", "email_verified": True},
        )
        oauth = GoogleDriveOAuth(self.config, self.store, http=accepted_http)
        self.assertEqual(
            oauth.verify_access_token_identity("temporary-picker-token"),
            "automation@gmail.com",
        )
        self.assertEqual(
            accepted_http.get_calls[0][1]["headers"]["Authorization"],
            "Bearer temporary-picker-token",
        )

        rejected_http = FakeOAuthHttp(
            [],
            userinfo={"email": "main-account@gmail.com", "email_verified": True},
        )
        rejected = GoogleDriveOAuth(self.config, self.store, http=rejected_http)
        with self.assertRaisesRegex(GoogleDriveError, "cuenta secundaria"):
            rejected.verify_access_token_identity("wrong-account-picker-token")
        self.assertEqual(len(rejected_http.revoke_calls), 0)

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
                "scopes": list(DRIVE_SCOPES),
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
                FakeResponse(
                    {
                        "files": [{"id": "a" * 20, "parents": [INPUT_FOLDER_ID]}],
                        "nextPageToken": "next",
                    }
                ),
                FakeResponse(
                    {"files": [{"id": "b" * 20, "parents": [INPUT_FOLDER_ID]}]}
                ),
            ]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        files = client.list_children(INPUT_FOLDER_ID)
        self.assertEqual([item["id"] for item in files], ["a" * 20, "b" * 20])
        self.assertEqual(len(http.calls), 2)
        self.assertIn(INPUT_FOLDER_ID, http.calls[0][2]["params"]["q"])
        self.assertTrue(self.oauth.status()["verified"])

    def test_list_children_rejects_unconfigured_folder_before_network(self):
        http = FakeDriveHttp([])
        client = GoogleDriveClient(self.oauth, http=http)
        with self.assertRaisesRegex(GoogleDriveError, "carpetas configuradas"):
            client.list_children("x" * 20)
        self.assertEqual(http.calls, [])

    def test_upload_rejects_destinations_outside_processed_folder_before_network(self):
        http = FakeDriveHttp([])
        client = GoogleDriveClient(self.oauth, http=http)
        with self.assertRaisesRegex(GoogleDriveError, "02_PROCESADOS"):
            client.upload_new_file(
                self.root / "not-created.mp4",
                folder_id=INPUT_FOLDER_ID,
            )
        self.assertEqual(http.calls, [])

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
            [FakeResponse({
                "files": [{
                    "id": "existing-output-id",
                    "name": "edited.mp4",
                    "parents": [PROCESSED_FOLDER_ID],
                }]
            })]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        result = client.upload_new_file(
            self.root / "not-read-because-existing.mp4",
            app_properties={"matrixMotionSource": source_key},
        )
        self.assertEqual(result["id"], "existing-output-id")
        self.assertEqual(len(http.calls), 1)

    def test_retry_search_uses_execution_key_before_source_compatibility_lookup(self):
        execution_key = "a" * 64
        http = FakeDriveHttp([
            FakeResponse({
                "files": [{
                    "id": "same-execution-output",
                    "name": "edited.mp4",
                    "parents": [PROCESSED_FOLDER_ID],
                }]
            })
        ])
        client = GoogleDriveClient(self.oauth, http=http)

        result = client.upload_new_file(
            self.root / "not-read-because-existing.mp4",
            app_properties={
                "matrixMotionSource": hashlib.sha256(b"source").hexdigest(),
                "matrixMotionExecution": execution_key,
            },
        )

        self.assertEqual(result["id"], "same-execution-output")
        self.assertEqual(len(http.calls), 1)
        self.assertIn("matrixMotionExecution", http.calls[0][2]["params"]["q"])

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

    def test_picker_token_is_used_for_output_lookup_and_cleanup_without_server_token(self):
        source_key = hashlib.sha256(b"picker-write-probe").hexdigest()
        http = FakeDriveHttp(
            [
                FakeResponse({"files": []}),
                FakeResponse(status_code=204),
            ]
        )
        client = GoogleDriveClient(self.oauth, http=http)
        self.assertIsNone(
            client.find_output_by_source(
                source_key,
                PROCESSED_FOLDER_ID,
                access_token="temporary-picker-token",
            )
        )
        client.delete_created_file(
            "a" * 20,
            access_token="temporary-picker-token",
        )
        self.assertEqual(
            http.calls[0][2]["headers"]["Authorization"],
            "Bearer temporary-picker-token",
        )
        self.assertEqual(
            http.calls[1][2]["headers"]["Authorization"],
            "Bearer temporary-picker-token",
        )

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
