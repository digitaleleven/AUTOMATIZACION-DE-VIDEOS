"""OpenAI Whisper transcription with word-level timestamp support."""

import logging
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from .media import extract_audio_chunks
from .models import VideoInfo, WordTiming
from .subtitles import normalize_transcription

logger = logging.getLogger(__name__)


def transcribe_video(
    video: VideoInfo,
    work_dir: str | Path,
    model: str = "whisper-1",
    client: Any | None = None,
) -> list[WordTiming]:
    """Transcribe extracted chunks, preferring model-provided word timestamps."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not client and not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in Replit Secrets.")
    if client is None:
        client = OpenAI(api_key=api_key)

    all_words: list[WordTiming] = []
    for index, (audio_path, offset) in enumerate(extract_audio_chunks(video, work_dir), start=1):
        logger.info("Transcribing audio chunk %d", index)
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
        all_words.extend(normalize_transcription(response, chunk_offset=offset))
    if not all_words:
        raise RuntimeError("Transcription returned no usable words.")
    return sorted(all_words, key=lambda item: (item.start, item.end))

