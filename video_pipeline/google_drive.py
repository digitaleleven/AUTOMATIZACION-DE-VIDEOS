"""Least-privilege Google Drive OAuth and API helpers.

No network requests are made at import or application startup. OAuth is disabled
unless the owner explicitly enables it after configuring the client credentials.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlencode, quote, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken

from .config import INPUT_FOLDER_ID, PROCESSED_FOLDER_ID, LOGOS_FOLDER_ID

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE_API = "https://www.googleapis.com/drive/v3"
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_OPENID = "openid"
SCOPE_EMAIL = "email"
DRIVE_SCOPES = (
    SCOPE_DRIVE_READONLY,
    SCOPE_DRIVE_FILE,
    SCOPE_OPENID,
    SCOPE_EMAIL,
)
DRIVE_ACCESS_SCOPES = (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
VIDEO_EXTENSIONS = {".mp4", ".mov"}
MAX_DRIVE_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DRIVE_DOC_BYTES = 128 * 1024
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


class GoogleDriveError(RuntimeError):
    """Safe-to-display Drive error; never contains credentials or response bodies."""


@dataclass(frozen=True)
class DriveOAuthConfig:
    enabled: bool
    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str
    allowed_email: str = ""

    @classmethod
    def from_environment(cls) -> "DriveOAuthConfig":
        return cls(
            enabled=os.getenv("GOOGLE_DRIVE_OAUTH_ENABLED", "").strip().lower()
            in {"1", "true", "yes"},
            client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip(),
            session_secret=os.getenv("SESSION_SECRET", ""),
            allowed_email=os.getenv("GOOGLE_OAUTH_ALLOWED_EMAIL", "").strip(),
        )

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.client_id:
            missing.append("GOOGLE_OAUTH_CLIENT_ID")
        if not self.client_secret:
            missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("GOOGLE_OAUTH_REDIRECT_URI")
        if not self.session_secret:
            missing.append("SESSION_SECRET")
        if not self.allowed_email:
            missing.append("GOOGLE_OAUTH_ALLOWED_EMAIL")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.allowed_email):
            missing.append("GOOGLE_OAUTH_ALLOWED_EMAIL (formato no válido)")
        try:
            redirect = urlsplit(self.redirect_uri)
            _ = redirect.port
            invalid_redirect = bool(self.redirect_uri) and (
                redirect.scheme != "https"
                or not redirect.hostname
                or redirect.username
                or redirect.password
                or redirect.query
                or redirect.fragment
            )
        except ValueError:
            invalid_redirect = True
        if invalid_redirect:
            missing.append("GOOGLE_OAUTH_REDIRECT_URI (debe ser una URL HTTPS exacta)")
        return missing

    @property
    def ready(self) -> bool:
        return self.enabled and not self.missing


class GoogleDriveStore:
    """Encrypted OAuth credentials, one-use OAuth states, and import idempotency."""

    def __init__(self, database_path: str | Path, session_secret: str):
        if not session_secret:
            raise ValueError("SESSION_SECRET is required to encrypt Drive credentials.")
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(
            b"matrix-motion/google-drive-token-store/v1\0"
            + session_secret.encode("utf-8")
        ).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        self._initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS google_drive_credentials (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    token_ciphertext BLOB NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS google_oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    verifier_ciphertext BLOB NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS google_drive_imports (
                    drive_file_id TEXT PRIMARY KEY,
                    local_upload_id TEXT UNIQUE NOT NULL,
                    filename TEXT NOT NULL,
                    md5_checksum TEXT,
                    doc_file_id TEXT,
                    style_overrides_json TEXT NOT NULL DEFAULT '{}',
                    logo_path TEXT,
                    output_drive_file_id TEXT,
                    status TEXT NOT NULL DEFAULT 'imported',
                    error TEXT,
                    updated_at REAL NOT NULL
                );
                """
            )

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            return self._fernet.decrypt(value).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise GoogleDriveError(
                "No se pudo descifrar la conexión guardada. Será necesario volver a autorizar Drive."
            ) from exc

    def save_oauth_state(self, state: str, verifier: str, ttl_seconds: int = 600) -> None:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as db:
            db.execute("DELETE FROM google_oauth_states WHERE expires_at < ?", (now,))
            db.execute(
                """INSERT INTO google_oauth_states
                   (state_hash, verifier_ciphertext, expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (state_hash, self.encrypt(verifier), now + ttl_seconds, now),
            )

    def consume_oauth_state(self, state: str, cookie_state: str) -> str:
        if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
            raise GoogleDriveError("La autorización de Google no coincide con esta sesión. Vuelve a intentarlo.")
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT verifier_ciphertext, expires_at FROM google_oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            db.execute("DELETE FROM google_oauth_states WHERE state_hash = ?", (state_hash,))
            if not row or float(row["expires_at"]) < now:
                raise GoogleDriveError("La autorización de Google caducó. Inicia la conexión otra vez.")
            verifier_ciphertext = bytes(row["verifier_ciphertext"])
        return self.decrypt(verifier_ciphertext)

    def save_token_bundle(self, bundle: dict[str, Any]) -> None:
        encrypted = self.encrypt(json.dumps(bundle, separators=(",", ":")))
        with self._connect() as db:
            db.execute(
                """INSERT INTO google_drive_credentials (id, token_ciphertext, updated_at)
                   VALUES (1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     token_ciphertext = excluded.token_ciphertext,
                     updated_at = excluded.updated_at""",
                (encrypted, time.time()),
            )

    def token_bundle(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT token_ciphertext FROM google_drive_credentials WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(self.decrypt(bytes(row["token_ciphertext"])))
        except json.JSONDecodeError as exc:
            raise GoogleDriveError("La conexión guardada de Google no es válida.") from exc
        return value if isinstance(value, dict) else None

    def has_token(self) -> bool:
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM google_drive_credentials WHERE id = 1"
            ).fetchone() is not None

    def mark_verified(self) -> None:
        bundle = self.token_bundle()
        if not bundle:
            return
        bundle["last_verified_at"] = time.time()
        self.save_token_bundle(bundle)

    def disconnect(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM google_drive_credentials WHERE id = 1")

    def get_import(self, drive_file_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM google_drive_imports WHERE drive_file_id = ?",
                (drive_file_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_import_for_local_upload(self, local_upload_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM google_drive_imports WHERE local_upload_id = ?",
                (local_upload_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_import(
        self,
        *,
        drive_file_id: str,
        local_upload_id: str,
        filename: str,
        md5_checksum: str | None,
        doc_file_id: str | None,
        style_overrides: dict[str, Any],
        logo_path: str | None,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO google_drive_imports
                   (drive_file_id, local_upload_id, filename, md5_checksum, doc_file_id,
                    style_overrides_json, logo_path, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    drive_file_id, local_upload_id, filename, md5_checksum, doc_file_id,
                    json.dumps(style_overrides, ensure_ascii=False), logo_path, time.time(),
                ),
            )
        return cursor.rowcount == 1

    def update_import(self, drive_file_id: str, **fields: Any) -> None:
        allowed = {"status", "error", "output_drive_file_id"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported Drive import update.")
        assignments = [f"{name} = ?" for name in fields]
        values = list(fields.values())
        assignments.append("updated_at = ?")
        values.extend((time.time(), drive_file_id))
        with self._connect() as db:
            db.execute(
                f"UPDATE google_drive_imports SET {', '.join(assignments)} WHERE drive_file_id = ?",
                values,
            )


class GoogleDriveOAuth:
    def __init__(
        self,
        config: DriveOAuthConfig,
        store: GoogleDriveStore,
        http: Any = requests,
    ):
        self.config = config
        self.store = store
        self.http = http

    def status(self) -> dict[str, Any]:
        bundle = self.store.token_bundle()
        stored_email = str(bundle.get("account_email") or "").strip().casefold() if bundle else ""
        allowed_email = self.config.allowed_email.strip().casefold()
        raw_scopes = bundle.get("scopes") if bundle else None
        if isinstance(raw_scopes, str):
            granted_scopes = set(raw_scopes.split())
        elif isinstance(raw_scopes, (list, tuple, set)):
            granted_scopes = {str(scope) for scope in raw_scopes}
        else:
            granted_scopes = set()
        missing_scopes = sorted(set(DRIVE_ACCESS_SCOPES) - granted_scopes)
        identity_verified = bool(
            stored_email
            and allowed_email
            and hmac.compare_digest(stored_email, allowed_email)
            and bundle.get("identity_verified_at")
        )
        authorized = bool(
            bundle
            and bundle.get("refresh_token")
            and identity_verified
            and not missing_scopes
        )
        needs_reauthorization = bool(
            bundle
            and bundle.get("refresh_token")
            and identity_verified
            and missing_scopes
        )
        verified_at = bundle.get("last_verified_at") if authorized and bundle else None
        return {
            "enabled": self.config.enabled,
            "configured": not self.config.missing,
            "ready": self.config.ready,
            "authorized": authorized,
            "identity_verified": identity_verified,
            "needs_reauthorization": needs_reauthorization,
            "missing_scopes": missing_scopes,
            "identity_mismatch": bool(
                bundle
                and bundle.get("refresh_token")
                and allowed_email
                and not identity_verified
            ),
            "verified": bool(verified_at),
            "last_verified_at": verified_at,
            "connected": bool(verified_at),
            "missing_configuration": self.config.missing,
            "authorization_pending": not authorized,
            "scopes": list(DRIVE_SCOPES),
        }

    def _require_ready(self) -> None:
        if not self.config.ready:
            raise GoogleDriveError(
                "La conexión de Drive sigue desactivada o falta su configuración. No se inició OAuth."
            )

    def _revoke_unaccepted_grant(self, payload: dict[str, Any]) -> None:
        token = payload.get("refresh_token") or payload.get("access_token")
        if not token:
            return
        try:
            response = self.http.post(
                REVOKE_URL,
                data={"token": str(token)},
                timeout=20,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Could not revoke an unaccepted Google OAuth grant (%s).",
                type(exc).__name__,
            )

    def verify_access_token_identity(
        self,
        access_token: str,
        *,
        revoke_on_failure: bool = False,
    ) -> str:
        """Verify browser-issued Picker tokens against the secondary-account allowlist."""
        self._require_ready()
        token = str(access_token or "")
        if not token or len(token) > 8192 or any(char.isspace() for char in token):
            raise GoogleDriveError("El token temporal de Google no es válido.")
        try:
            response = self.http.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
                allow_redirects=False,
            )
            response.raise_for_status()
            identity = response.json()
        except Exception as exc:
            if revoke_on_failure:
                self._revoke_unaccepted_grant({"access_token": token})
            logger.warning("Google access-token identity verification failed (%s)", type(exc).__name__)
            raise GoogleDriveError(
                "No se pudo verificar la cuenta de Google. No se usó el token."
            ) from exc
        if not isinstance(identity, dict):
            identity = {}
        account_email = str(identity.get("email") or "").strip().casefold()
        if (
            not account_email
            or identity.get("email_verified") is not True
            or not hmac.compare_digest(account_email, self.config.allowed_email.strip().casefold())
        ):
            if revoke_on_failure:
                self._revoke_unaccepted_grant({"access_token": token})
            raise GoogleDriveError(
                "La cuenta seleccionada no coincide con la cuenta secundaria autorizada."
            )
        return account_email

    def authorization_url(self) -> tuple[str, str]:
        self._require_ready()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.store.save_oauth_state(state, verifier)
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(DRIVE_SCOPES),
            "access_type": "offline",
            "prompt": "select_account consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}", state

    def complete_authorization(
        self, code: str, state: str, cookie_state: str
    ) -> None:
        self._require_ready()
        if not code or len(code) > 4096:
            raise GoogleDriveError("Google no devolvió un código de autorización válido.")
        verifier = self.store.consume_oauth_state(state, cookie_state)
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.config.redirect_uri,
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Google OAuth code exchange failed (%s)", type(exc).__name__)
            raise GoogleDriveError("Google no pudo completar la autorización. Revisa la configuración OAuth.") from exc

        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise GoogleDriveError("Google no devolvió un token de acceso válido.")
        granted_scopes = payload.get("scope")
        actual = set(str(granted_scopes or "").split())
        if not set(DRIVE_ACCESS_SCOPES).issubset(actual):
            self._revoke_unaccepted_grant(payload)
            raise GoogleDriveError(
                "Google no concedió los permisos de Drive configurados; no se guardó la conexión."
            )
        try:
            account_email = self.verify_access_token_identity(
                payload["access_token"],
                revoke_on_failure=True,
            )
        except GoogleDriveError as exc:
            raise GoogleDriveError(
                f"{exc} No se guardó la conexión."
            ) from exc
        old_bundle = self.store.token_bundle() or {}
        old_email = str(old_bundle.get("account_email") or "").strip().casefold()
        same_account = bool(
            old_email
            and hmac.compare_digest(old_email, account_email)
            and old_bundle.get("identity_verified_at")
        )
        refresh_token = payload.get("refresh_token") or (
            old_bundle.get("refresh_token") if same_account else None
        )
        if not refresh_token:
            self._revoke_unaccepted_grant(payload)
            raise GoogleDriveError(
                "Google no devolvió un refresh token para la cuenta secundaria. "
                "Revoca el acceso anterior de esa cuenta y vuelve a conectar."
            )
        bundle = {
            "access_token": payload["access_token"],
            "refresh_token": refresh_token,
            "expires_at": time.time() + max(0, int(payload.get("expires_in", 3600))),
            "scopes": granted_scopes.split() if granted_scopes else None,
            "account_email": account_email,
            "identity_verified_at": time.time(),
        }
        self.store.save_token_bundle(bundle)

    def access_token(self, force_refresh: bool = False) -> str:
        self._require_ready()
        bundle = self.store.token_bundle()
        if not bundle or not bundle.get("refresh_token"):
            raise GoogleDriveError("Google Drive no está conectado.")
        stored_email = str(bundle.get("account_email") or "").strip().casefold()
        allowed_email = self.config.allowed_email.strip().casefold()
        if (
            not stored_email
            or not allowed_email
            or not bundle.get("identity_verified_at")
            or not hmac.compare_digest(stored_email, allowed_email)
        ):
            raise GoogleDriveError(
                "La conexión guardada no pertenece a la cuenta secundaria autorizada. Vuelve a conectar la cuenta correcta."
            )
        if (
            not force_refresh
            and bundle.get("access_token")
            and float(bundle.get("expires_at", 0)) > time.time() + 60
        ):
            return str(bundle["access_token"])
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": bundle["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Google OAuth refresh failed (%s)", type(exc).__name__)
            raise GoogleDriveError("No se pudo renovar la conexión de Google Drive.") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise GoogleDriveError("Google no devolvió un token renovado válido.")
        bundle.update(
            access_token=payload["access_token"],
            expires_at=time.time() + max(0, int(payload.get("expires_in", 3600))),
        )
        if payload.get("refresh_token"):
            bundle["refresh_token"] = payload["refresh_token"]
        self.store.save_token_bundle(bundle)
        return str(bundle["access_token"])


class GoogleDriveClient:
    def __init__(
        self,
        oauth: GoogleDriveOAuth,
        http: Any = requests,
        timeout: int = 60,
    ):
        self.oauth = oauth
        self.http = http
        self.timeout = timeout

    def _request(self, method: str, url: str, **kwargs: Any):
        extra_headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            token = self.oauth.access_token(force_refresh=attempt == 1)
            headers = dict(extra_headers)
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = self.http.request(
                    method, url, headers=headers, timeout=self.timeout, **kwargs
                )
            except Exception as exc:
                logger.warning("Google Drive request failed (%s)", type(exc).__name__)
                raise GoogleDriveError("No se pudo contactar con Google Drive.") from exc
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code < 200 or response.status_code >= 300:
                logger.warning("Google Drive API returned HTTP %s", response.status_code)
                if response.status_code == 401:
                    raise GoogleDriveError("Google Drive rechazó la conexión. Vuelve a autorizar.")
                if response.status_code == 403:
                    raise GoogleDriveError(
                        "Google Drive denegó el acceso a la carpeta o al archivo. Revisa los permisos autorizados."
                    )
                raise GoogleDriveError(f"Google Drive devolvió un error HTTP {response.status_code}.")
            self.oauth.store.mark_verified()
            return response
        raise GoogleDriveError("Google Drive rechazó la conexión.")

    def _request_with_token(
        self,
        method: str,
        url: str,
        access_token: str,
        **kwargs: Any,
    ):
        if not isinstance(access_token, str) or not access_token:
            raise GoogleDriveError("Falta autorización temporal de Google Drive.")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {access_token}"
        try:
            response = self.http.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
                **kwargs,
            )
        except Exception as exc:
            logger.warning("Google Drive request with temporary authorization failed (%s)", type(exc).__name__)
            raise GoogleDriveError("No se pudo contactar con Google Drive.") from exc
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning("Google Drive API returned HTTP %s", response.status_code)
            if response.status_code == 401:
                raise GoogleDriveError("La autorización temporal de Google caducó; vuelve a autorizar.")
            if response.status_code == 403:
                raise GoogleDriveError(
                    "Google Drive denegó el acceso a la carpeta o al archivo seleccionado."
                )
            raise GoogleDriveError(f"Google Drive devolvió un error HTTP {response.status_code}.")
        return response

    def list_children(self, folder_id: str) -> list[dict[str, Any]]:
        if not _DRIVE_ID.fullmatch(folder_id) or folder_id not in configured_drive_folders().values():
            raise GoogleDriveError("Solo se pueden consultar las carpetas configuradas de MATRIX MOTION.")
        query = f"'{folder_id}' in parents and trashed = false"
        files: list[dict[str, Any]] = []
        page_token = None
        for _ in range(100):
            params: dict[str, Any] = {
                "q": query,
                "pageSize": 1000,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,size,parents)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._request("GET", f"{DRIVE_API}/files", params=params)
            try:
                payload = response.json()
            except Exception as exc:
                raise GoogleDriveError("Google Drive devolvió una lista no válida.") from exc
            batch = payload.get("files", []) if isinstance(payload, dict) else []
            if not isinstance(batch, list):
                raise GoogleDriveError("Google Drive devolvió una lista no válida.")
            files.extend(
                item
                for item in batch
                if isinstance(item, dict)
                and isinstance(item.get("parents"), list)
                and folder_id in item["parents"]
            )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return files
        raise GoogleDriveError("La carpeta contiene demasiadas páginas para un único escaneo.")

    def get_file(self, file_id: str) -> dict[str, Any]:
        if not _DRIVE_ID.fullmatch(file_id):
            raise GoogleDriveError("El identificador de archivo de Drive no es válido.")
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{quote(file_id, safe='')}",
            params={
                "fields": "id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed",
                "supportsAllDrives": "true",
            },
        )
        try:
            item = response.json()
        except Exception as exc:
            raise GoogleDriveError("Google Drive devolvió metadatos no válidos.") from exc
        if (
            not isinstance(item, dict)
            or item.get("id") != file_id
            or item.get("trashed")
        ):
            raise GoogleDriveError("El archivo elegido no está disponible en Google Drive.")
        return item

    def download_file(self, file_id: str, destination: str | Path) -> Path:
        if not _DRIVE_ID.fullmatch(file_id):
            raise GoogleDriveError("El identificador de archivo de Drive no es válido.")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise GoogleDriveError("El destino local ya existe; no se sobrescribió.")
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.part")
        written = 0
        try:
            response = self._request(
                "GET",
                f"{DRIVE_API}/files/{quote(file_id, safe='')}?alt=media",
                stream=True,
            )
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_DRIVE_VIDEO_BYTES:
                        raise GoogleDriveError("El vídeo de Drive supera el límite de tamaño admitido.")
                except ValueError as exc:
                    raise GoogleDriveError("Google Drive devolvió un tamaño de archivo no válido.") from exc
            with temporary.open("xb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DRIVE_VIDEO_BYTES:
                        raise GoogleDriveError("El vídeo de Drive supera el límite de tamaño admitido.")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written == 0:
                raise GoogleDriveError("Google Drive devolvió un archivo vacío.")
            os.link(temporary, destination)
            return destination
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove a temporary Drive download file.")

    def export_document_text(self, file_id: str) -> str:
        if not _DRIVE_ID.fullmatch(file_id):
            raise GoogleDriveError("El identificador del documento de Drive no es válido.")
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{quote(file_id, safe='')}/export",
            params={"mimeType": "text/plain"},
            stream=True,
        )
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_DRIVE_DOC_BYTES:
                    raise GoogleDriveError("El documento de instrucciones supera el límite de tamaño admitido.")
            except ValueError as exc:
                raise GoogleDriveError("Google Drive devolvió un tamaño de documento no válido.") from exc
        content = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > MAX_DRIVE_DOC_BYTES:
                raise GoogleDriveError("El documento de instrucciones supera el límite de tamaño admitido.")
        if not content:
            raise GoogleDriveError("El documento de instrucciones supera el límite de tamaño admitido.")
        try:
            return bytes(content).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise GoogleDriveError("El documento de instrucciones no contiene texto UTF-8 válido.") from exc

    def upload_new_file(
        self,
        path: str | Path,
        folder_id: str = PROCESSED_FOLDER_ID,
        *,
        app_properties: dict[str, str] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        if folder_id != configured_drive_folders()["processed"]:
            raise GoogleDriveError("Solo se pueden crear resultados en 02_PROCESADOS.")
        source_key = (app_properties or {}).get("matrixMotionSource")
        if source_key:
            existing = self.find_output_by_source(
                source_key, folder_id, access_token=access_token
            )
            if existing:
                return existing
        # Import lazily to avoid an import cycle and preserve the existing resumable uploader.
        from .drive import upload_new_file

        result = upload_new_file(
            path,
            folder_id=folder_id,
            access_token=access_token or self.oauth.access_token(),
            app_properties=app_properties,
            session=self.http,
        )
        if not access_token:
            self.oauth.store.mark_verified()
        return result

    def delete_created_file(self, file_id: str, *, access_token: str | None = None) -> None:
        """Delete a file created by this process, after validating its Drive ID."""
        if not _DRIVE_ID.fullmatch(file_id):
            raise GoogleDriveError("El identificador del archivo temporal no es válido.")
        url = f"{DRIVE_API}/files/{quote(file_id, safe='')}"
        params = {"supportsAllDrives": "true"}
        if access_token:
            self._request_with_token("DELETE", url, access_token, params=params)
        else:
            self._request("DELETE", url, params=params)

    def find_output_by_source(
        self,
        source_key: str,
        folder_id: str = PROCESSED_FOLDER_ID,
        *,
        access_token: str | None = None,
    ) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-f0-9]{64}", source_key):
            raise GoogleDriveError("La clave de idempotencia del vídeo no es válida.")
        if folder_id != configured_drive_folders()["processed"]:
            raise GoogleDriveError("Solo se pueden buscar resultados en 02_PROCESADOS.")
        query = (
            "appProperties has { key='matrixMotionSource' and "
            f"value='{source_key}' }} and '{folder_id}' in parents and trashed = false"
        )
        params = {
            "q": query,
            "pageSize": 2,
            "fields": "files(id,name,mimeType,parents,appProperties)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if access_token:
            response = self._request_with_token(
                "GET", f"{DRIVE_API}/files", access_token, params=params
            )
        else:
            response = self._request("GET", f"{DRIVE_API}/files", params=params)
        try:
            items = response.json().get("files", [])
        except Exception as exc:
            raise GoogleDriveError("Google Drive devolvió una respuesta de búsqueda no válida.") from exc
        return items[0] if isinstance(items, list) and items else None


def configured_drive_folders() -> dict[str, str]:
    return {
        "input": INPUT_FOLDER_ID,
        "processed": PROCESSED_FOLDER_ID,
        "logos": LOGOS_FOLDER_ID,
    }


DRIVE_STYLE_KEYS = {
    "font_family",
    "font_size",
    "font_color",
    "highlight_color",
    "stroke_color",
    "stroke_width",
    "animation_style",
    "caption_position",
    "caption_offset_y",
    "emoji_by_keyword",
    "logo_position",
    "logo_scale",
    "logo_offset_x",
    "logo_offset_y",
}


def parse_style_instructions(text: str) -> dict[str, Any]:
    """Read only explicit allow-listed style settings from a matching Google Doc.

    Accepts a JSON object or one `key: value` / `key=value` setting per line.
    Free-form prose is never passed to an LLM or interpreted as executable prompts.
    """
    if not isinstance(text, str) or len(text) > MAX_DRIVE_DOC_BYTES:
        return {}
    source = text.strip()
    if not source:
        return {}
    if source.startswith("```") and source.endswith("```"):
        source = source.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return {key: value for key, value in parsed.items() if key in DRIVE_STYLE_KEYS}
    overrides: dict[str, Any] = {}
    for raw_line in source.splitlines():
        line = raw_line.strip().lstrip("-* ").strip()
        if not line or line.startswith("#"):
            continue
        separator = ":" if ":" in line else "=" if "=" in line else None
        if not separator:
            continue
        key, raw_value = (part.strip() for part in line.split(separator, 1))
        if key not in DRIVE_STYLE_KEYS or not raw_value:
            continue
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            try:
                value = shlex.split(raw_value)[0]
            except (ValueError, IndexError):
                continue
        overrides[key] = value
    return overrides
