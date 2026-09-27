import threading
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import settings
from app.services import audio as audio_service
from app.services.audio import AudioError, Workspace, download, prepare, split_wav


def write_wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    """Cisza w 8 kHz wystarczy - sprawdzamy obróbkę pliku, nie zawartość dźwięku."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


@pytest.fixture
def http_server(tmp_path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}", tmp_path
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def sample_audio(tmp_path):
    return write_wav(tmp_path / "sample.wav", seconds=2.0)


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_download_stores_file(http_server, sample_audio):
    base_url, _ = http_server
    with Workspace("job-test") as work_dir:
        target = work_dir / "out.wav"
        size = download(f"{base_url}/sample.wav", target)

        assert target.exists()
        assert size == sample_audio.stat().st_size


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_download_rejects_html_response(http_server, tmp_path):
    (tmp_path / "page.html").write_text("<html>404</html>", encoding="utf-8")
    base_url, _ = http_server

    with Workspace("job-test") as work_dir, pytest.raises(AudioError, match="zawartości"):
        download(f"{base_url}/page.html", work_dir / "out.html")


def test_download_rejects_non_http_scheme():
    with Workspace("job-test") as work_dir, pytest.raises(AudioError):
        download("ftp://example.com/a.mp3", work_dir / "a.mp3")


def test_download_rejects_404(http_server, tmp_path):
    base_url, _ = http_server
    with Workspace("job-test") as work_dir, pytest.raises(AudioError, match="404"):
        download(f"{base_url}/missing.mp3", work_dir / "a.mp3")


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_probe_duration_reads_wav(sample_audio):
    assert audio_service.probe_duration(sample_audio) == pytest.approx(2.0, abs=0.05)


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_probe_rejects_file_without_audio(tmp_path):
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"\x00" * 64)

    with pytest.raises(AudioError):
        audio_service.probe_duration(empty)


def test_split_wav_returns_single_file_for_short_audio(sample_audio, tmp_path):
    chunks = split_wav(sample_audio, tmp_path / "chunks", chunk_seconds=300)

    assert chunks == [sample_audio]


def test_split_wav_cuts_on_sample_boundaries(tmp_path):
    long_file = write_wav(tmp_path / "long.wav", seconds=7.0, rate=16000)
    chunks = split_wav(long_file, tmp_path / "chunks", chunk_seconds=3)

    assert len(chunks) == 3
    with wave.open(str(chunks[0]), "rb") as first:
        assert first.getnframes() == 3 * 16000
    with wave.open(str(chunks[-1]), "rb") as last:
        assert last.getnframes() == 1 * 16000


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_prepare_converts_to_16k_mono_and_reports_duration(http_server, sample_audio, tmp_path):
    base_url, _ = http_server
    with Workspace("job-test") as work_dir:
        source, wav, chunks = prepare(f"{base_url}/sample.wav", work_dir, chunk_seconds=60)

        with wave.open(str(wav), "rb") as handle:
            assert handle.getframerate() == audio_service.SAMPLE_RATE
            assert handle.getnchannels() == 1

    assert source.sample_rate == 16000
    assert source.duration == pytest.approx(2.0, abs=0.05)
    assert chunks == [wav]


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_prepare_rejects_file_over_size_limit(http_server, sample_audio, monkeypatch, tmp_path):
    base_url, _ = http_server
    monkeypatch.setattr(settings, "audio_max_size_mb", 0)

    with Workspace("job-test") as work_dir, pytest.raises(AudioError, match="limit"):
        prepare(f"{base_url}/sample.wav", work_dir)


@pytest.mark.skipif(not audio_service.ffmpeg_available(), reason="ffmpeg niedostępny")
def test_workspace_is_removed_after_use():
    with Workspace("job-cleanup") as work_dir:
        path = Path(work_dir)
        (path / "file.bin").write_bytes(b"x")

    assert not path.exists()
