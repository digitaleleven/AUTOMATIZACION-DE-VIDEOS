"""Non-secret configuration for the video pipeline."""

from dataclasses import dataclass
import os


INPUT_FOLDER_ID = "13xrMgSYEOkGANxrUYbmYw1t6TdvXWRYg"
PROCESSED_FOLDER_ID = "1J6Z0bsWb2dQNKY4hyq4xl7tigtm_pQKN"
LOGOS_FOLDER_ID = "1t46UE2Mog9JRDSe9TzoPxfs96nE9qDTW"


@dataclass(frozen=True)
class PipelineConfig:
    """Pipeline settings; credentials are read only when a live adapter is added."""

    input_folder_id: str = INPUT_FOLDER_ID
    processed_folder_id: str = PROCESSED_FOLDER_ID
    logos_folder_id: str = LOGOS_FOLDER_ID
    local_input_dir: str = "data/incoming"
    openai_api_key_present: bool = bool(os.getenv("OPENAI_API_KEY"))
    google_oauth_token_present: bool = bool(os.getenv("GOOGLE_OAUTH_TOKEN"))
    shotstack_api_key_present: bool = bool(os.getenv("SHOTSTACK_API_KEY"))

    @classmethod
    def from_environment(cls) -> "PipelineConfig":
        return cls()

