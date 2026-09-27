"""Helpery używane w kilku miejscach - czas w UTC i operacje na tekście."""

from __future__ import annotations

import re
from datetime import UTC, datetime

BRACKET_ARTIFACTS = re.compile(r"\[[^\[\]]{0,40}\]")
WHITESPACE = re.compile(r"\s+")
REPEATED_SENTENCE = re.compile(r"(?:\b(.+?)\b)(?:\s+\1\b){1,}")
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


def utcnow() -> datetime:
    # SQLite nie trzyma strefy czasowej, więc zapisujemy naiwny UTC i doklejamy "Z" przy serializacji.
    return datetime.now(UTC).replace(tzinfo=None)


def to_iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: str) -> str:
    text = BRACKET_ARTIFACTS.sub(" ", value)
    text = INVISIBLE.sub("", text).replace("\xa0", " ")
    return WHITESPACE.sub(" ", text).strip()


def strip_loops(value: str) -> str:
    """Whisper potrafi wpaść w pętlę i powtarzać tę samą frazę wielokrotnie."""
    return REPEATED_SENTENCE.sub(r"\1", value).strip()


def format_timestamp(seconds: float, always_hours: bool = True) -> str:
    if seconds < 0:
        seconds = 0.0
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if hours or always_hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
