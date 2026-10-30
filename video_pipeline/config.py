"""Configuration for local processing and the fixed MATRIX MOTION Drive folders."""

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


# Server-side OAuth automation is deliberately bounded to these three folders.
INPUT_FOLDER_ID = "13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg"
PROCESSED_FOLDER_ID = "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN"
LOGOS_FOLDER_ID = "1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW"
RENDER_IDENTITY_VERSION = "v1"


@dataclass(frozen=True)
class PipelineConfig:
    """Settings safe to log; credential values are read only at call time."""

    input_folder_id: str | None = None
    processed_folder_id: str | None = None
    logos_folder_id: str | None = None
    local_input_dir: str = "data/incoming"
    output_dir: str = "outputs"
    openai_model: str = "whisper-1"

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        return cls(
            input_folder_id=INPUT_FOLDER_ID,
            processed_folder_id=PROCESSED_FOLDER_ID,
            logos_folder_id=LOGOS_FOLDER_ID,
        )

    @property
    def openai_api_key_present(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    @property
    def google_oauth_token_present(self) -> bool:
        """Legacy status only; access tokens should be supplied per browser session."""
        return bool(os.getenv("GOOGLE_OAUTH_TOKEN"))

    @property
    def google_picker_configured(self) -> bool:
        return all(
            os.getenv(name)
            for name in ("GOOGLE_DRIVE_API_KEY", "GOOGLE_DRIVE_APP_ID")
        ) and bool(
            os.getenv("GOOGLE_DRIVE_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        )

    @property
    def google_oauth_configured(self) -> bool:
        redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")
        try:
            redirect_path = urlsplit(redirect_uri).path
        except ValueError:
            redirect_path = ""
        return (
            os.getenv("GOOGLE_DRIVE_OAUTH_ENABLED", "").strip().lower()
            in {"1", "true", "yes", "on"}
            and redirect_path == "/oauth/google/callback"
            and all(
                os.getenv(name)
                for name in (
                    "GOOGLE_OAUTH_CLIENT_ID",
                    "GOOGLE_OAUTH_CLIENT_SECRET",
                    "GOOGLE_OAUTH_ALLOWED_EMAIL",
                    "SESSION_SECRET",
                )
            )
        )
