"""Configuration for local processing and explicitly selected Drive folders."""

from dataclasses import dataclass
import json
import os
from pathlib import Path


# Legacy folder IDs remain available for CLI and compatibility tests only.
# The web panel uses Picker-selected IDs and never assumes these grant access.
INPUT_FOLDER_ID = "13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg"
PROCESSED_FOLDER_ID = "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN"
LOGOS_FOLDER_ID = "1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW"
DRIVE_SELECTION_PATH = Path("data/drive_folders.json")


def _selected_drive_folders() -> dict[str, dict[str, str]]:
    """Read only user-selected folder IDs; never assume access from known IDs."""
    try:
        selection = json.loads(DRIVE_SELECTION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(selection, dict):
        return {}
    return {
        key: value
        for key, value in selection.items()
        if key in {"entrada", "procesados", "logos"}
        and isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and value["id"].strip()
    }


@dataclass(frozen=True)
class PipelineConfig:
    """Settings safe to log; credential values are read only at call time."""

    input_folder_id: str | None = None
    processed_folder_id: str | None = None
    logos_folder_id: str | None = None
    local_input_dir: str = "data/incoming"
    output_dir: str = "outputs"
    openai_model: str = "whisper-1"
    shotstack_environment: str = "stage"
    shotstack_poll_seconds: float = 5.0
    shotstack_timeout_seconds: float = 900.0

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        environment = os.getenv("SHOTSTACK_ENVIRONMENT", "stage").strip().lower()
        if environment != "stage":
            raise ValueError("This project is locked to Shotstack Stage; production is disabled.")
        selected = _selected_drive_folders()
        return cls(
            input_folder_id=selected.get("entrada", {}).get("id"),
            processed_folder_id=selected.get("procesados", {}).get("id"),
            logos_folder_id=selected.get("logos", {}).get("id"),
            shotstack_environment=environment,
            shotstack_poll_seconds=float(os.getenv("SHOTSTACK_POLL_SECONDS", "5")),
            shotstack_timeout_seconds=float(os.getenv("SHOTSTACK_TIMEOUT_SECONDS", "900")),
        )

    @property
    def openai_api_key_present(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    @property
    def shotstack_api_key_present(self) -> bool:
        return bool(os.getenv("SHOTSTACK_API_KEY"))

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
