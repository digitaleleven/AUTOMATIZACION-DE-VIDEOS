"""Transcription response normalization and 2–4-word caption grouping."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import SubtitleSegment, WordTiming

_TOKEN_RE = re.compile(r"\S+")
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)\]]*$")


class TranscriptionError(RuntimeError):
    """Raised when a transcription response contains no usable timing data."""


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _split_text(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.strip())


def _words_from_text(text: str, start: float, end: float) -> list[WordTiming]:
    tokens = _split_text(text)
    if not tokens:
        return []
    weights = [max(1, len(token.strip(".,!?;:()[]{}\"'"))) for token in tokens]
    total_weight = sum(weights)
    duration = max(0.0, end - start)
    cursor = start
    words: list[WordTiming] = []
    for index, (token, weight) in enumerate(zip(tokens, weights)):
        token_end = end if index == len(tokens) - 1 else cursor + duration * weight / total_weight
        words.append(WordTiming(token, cursor, token_end))
        cursor = token_end
    return words


def normalize_transcription(response: Any, chunk_offset: float = 0.0) -> list[WordTiming]:
    """Use precise word timestamps when returned, else distribute segment timing."""
    raw_words = _value(response, "words", None) or []
    words: list[WordTiming] = []
    for raw in raw_words:
        text = str(_value(raw, "word", _value(raw, "text", ""))).strip()
        try:
            start = float(_value(raw, "start"))
            end = float(_value(raw, "end"))
        except (TypeError, ValueError):
            continue
        if text and start >= 0 and end > start:
            words.append(WordTiming(text, start + chunk_offset, end + chunk_offset))

    if words:
        return sorted(words, key=lambda item: (item.start, item.end))

    raw_segments = _value(response, "segments", None) or []
    for segment in raw_segments:
        text = str(_value(segment, "text", "")).strip()
        try:
            start = float(_value(segment, "start"))
            end = float(_value(segment, "end"))
        except (TypeError, ValueError):
            continue
        if text and start >= 0 and end > start:
            words.extend(_words_from_text(text, start + chunk_offset, end + chunk_offset))

    if not words:
        text = str(_value(response, "text", "")).strip()
        duration = float(_value(response, "duration", 0) or 0)
        if text and duration > 0:
            words.extend(_words_from_text(text, chunk_offset, chunk_offset + duration))

    if not words:
        raise TranscriptionError("OpenAI returned no transcript words with usable timestamps.")
    return sorted(words, key=lambda item: (item.start, item.end))


def _sentence_groups(words: Sequence[WordTiming]) -> list[list[WordTiming]]:
    groups: list[list[WordTiming]] = []
    sentence: list[WordTiming] = []
    for word in words:
        sentence.append(word)
        if _SENTENCE_END_RE.search(word.text):
            groups.append(sentence)
            sentence = []
    if sentence:
        groups.append(sentence)
    return groups


def _partition_sentence(words: Sequence[WordTiming]) -> list[list[WordTiming]]:
    remaining = list(words)
    if len(remaining) <= 4:
        return [remaining] if remaining else []
    group_sizes: list[int] = []
    while len(remaining) > 4:
        # Avoid leaving a one-word caption at the end of the sentence.
        size = 3 if len(remaining) - 4 == 1 else 4
        group_sizes.append(size)
        remaining = remaining[size:]
    if remaining:
        group_sizes.append(len(remaining))

    result: list[list[WordTiming]] = []
    index = 0
    for size in group_sizes:
        result.append(list(words[index:index + size]))
        index += size
    return result


def build_subtitle_segments(
    words: Sequence[WordTiming],
    video_duration: float | None = None,
    min_words: int = 2,
    max_words: int = 4,
) -> list[SubtitleSegment]:
    if min_words != 2 or max_words != 4:
        if min_words < 1 or max_words < min_words:
            raise ValueError("Invalid subtitle word-count bounds.")

    ordered = sorted(words, key=lambda item: (item.start, item.end))
    valid = [
        word for word in ordered
        if word.text.strip() and word.start >= 0 and word.end > word.start
    ]
    if video_duration is not None:
        valid = [
            WordTiming(word.text, word.start, min(word.end, video_duration))
            for word in valid if word.start < video_duration
        ]
    if len(valid) < min_words:
        raise TranscriptionError("At least two timed words are required to create captions.")

    # Keep sentence punctuation when possible, then merge short sentence tails
    # with adjacent words so every caption remains within the requested range.
    sentence_groups = _sentence_groups(valid)
    chunks: list[list[WordTiming]] = []
    carry: list[WordTiming] = []
    for sentence in sentence_groups:
        combined = carry + sentence
        carry = []
        if len(combined) < min_words:
            carry = combined
            continue
        chunks.extend(_partition_sentence(combined))
    if carry:
        if chunks and len(chunks[-1]) + len(carry) <= max_words:
            chunks[-1].extend(carry)
        elif len(carry) >= min_words:
            chunks.extend(_partition_sentence(carry))
        else:
            # Rebalance a final single word from the previous full group.
            if chunks and len(chunks[-1]) == max_words:
                last = chunks[-1].pop()
                chunks.append([last, *carry])
            else:
                raise TranscriptionError("Could not group the final word into a 2–4-word caption.")

    captions = [
        SubtitleSegment(
            text=" ".join(word.text for word in chunk),
            start=max(0.0, chunk[0].start),
            end=min(video_duration, chunk[-1].end) if video_duration is not None else chunk[-1].end,
        )
        for chunk in chunks
    ]
    captions = [caption for caption in captions if caption.end > caption.start]
    if not captions or any(not (min_words <= len(_split_text(c.text)) <= max_words) for c in captions):
        raise TranscriptionError("Subtitle grouping did not produce valid 2–4-word captions.")
    return captions


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def captions_to_srt(
    captions: Sequence[SubtitleSegment],
    emoji_by_keyword: Mapping[str, str] | None = None,
) -> str:
    """Serialize timed 2–4-word captions into UTF-8 SubRip text."""
    if not captions:
        raise TranscriptionError("Cannot create an SRT file without captions.")
    cues: list[str] = []
    keyword_emojis = emoji_by_keyword or {}
    for index, caption in enumerate(captions, start=1):
        words = _split_text(caption.text)
        if not 2 <= len(words) <= 4:
            raise TranscriptionError("Every SRT cue must contain 2–4 words.")
        if caption.start < 0 or caption.end <= caption.start:
            raise TranscriptionError("SRT cue times must be positive and increasing.")
        text = " ".join(caption.text.splitlines()).strip()
        folded_text = text.casefold()
        emoji = next(
            (symbol for keyword, symbol in keyword_emojis.items() if keyword.casefold() in folded_text),
            "",
        )
        if emoji:
            text = f"{text} {emoji}"
        cues.append(
            f"{index}\n{_srt_timestamp(caption.start)} --> {_srt_timestamp(caption.end)}\n{text}"
        )
    return "\n\n".join(cues) + "\n"

