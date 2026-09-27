"""Budowanie plików WebVTT na podstawie segmentów i słów z Whispera."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.utils import clamp, clean_text, format_timestamp

SOFT_BREAKS = ",;:—–"
HARD_BREAKS = ".!?…"


@dataclass
class VttWord:
    start: float
    end: float
    text: str
    probability: float | None = None


@dataclass
class VttSegment:
    start: float
    end: float
    text: str
    words: list[VttWord] = field(default_factory=list)


@dataclass
class VttConfig:
    max_cue_duration: float = 6.0
    max_chars_per_line: int = 42
    max_lines: int = 2
    min_cue_duration: float = 0.35
    pause_split: float = 0.6


@dataclass
class Cue:
    start: float
    end: float
    words: list[VttWord]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words).strip()

    @property
    def duration(self) -> float:
        return self.end - self.start


def _greedy_wrap(words: list[str], width: int) -> list[str]:
    if width < 1:
        width = 1
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _wrap_text(text: str, config: VttConfig) -> str:
    words = text.split()
    if not words:
        return ""
    if len(text) <= config.max_chars_per_line:
        return text

    lines = _greedy_wrap(words, config.max_chars_per_line)
    if len(lines) > config.max_lines:
        # Lepiej rozłożyć tekst na równo w dwóch wierszach niż generować cztery
        # krótkie - czas czytania zależy od liczby linii, nie od idealnej szerokości.
        lines = _greedy_wrap(words, math.ceil(len(text) / config.max_lines))
        lines = lines[: config.max_lines]

    return "\n".join(line.strip() for line in lines)


def _should_close(buffer: list[VttWord], next_word: VttWord, config: VttConfig) -> bool:
    if not buffer:
        return False
    last = buffer[-1]
    candidate = [*buffer, next_word]
    text = " ".join(word.text for word in candidate)

    if next_word.start - last.end > config.pause_split:
        return True
    if next_word.end - buffer[0].start > config.max_cue_duration:
        return True
    if len(text) > config.max_chars_per_line * config.max_lines:
        return True
    if last.text[-1:] in HARD_BREAKS:
        return next_word.end - buffer[0].start >= 1.2
    if last.text[-1:] in SOFT_BREAKS:
        return next_word.end - buffer[0].start >= config.max_cue_duration * 0.75
    return False


def _group_words(segments: list[VttSegment], config: VttConfig) -> list[Cue]:
    cues: list[Cue] = []
    buffer: list[VttWord] = []

    for segment in segments:
        if not segment.words:
            # Fragmenty bez znaczników czasu są już przeliczone przez _split_evenly,
            # więc zapisujemy je jako gotowe cue i nie mieszamy z sąsiadami.
            cues.append(Cue(segment.start, segment.end, [VttWord(segment.start, segment.end, segment.text)]))
            continue

        for word in segment.words:
            text = clean_text(word.text)
            if not text:
                continue
            word = VttWord(word.start, word.end, text, word.probability)
            if _should_close(buffer, word, config):
                cues.append(Cue(buffer[0].start, buffer[-1].end, buffer))
                buffer = []
            buffer.append(word)

    if buffer:
        cues.append(Cue(buffer[0].start, buffer[-1].end, buffer))
    return _merge_short_cues(cues, config)


def _merge_short_cues(cues: list[Cue], config: VttConfig) -> list[Cue]:
    merged: list[Cue] = []
    for cue in cues:
        previous = merged[-1] if merged else None
        orphan = cue.duration < config.min_cue_duration or (
            len(cue.words) == 1 and cue.duration < 1.0
        )
        room = previous is not None and previous.duration < config.max_cue_duration
        if previous and orphan and room and cue.start - previous.end <= config.pause_split:
            previous.words.extend(cue.words)
            previous.end = max(previous.end, cue.end)
            continue
        merged.append(cue)
    return merged


def _normalize_cues(cues: list[Cue], config: VttConfig) -> list[Cue]:
    normalized: list[Cue] = []
    for cue in cues:
        start = max(0.0, cue.start)
        end = max(cue.end, start + config.min_cue_duration)
        if normalized and start <= normalized[-1].end:
            start = normalized[-1].end + 0.001
            end = max(cue.end, start + config.min_cue_duration)
        if end - start > 0:
            normalized.append(Cue(start, end, cue.words))
    return normalized


def _split_evenly(segment: VttSegment, config: VttConfig) -> list[VttSegment]:
    """Fallback dla segmentów bez znaczników słów - dzielimy je po czasie."""
    text = clean_text(segment.text)
    if not text:
        return []
    duration = max(segment.end - segment.start, config.min_cue_duration)
    if duration <= config.max_cue_duration:
        return [VttSegment(segment.start, segment.end, text)]

    parts = math.ceil(duration / config.max_cue_duration)
    words = text.split()
    step = duration / parts
    per_part = max(1, math.ceil(len(words) / parts))

    segments: list[VttSegment] = []
    for index in range(parts):
        chunk = words[index * per_part : (index + 1) * per_part]
        if not chunk:
            continue
        start = segment.start + step * index
        end = segment.end if index == parts - 1 else start + step
        segments.append(VttSegment(start, end, " ".join(chunk)))
    return segments


def build_cues(segments: list[VttSegment], config: VttConfig | None = None) -> list[Cue]:
    config = config or VttConfig()
    expanded: list[VttSegment] = []
    for segment in segments:
        if segment.words:
            expanded.append(segment)
        else:
            expanded.extend(_split_evenly(segment, config))

    expanded = [
        segment
        for segment in expanded
        if clean_text(segment.text) or any(clean_text(word.text) for word in segment.words)
    ]
    if not expanded:
        return []
    return _normalize_cues(_group_words(expanded, config), config)


def render_webvtt(
    segments: list[VttSegment],
    *,
    config: VttConfig | None = None,
    notes: list[str] | None = None,
) -> str:
    config = config or VttConfig()
    cues = build_cues(segments, config)

    lines = ["WEBVTT", ""]
    if notes:
        lines.append("NOTE")
        lines.extend(notes)
        lines.append("")

    number = 0
    for cue in cues:
        text = _wrap_text(cue.text, config)
        if not text:
            continue
        number += 1
        lines.append(str(number))
        lines.append(
            f"{format_timestamp(cue.start)} --> "
            f"{format_timestamp(clamp(cue.end, 0, 86_399.999))}"
        )
        lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
