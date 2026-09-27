"""Testy logiki chunkowania i sklejania fragmentów."""

from pathlib import Path

from app.services.transcriber import TranscriptResult, WhisperTranscriber
from app.services.vtt import VttSegment, VttWord


class FakeModel:
    """Zwraca z góry ustalone słowa z uwzględnieniem przesunięcia o offset."""

    def __init__(self, per_chunk: list[list[tuple[float, float, str]]]):
        self.per_chunk = per_chunk
        self.calls: list[dict] = []

    def transcribe(self, path: str, **kwargs):
        self.calls.append({"path": path, "kwargs": kwargs})
        index = len(self.calls) - 1
        words = self.per_chunk[index]
        return {
            "language": "pl",
            "segments": [
                {
                    "start": words[0][0],
                    "end": words[-1][1],
                    "text": " ".join(word[2] for word in words),
                    "words": [
                        {"start": s, "end": e, "word": t, "probability": 0.9}
                        for s, e, t in words
                    ],
                }
            ],
        }


class FakeWhisper:
    def __init__(self, model):
        self.model = model

    def load_model(self, *args, **kwargs):
        return self.model


def make_transcriber(model) -> WhisperTranscriber:
    transcriber = WhisperTranscriber()
    transcriber._model = model
    return transcriber


def test_offsets_from_later_chunks_are_added():
    model = FakeModel(
        [
            [(0.0, 1.0, "Pierwsze"), (1.0, 2.0, "zdanie.")],
            [(0.0, 1.0, "Drugie"), (1.0, 2.0, "zdanie.")],
        ]
    )
    chunks = [Path("chunk_0.wav"), Path("chunk_1.wav")]

    result = make_transcriber(model).transcribe_chunks(chunks, "pl", chunk_seconds=30)

    assert result.segments[0].words[0].start == 0.0
    assert result.segments[1].words[0].start == 30.0
    assert result.segments[1].words[-1].end == 32.0
    assert result.text == "Pierwsze zdanie. Drugie zdanie."


def test_trailing_cut_off_word_is_dropped_at_chunk_border():
    model = FakeModel(
        [
            [(0.0, 1.0, "Witam"), (1.0, 2.0, "Polskim"), (2.0, 3.0, "Radiu."), (3.0, 4.0, "w")],
            [(0.0, 1.0, "Kolejne"), (1.0, 2.0, "zdanie.")],
        ]
    )
    chunks = [Path("chunk_0.wav"), Path("chunk_1.wav")]

    result = make_transcriber(model).transcribe_chunks(chunks, "pl", chunk_seconds=4)

    # ostatnie słowo pierwszego fragmentu leżało na krawędzi cięcia, więc znika
    assert [word.text for word in result.segments[0].words] == ["Witam", "Polskim", "Radiu."]
    assert result.text == "Witam Polskim Radiu. Kolejne zdanie."


def test_word_with_ellipsis_at_chunk_border_is_dropped():
    model = FakeModel(
        [
            [(0.0, 1.0, "Dzień"), (1.0, 2.0, "dobry,"), (2.0, 3.0, "witam"), (3.0, 4.0, "w...")],
            [(0.0, 1.0, "Dalej.")],
        ]
    )
    result = make_transcriber(model).transcribe_chunks(
        [Path("chunk_0.wav"), Path("chunk_1.wav")], "pl", chunk_seconds=4
    )

    assert result.segments[0].text == "Dzień dobry, witam"


def test_word_ending_sentence_at_chunk_border_is_kept():
    model = FakeModel(
        [
            [(0.0, 3.9, "Koniec."), (3.9, 4.0, "Hmm")],
            [(0.0, 1.0, "Dalej.")],
        ]
    )
    chunks = [Path("chunk_0.wav"), Path("chunk_1.wav")]

    result = make_transcriber(model).transcribe_chunks(chunks, "pl", chunk_seconds=4)

    assert [word.text for word in result.segments[0].words] == ["Koniec."]


def test_last_chunk_keeps_every_word():
    model = FakeModel([[(0.0, 1.0, "Początek"), (1.0, 2.0, "w")]])
    result = make_transcriber(model).transcribe_chunks([Path("chunk_0.wav")], "pl", chunk_seconds=30)

    assert [word.text for word in result.segments[0].words] == ["Początek", "w"]


def test_progress_callback_reports_every_chunk():
    model = FakeModel([[(0.0, 1.0, "A")], [(0.0, 1.0, "B")], [(0.0, 1.0, "C")]])
    reported: list[tuple[int, int]] = []

    make_transcriber(model).transcribe_chunks(
        [Path("a.wav"), Path("b.wav"), Path("c.wav")],
        "pl",
        chunk_seconds=30,
        on_chunk=lambda done, total: reported.append((done, total)),
    )

    assert reported == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_initial_prompt_is_passed_to_whisper():
    model = FakeModel([[(0.0, 1.0, "Test")]])
    make_transcriber(model).transcribe_chunks(
        [Path("a.wav")], "pl", chunk_seconds=0, initial_prompt="Polskie Radio"
    )

    assert model.calls[0]["kwargs"]["initial_prompt"] == "Polskie Radio"
    assert model.calls[0]["kwargs"]["language"] == "pl"


def test_verbose_is_not_forced_so_whisper_stays_quiet():
    model = FakeModel([[(0.0, 1.0, "Test")]])
    make_transcriber(model).transcribe_chunks([Path("a.wav")], "pl", chunk_seconds=0)

    assert "verbose" not in model.calls[0]["kwargs"]


def test_word_count_counts_words_in_result():
    result = TranscriptResult(text="raz dwa trzy", language="pl")

    assert result.word_count == 3


def test_chunks_are_ignored_when_segment_has_no_words():
    model = FakeModel(
        [
            [
                (0.0, 2.0, "Bez"),
                (2.0, 3.0, "znaczników"),
            ]
        ]
    )
    model.per_chunk[0] = []

    class NoWordsModel:
        def transcribe(self, path, **kwargs):
            return {
                "language": "pl",
                "segments": [
                    {"start": 0.0, "end": 3.0, "text": "  Bez   znaczników  ", "words": []}
                ],
            }

    result = make_transcriber(NoWordsModel()).transcribe_chunks(
        [Path("a.wav")], "pl", chunk_seconds=0
    )

    assert result.text == "Bez znaczników"
    assert result.segments[0].words == []


def test_empty_transcription_returns_empty_segments():
    class EmptyModel:
        def transcribe(self, path, **kwargs):
            return {"language": "pl", "segments": []}

    result = make_transcriber(EmptyModel()).transcribe_chunks(
        [Path("a.wav")], "pl", chunk_seconds=0
    )

    assert result.text == ""
    assert result.segments == []


def test_vtt_word_probability_is_preserved():
    segment = VttSegment(0.0, 1.0, "Słowo", [VttWord(0.0, 1.0, "Słowo", 0.42)])

    assert segment.words[0].probability == 0.42
