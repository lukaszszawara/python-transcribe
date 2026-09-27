"""Wrapper na OpenAI Whisper: ładowanie modelu, transkrypcja chunków, postprocessing."""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.services.vtt import VttSegment, VttWord
from app.utils import clean_text, strip_loops

logger = logging.getLogger("transcription.whisper")


@dataclass
class TranscriptResult:
    text: str
    language: str
    segments: list[VttSegment] = field(default_factory=list)
    duration: float = 0.0
    model: str = settings.whisper_model

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class ModelNotAvailableError(RuntimeError):
    pass


def _trim_cut_off_tail(
    text: str, words: list[VttWord], chunk_seconds: int, offset: float
) -> tuple[str, list[VttWord]]:
    """Ostatnie słowo przyciętego fragmentu to zwykle urwany sylab.

    Whisper dostaje koniec zdania w połowie, więc zostawia na końcu coś w rodzaju
    "witajmy w". Takie słowo wyrzucamy, ale tylko jeśli naprawdę leży na krawędzi
    cięcia i nie kończy się interpunkcją. Czasy słów są już przesunięte o offset,
    więc porównujemy pozycję wewnątrz fragmentu.
    """
    if not words:
        return text, words

    last = words[-1]
    if (last.end - offset) < chunk_seconds - 0.25:
        return text, words

    # whisper oznacza urwane słudo wielokropkiem ("w..."), a normalną kropkę
    # stawia dopiero po zakończeniu zdania
    looks_cut_off = last.text.endswith("...") or last.text.endswith("…")
    if not last.text.rstrip(".,!?… "):
        return " ".join(word.text for word in words[:-1]).strip(), words[:-1]
    if not looks_cut_off and last.text[-1:] in ".!?…":
        return text, words

    trimmed = words[:-1]
    if not trimmed:
        return "", []
    return " ".join(word.text for word in trimmed).strip(), trimmed


class WhisperTranscriber:
    """Model trzymamy w pamięci procesu workera i ładujemy tylko raz."""

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        model_dir: str | None = None,
    ) -> None:
        self.model_name = model_name or settings.whisper_model
        self.device = device or settings.whisper_device
        self.model_dir = model_dir or settings.whisper_model_dir
        self._model = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                import whisper
            except ImportError as exc:  # pragma: no cover - środowisko bez torch
                raise ModelNotAvailableError(
                    "Brak pakietu openai-whisper. Zainstaluj requirements.txt albo użyj obrazu Docker."
                ) from exc

            Path(self.model_dir).mkdir(parents=True, exist_ok=True)
            if settings.whisper_force_single_thread:
                # Kilka workerów na jednym CPU potrafi się wzajemnie dusić wątkami BLAS.
                import torch

                torch.set_num_threads(1)

            logger.info(
                "ładowanie modelu whisper=%s device=%s", self.model_name, self.device
            )
            try:
                self._model = whisper.load_model(
                    self.model_name, device=self.device, download_root=self.model_dir
                )
            except Exception as exc:
                raise ModelNotAvailableError(
                    f"Nie udało się załadować modelu '{self.model_name}': {exc}"
                ) from exc
            logger.info("model gotowy")
            return self._model

    def _allowed_params(self) -> set[str]:
        import inspect

        import whisper

        # W whisperze 20240930 language, task i fp16 przechodzą przez **decode_options,
        # a nie przez sygnaturę transcribe(), więc patrzenie tylko na sygnaturę
        # gubiłoby wskazanie języka. Bierzemy sumę sygnatury i pól DecodingOptions.
        allowed = set(inspect.signature(whisper.transcribe).parameters)
        options = getattr(whisper.decoding, "DecodingOptions", None)
        if options is not None:
            allowed |= {field.name for field in dataclasses.fields(options)}
        return allowed

    def _transcribe_kwargs(self, language: str, initial_prompt: str | None) -> dict:
        # "verbose" zostawiamy domyślne (None) - przy False whisper rysuje pasek postępu,
        # a przy True wypisuje rozpoznany język.
        desired = {
            "task": "transcribe",
            "language": language,
            "fp16": self.device.startswith("cuda"),
            "temperature": settings.temperature,
            "word_timestamps": settings.word_timestamps,
            "initial_prompt": initial_prompt or settings.initial_prompt,
            # Między chunkami nie ciągniemy kontekstu - Whisper startuje wtedy
            # od nowa i nie wpada w pętlę powtarzania tego samego zdania.
            "condition_on_previous_text": False,
        }
        allowed = self._allowed_params()
        return {key: value for key, value in desired.items() if key in allowed}

    def transcribe_chunks(
        self,
        chunks: list[Path],
        language: str,
        *,
        chunk_seconds: int,
        initial_prompt: str | None = None,
        on_chunk: Callable[[int, int], None] | None = None,
    ) -> TranscriptResult:
        model = self.load()
        kwargs = self._transcribe_kwargs(language, initial_prompt)

        segments: list[VttSegment] = []
        texts: list[str] = []
        detected: str | None = None

        for index, chunk in enumerate(chunks):
            offset = index * chunk_seconds
            if on_chunk:
                on_chunk(index, len(chunks))

            raw = model.transcribe(str(chunk), **kwargs)
            detected = raw.get("language") or detected
            is_last_chunk = index == len(chunks) - 1

            for segment in raw.get("segments", []) or []:
                text = clean_text(segment.get("text", ""))
                if not text:
                    continue
                words = [
                    VttWord(
                        start=float(word.get("start", 0.0)) + offset,
                        end=float(word.get("end", 0.0)) + offset,
                        text=clean_text(word.get("word", "")),
                        probability=word.get("probability"),
                    )
                    for word in segment.get("words", []) or []
                ]
                words = [word for word in words if word.text]

                if not is_last_chunk and chunk_seconds > 0:
                    text, words = _trim_cut_off_tail(text, words, chunk_seconds, offset)
                    if not text:
                        continue
                texts.append(text)

                segments.append(
                    VttSegment(
                        start=float(segment.get("start", 0.0)) + offset,
                        end=float(segment.get("end", 0.0)) + offset,
                        text=text,
                        words=words,
                    )
                )

            logger.info(
                "chunk %s/%s gotowy (offset %.0f s)", index + 1, len(chunks), offset
            )

        if on_chunk:
            on_chunk(len(chunks), len(chunks))

        return TranscriptResult(
            text=strip_loops(" ".join(texts)),
            language=detected or language,
            segments=segments,
            duration=len(chunks) * chunk_seconds,
        )

    def transcribe_file(
        self,
        path: Path,
        language: str,
        *,
        initial_prompt: str | None = None,
    ) -> TranscriptResult:
        return self.transcribe_chunks(
            [path], language, chunk_seconds=0, initial_prompt=initial_prompt
        )

_transcriber: WhisperTranscriber | None = None
_transcriber_lock = threading.Lock()


def get_transcriber() -> WhisperTranscriber:
    global _transcriber
    if _transcriber is None:
        with _transcriber_lock:
            if _transcriber is None:
                _transcriber = WhisperTranscriber()
    return _transcriber


def reset_transcriber() -> None:
    """Używane w testach, żeby nie dziedziczyć modelu między testami."""
    global _transcriber
    with _transcriber_lock:
        _transcriber = None
