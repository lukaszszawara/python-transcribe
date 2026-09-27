"""Pobieranie pliku audio i przygotowanie go dla Whispera (16 kHz, mono, WAV)."""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.utils import utcnow

logger = logging.getLogger("transcription.audio")

AUDIO_CONTENT_TYPES = ("audio/", "video/", "application/octet-stream", "binary/")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma")
SAMPLE_RATE = 16_000


class AudioError(Exception):
    """Błąd na etapie pobierania lub konwersji pliku."""


@dataclass
class AudioSource:
    path: Path
    size_bytes: int
    duration: float
    sample_rate: int
    format_name: str


def require_ffmpeg() -> None:
    missing = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
    if missing:
        raise AudioError(
            f"Brak narzędzi: {', '.join(missing)}. Doinstaluj ffmpeg w obrazie kontenera."
        )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _check_public_host(url: str) -> None:
    if settings.allow_private_audio_urls:
        return
    hostname = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise AudioError(f"Nie można rozwiązać hosta: {hostname}") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise AudioError(
                f"Adres {address} jest wewnętrzny. Ustaw ALLOW_PRIVATE_AUDIO_URLS=true tylko lokalnie."
            )


def download(
    url: str,
    destination: Path,
    *,
    on_progress=None,
) -> int:
    _check_public_host(url)
    limit = settings.audio_max_size_mb * 1024 * 1024
    downloaded = 0
    timeout = httpx.Timeout(
        settings.download_timeout_seconds, connect=settings.download_connect_timeout_seconds
    )

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        try:
            with client.stream("GET", url, headers={"User-Agent": f"{settings.app_name}/{settings.app_version}"}) as response:
                if response.status_code >= 400:
                    raise AudioError(f"Serwer zwrócił HTTP {response.status_code} dla {url}")

                content_type = response.headers.get("content-type", "").lower()
                if content_type and not content_type.startswith(AUDIO_CONTENT_TYPES):
                    raise AudioError(
                        f"Typ zawartości {content_type} nie wygląda na audio. "
                        "Sprawdź, czy URL prowadzi do pliku, a nie do strony HTML."
                    )

                length = response.headers.get("content-length")
                if length and int(length) > limit:
                    raise AudioError(
                        f"Plik jest większy niż limit {settings.audio_max_size_mb} MB"
                    )

                with open(destination, "wb") as handle:
                    for block in response.iter_bytes(chunk_size=64 * 1024):
                        downloaded += len(block)
                        if downloaded > limit:
                            raise AudioError(
                                f"Plik przekroczył limit {settings.audio_max_size_mb} MB w trakcie pobierania"
                            )
                        handle.write(block)
                        if on_progress and length:
                            on_progress(downloaded / int(length))
        except httpx.HTTPError as exc:
            raise AudioError(f"Błąd pobierania: {type(exc).__name__}: {exc}") from exc

    if downloaded == 0:
        raise AudioError("Pobrany plik jest pusty")
    return downloaded


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,size",
            "-show_entries",
            "stream=codec_type,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe nie odczytał pliku: {result.stderr.strip()[:400]}")

    data = json.loads(result.stdout or "{}")
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise AudioError("W pliku nie znaleziono strumienia audio")

    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise AudioError("Nie udało się ustalić długości pliku audio")
    if duration > settings.audio_max_duration_seconds:
        hours = settings.audio_max_duration_seconds / 3600
        raise AudioError(
            f"Plik trwa {duration / 3600:.1f} h, a limit to {hours:.0f} h"
        )
    return duration


def to_wav16k(source: Path, target: Path) -> Path:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioError(f"Konwersja do WAV nie powiodła się: {result.stderr.strip()[:400]}")
    return target


def split_wav(path: Path, target_dir: Path, chunk_seconds: int) -> list[Path]:
    """Tnie gotowy WAV po granicach próbek - bez ponownego kodowania."""
    target_dir.mkdir(parents=True, exist_ok=True)
    frames_per_chunk = chunk_seconds * SAMPLE_RATE
    chunks: list[Path] = []

    with wave.open(str(path), "rb") as source:
        if source.getnframes() <= frames_per_chunk:
            return [path]
        index = 0
        while True:
            frames = source.readframes(frames_per_chunk)
            if not frames:
                break
            target = target_dir / f"chunk_{index:04d}.wav"
            with wave.open(str(target), "wb") as sink:
                sink.setnchannels(source.getnchannels())
                sink.setsampwidth(source.getsampwidth())
                sink.setframerate(source.getframerate())
                sink.writeframes(frames)
            chunks.append(target)
            index += 1
            if index % 10 == 0:
                logger.info("podzielono %s fragmentów", index)
    return chunks


def prepare(
    url: str,
    work_dir: Path,
    *,
    chunk_seconds: int | None = None,
    on_progress=None,
) -> tuple[AudioSource, Path, list[Path]]:
    require_ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)
    chunk_seconds = chunk_seconds or settings.audio_chunk_seconds

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".mp3"

    raw = work_dir / f"source_{utcnow().strftime('%H%M%S')}{suffix}"
    logger.info("pobieranie %s", url)
    size = download(url, raw, on_progress=on_progress)
    duration = probe_duration(raw)

    wav = to_wav16k(raw, work_dir / "audio_16k.wav")
    chunks = split_wav(wav, work_dir / "chunks", chunk_seconds)

    source = AudioSource(
        path=raw,
        size_bytes=size,
        duration=duration,
        sample_rate=SAMPLE_RATE,
        format_name=suffix.lstrip("."),
    )
    logger.info(
        "gotowe: %s, %.1f s, %s, %s fragment(ów)",
        url,
        duration,
        size,
        len(chunks),
    )
    return source, wav, chunks


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - ścieżka awaryjna
        logger.warning("nie udało się usunąć %s: %s", path, exc)


class Workspace:
    """Katalog roboczy per zadanie, sprzątany w __exit__."""

    def __init__(self, job_id: str) -> None:
        self.path = Path(tempfile.mkdtemp(prefix=f"job_{job_id[:8]}_", dir=_temp_root()))

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _temp_root() -> str:
    root = Path(settings.temp_dir)
    root.mkdir(parents=True, exist_ok=True)
    return str(root)
