import itertools
import re

import pytest

from app.services.vtt import VttConfig, VttSegment, VttWord, build_cues, render_webvtt
from app.utils import format_timestamp

TIMING = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2}):(\d{2}):(\d{2})\.(\d{3})$")


def words_from(text: str, start: float, step: float = 0.5) -> list[VttWord]:
    words = []
    position = start
    for token in text.split():
        words.append(VttWord(position, position + step, token))
        position += step
    return words


def test_webvtt_starts_with_header_and_cue_ids():
    segment = VttSegment(0.0, 3.0, "Dzień dobry", words_from("Dzień dobry", 0.0))
    content = render_webvtt([segment])

    lines = content.splitlines()
    assert lines[0] == "WEBVTT"
    assert lines[2] == "1"
    assert TIMING.match(lines[3])
    assert lines[4] == "Dzień dobry"


def test_cue_is_split_on_long_pause():
    segment = VttSegment(
        start=0.0,
        end=12.0,
        text="",
        words=[
            VttWord(0.0, 0.6, "Pierwsze"),
            VttWord(0.6, 1.2, "zdanie."),
            VttWord(8.0, 8.6, "Drugie"),
            VttWord(8.6, 9.4, "zdanie."),
        ],
    )
    cues = build_cues([segment])

    assert len(cues) == 2
    assert cues[0].text == "Pierwsze zdanie."
    assert cues[1].text == "Drugie zdanie."
    assert cues[1].start > 7.0


def test_long_segment_is_split_by_duration():
    words = [VttWord(i * 0.4, i * 0.4 + 0.4, f"slowo{i}") for i in range(60)]
    cues = build_cues([VttSegment(0.0, 24.0, " ".join(w.text for w in words), words)])

    assert len(cues) > 1
    for cue in cues:
        assert cue.duration <= VttConfig().max_cue_duration * 1.35


def test_line_wrapping_respects_width():
    words = [VttWord(i * 0.3, i * 0.3 + 0.3, f"wyraz{i}") for i in range(12)]
    content = render_webvtt([VttSegment(0.0, 4.0, " ".join(w.text for w in words), words)])

    cue_lines = content.splitlines()[4:]
    assert cue_lines
    assert len(cue_lines) <= 2
    assert all(len(line) <= 60 for line in cue_lines)


def test_short_segment_without_word_timestamps_is_evenly_split():
    segment = VttSegment(0.0, 25.0, " ".join(f"slowo{i}" for i in range(40)))
    cues = build_cues([segment])

    assert len(cues) >= 4
    assert all(cue.text for cue in cues)


def test_empty_input_produces_valid_header():
    assert render_webvtt([]) == "WEBVTT\n"


def test_notes_are_rendered_as_vtt_note_block():
    segment = VttSegment(0.0, 1.0, "Tekst", words_from("Tekst", 0.0))
    content = render_webvtt([segment], notes=["job_id: abc", "language: pl"])

    assert "NOTE" in content
    assert "job_id: abc" in content


def test_timestamps_follow_word_timings():
    segment = VttSegment(0.0, 5.5, "Test", words_from("Test", 0.0))
    content = render_webvtt([segment])

    assert "00:00:00.000 --> 00:00:00.500" in content


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00.000"),
        (1.5, "00:00:01.500"),
        (65.25, "00:01:05.250"),
        (3725.007, "01:02:05.007"),
    ],
)
def test_format_timestamp(seconds, expected):
    assert format_timestamp(seconds) == expected


def test_cues_never_overlap():
    words = [
        VttWord(0.0, 0.5, "A."),
        VttWord(0.5, 1.0, "B."),
        VttWord(1.0, 1.5, "C."),
    ]
    cues = build_cues([VttSegment(0.0, 1.5, "A. B. C.", words)])

    for previous, following in itertools.pairwise(cues):
        assert following.start >= previous.end


@pytest.mark.parametrize("duration", [0.2, 1.0, 12.0])
def test_segment_without_words_keeps_timing(duration):
    segment = VttSegment(1.0, 1.0 + duration, "Krótki fragment tekstu")
    cues = build_cues([segment])

    assert cues
    assert cues[0].start == pytest.approx(1.0)
