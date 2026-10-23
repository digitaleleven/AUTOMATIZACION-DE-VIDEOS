"""Non-secret configuration for local video processing."""

from dataclasses import dataclass
import os


INPUT_FOLDER_ID = "13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg"
PROCESSED_FOLDER_ID = "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN"
LOGOS_FOLDER_ID = "1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW"


@dataclass(frozen=True)
class PipelineConfig:
    """Settings that are safe to log; credential values are never stored here."""

    input_folder_id: str = INPUT_FOLDER_ID
    processed_folder_id: str = PROCESSED_FOLDER_ID
    logos_folder_id: str = LOGOS_FOLDER_ID
    local_input_dir: str = "data/incoming"
    output_dir: str = "outputs"
    openai_model: str = "whisper-1"
    shotstack_environment: str = "v1"
    shotstack_poll_seconds: float = 5.0
    shotstack_timeout_seconds: float = 900.0

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        environment = os.getenv("SHOTSTACK_ENVIRONMENT", "v1").strip().lower()
        if environment not in {"v1", "stage"}:
            raise ValueError("SHOTSTACK_ENVIRONMENT must be 'v1' or 'stage'.")
        return cls(
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

