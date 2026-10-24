"""Single-owner authentication for the MATRIX MOTION web panel."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Iterator


SESSION_COOKIE = "matrix_motion_session"
LOGIN_CSRF_COOKIE = "matrix_motion_login_csrf"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_IDLE_SECONDS = 30 * 60
LOGIN_CHALLENGE_TTL_SECONDS = 5 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
MIN_PASSWORD_LENGTH = 16
MIN_SESSION_SECRET_LENGTH = 32


class AuthError(RuntimeError):
    status_code = 401


class AuthNotConfigured(AuthError):
    status_code = 503


class LoginChallengeError(AuthError):
    status_code = 403


class LoginRateLimited(AuthError):
    status_code = 429


class CsrfError(AuthError):
    status_code = 403


@dataclass(frozen=True)
class OwnerSession:
    csrf_token: str


class OwnerAuth:
    """Opaque server-side sessions; never persist the browser's session token."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.owner_password = os.getenv("PANEL_OWNER_PASSWORD", "")
        self.session_secret = os.getenv("SESSION_SECRET", "")
        self.cookie_secure = os.getenv("PANEL_COOKIE_SECURE", "true").strip().lower() not in {
            "0", "false", "no", "off",
        }
        self.configured = (
            len(self.owner_password) >= MIN_PASSWORD_LENGTH
            and len(self.session_secret.encode("utf-8")) >= MIN_SESSION_SECRET_LENGTH
        )
        self._key = self.session_secret.encode("utf-8") if self.session_secret else secrets.token_bytes(32)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS panel_sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS panel_login_challenges (
                    token_hash TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS panel_login_attempts (
                    client_hash TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL,
                    window_started_at REAL NOT NULL,
                    blocked_until REAL NOT NULL DEFAULT 0
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS panel_auth_config (
                    config_key TEXT PRIMARY KEY,
                    value_hash TEXT NOT NULL
                )"""
            )
            now = time.time()
            db.execute(
                """DELETE FROM panel_sessions
                   WHERE expires_at <= ? OR last_seen_at <= ?""",
                (now, now - SESSION_IDLE_SECONDS),
            )
            if not self.configured:
                db.execute("DELETE FROM panel_sessions")
            else:
                fingerprint = self._hash("owner-password", self.owner_password)
                previous = db.execute(
                    "SELECT value_hash FROM panel_auth_config WHERE config_key = 'credential_fingerprint'"
                ).fetchone()
                if previous is None or not hmac.compare_digest(
                    str(previous["value_hash"]),
                    fingerprint,
                ):
                    db.execute("DELETE FROM panel_sessions")
                db.execute(
                    """INSERT INTO panel_auth_config(config_key, value_hash)
                       VALUES ('credential_fingerprint', ?)
                       ON CONFLICT(config_key) DO UPDATE SET value_hash = excluded.value_hash""",
                    (fingerprint,),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _hash(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._key,
            f"{purpose}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue_login_challenge(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as db:
            db.execute("DELETE FROM panel_login_challenges WHERE expires_at < ?", (now,))
            db.execute(
                "INSERT INTO panel_login_challenges(token_hash, expires_at) VALUES (?, ?)",
                (self._hash("login-challenge", token), now + LOGIN_CHALLENGE_TTL_SECONDS),
            )
        return token

    def _consume_login_challenge(self, cookie_token: str, form_token: str) -> bool:
        if (
            not cookie_token or not form_token
            or not hmac.compare_digest(cookie_token, form_token)
        ):
            return False
        token_hash = self._hash("login-challenge", cookie_token)
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires_at FROM panel_login_challenges WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            db.execute("DELETE FROM panel_login_challenges WHERE token_hash = ?", (token_hash,))
        return bool(row and float(row["expires_at"]) >= now)

    def _client_hash(self, client_id: str) -> str:
        return self._hash("login-client", client_id[:200] or "unknown")

    def _check_rate_limit(self, client_hash: str, now: float) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT blocked_until FROM panel_login_attempts WHERE client_hash = ?",
                (client_hash,),
            ).fetchone()
        return bool(row and float(row["blocked_until"]) > now)

    def _record_failed_login(self, client_hash: str, now: float) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT attempts, window_started_at FROM panel_login_attempts
                   WHERE client_hash = ?""",
                (client_hash,),
            ).fetchone()
            if row is None or now - float(row["window_started_at"]) >= LOGIN_WINDOW_SECONDS:
                attempts = 1
                window_started = now
            else:
                attempts = int(row["attempts"]) + 1
                window_started = float(row["window_started_at"])
            blocked_until = now + LOGIN_WINDOW_SECONDS if attempts >= LOGIN_MAX_ATTEMPTS else 0
            db.execute(
                """INSERT INTO panel_login_attempts
                   (client_hash, attempts, window_started_at, blocked_until)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(client_hash) DO UPDATE SET
                     attempts = excluded.attempts,
                     window_started_at = excluded.window_started_at,
                     blocked_until = excluded.blocked_until""",
                (client_hash, attempts, window_started, blocked_until),
            )

    def login(
        self,
        *,
        password: str,
        challenge_cookie: str,
        challenge_form: str,
        client_id: str,
    ) -> str:
        if not self.configured:
            raise AuthNotConfigured("El acceso de propietaria aún no está configurado.")
        if not self._consume_login_challenge(challenge_cookie, challenge_form):
            raise LoginChallengeError("La solicitud de acceso caducó. Vuelve a intentarlo.")

        now = time.time()
        client_hash = self._client_hash(client_id)
        if self._check_rate_limit(client_hash, now):
            raise LoginRateLimited("Demasiados intentos. Espera antes de volver a intentarlo.")

        candidate = password.encode("utf-8")
        expected = self.owner_password.encode("utf-8")
        if not hmac.compare_digest(candidate, expected):
            self._record_failed_login(client_hash, now)
            raise AuthError("No se pudo iniciar sesión.")

        with self._connect() as db:
            db.execute("DELETE FROM panel_login_attempts WHERE client_hash = ?", (client_hash,))
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        token_hash = self._hash("session", session_token)
        with self._connect() as db:
            db.execute(
                """INSERT INTO panel_sessions
                   (token_hash, csrf_token, created_at, last_seen_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_hash, csrf_token, now, now, now + SESSION_TTL_SECONDS),
            )
        return session_token

    def get_session(self, session_token: str | None) -> OwnerSession | None:
        if not self.configured or not session_token or len(session_token) > 100:
            return None
        token_hash = self._hash("session", session_token)
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT csrf_token, created_at, last_seen_at, expires_at
                   FROM panel_sessions WHERE token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if (
                now >= float(row["expires_at"])
                or now - float(row["last_seen_at"]) >= SESSION_IDLE_SECONDS
            ):
                db.execute("DELETE FROM panel_sessions WHERE token_hash = ?", (token_hash,))
                return None
            db.execute(
                "UPDATE panel_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
        return OwnerSession(csrf_token=str(row["csrf_token"]))

    @staticmethod
    def validate_csrf(session: OwnerSession, submitted_token: str | None) -> bool:
        return bool(
            submitted_token
            and hmac.compare_digest(session.csrf_token, submitted_token)
        )

    def logout(self, session_token: str | None) -> None:
        if not session_token:
            return
        with self._connect() as db:
            db.execute(
                "DELETE FROM panel_sessions WHERE token_hash = ?",
                (self._hash("session", session_token),),
            )
