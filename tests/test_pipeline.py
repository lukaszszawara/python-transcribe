import json
import threading
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.models import JobStatus
from app.services import audio as audio_service
from app.services.transcriber import TranscriptResult
from app.services.vtt import VttSegment, VttWord

pytestmark = pytest.mark.skipif(
    not audio_service.ffmpeg_available(), reason="test potrzebuje ffmpeg"
)

SENTENCE = [
    VttWord(0.0, 0.6, "W"),
    VttWord(0.6, 1.1, "Polskim"),
    VttWord(1.1, 1.6, "Radiu"),
    VttWord(1.6, 2.2, "mówi"),
    VttWord(2.2, 2.9, "od"),
    VttWord(2.9, 3.6, "północy."),
]


class FakeTranscriber:
    """Zastępuje Whispera, żeby testy nie ciągnęły wag modelu."""

    def __init__(self, language: str = "pl"):
        self.is_loaded = True
        self.language = language
        self.calls: list[int] = []

    def load(self):
        return self

    def transcribe_chunks(self, chunks, language, *, chunk_seconds=0, initial_prompt=None, on_chunk=None):
        self.calls.append(len(chunks))
        if on_chunk:
            for index in range(len(chunks) + 1):
                on_chunk(index, len(chunks))
        return TranscriptResult(
            text="W Polskim Radiu mówi od północy.",
            language=self.language,
            segments=[VttSegment(0.0, 3.6, "W Polskim Radiu mówi od północy.", list(SENTENCE))],
            duration=3.6,
        )


class FakeFailedTranscriber(FakeTranscriber):
    def transcribe_chunks(self, *args, **kwargs):
        raise RuntimeError("uszkodzony plik modelu")


def write_wav(path: Path, seconds: float = 2.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def make_handler(received: list[dict]):
    class Handler(SimpleHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return

    return Handler


@pytest.fixture
def server(tmp_path):
    received: list[dict] = []
    handler = make_handler(received)
    factory = partial(handler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), factory)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield {"base": f"http://{host}:{port}", "received": received}
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def sample(tmp_path):
    return write_wav(tmp_path / "wiadomosc.wav", seconds=2.0)


@pytest.fixture
def run_inline(monkeypatch, fake_transcriber):
    from app import tasks

    monkeypatch.setattr(tasks, "get_transcriber", lambda: fake_transcriber)

    def _run(job_id: str):
        return tasks.transcribe_audio.apply(args=[job_id]).get()

    return _run


@pytest.fixture
def fake_transcriber():
    return FakeTranscriber()


@pytest.fixture
def client(monkeypatch, run_inline):
    def inline_delay(job_id: str):
        run_inline(job_id)
        return None

    monkeypatch.setattr(routes.transcribe_audio, "delay", inline_delay)
    with TestClient(app) as test_client:
        yield test_client


def test_full_pipeline_produces_transcription_and_vtt(client, fetch_job, server, sample, metrics_path):
    response = client.post(
        "/api/v1/transcribe",
        json={"audio_url": f"{server['base']}/wiadomosc.wav", "language": "pl"},
    )
    job_id = response.json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == JobStatus.COMPLETED
    assert body["progress"] == 100
    assert body["transcription"] == "W Polskim Radiu mówi od północy."
    assert body["word_count"] == 6
    assert body["vtt_content"].startswith("WEBVTT")
    assert "00:00:00.000 --> " in body["vtt_content"]
    assert body["detected_language"] == "pl"
    assert body["audio_duration"] == pytest.approx(2.0, abs=0.1)
    assert body["processing_time"] > 0

    job = fetch_job(job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.error is None


def test_metrics_log_written_after_success(client, metrics_path, server, sample):
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": f"{server['base']}/wiadomosc.wav"}
    ).json()["job_id"]

    lines = metrics_path.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])

    assert record["job_id"] == job_id
    assert record["status"] == "completed"
    assert record["language"] == "pl"
    assert record["word_count"] == 6
    assert record["error"] is None
    assert record["processing_time"] > 0
    assert record["timestamp"].endswith("Z")
    assert record["audio_url"].endswith("wiadomosc.wav")
    assert record["model"] == "small"


def test_webhook_receives_full_payload(client, server, sample, metrics_path):
    client.post(
        "/api/v1/transcribe",
        json={
            "audio_url": f"{server['base']}/wiadomosc.wav",
            "webhook_url": f"{server['base']}/webhook",
        },
    )

    assert len(server["received"]) == 1
    call = server["received"][0]
    body = call["body"]

    assert call["path"] == "/webhook"
    assert call["headers"]["x-webhook-event"] == "transcription.completed"
    assert body["status"] == "completed"
    assert body["transcription"] == "W Polskim Radiu mówi od północy."
    assert body["vtt_content"].startswith("WEBVTT")
    assert body["word_count"] == 6
    assert body["metrics"]["job_id"] == body["job_id"]


def test_webhook_is_not_called_when_url_missing(client, server, sample):
    client.post("/api/v1/transcribe", json={"audio_url": f"{server['base']}/wiadomosc.wav"})

    assert server["received"] == []


def test_metrics_logged_even_when_webhook_delivery_fails(client, server, sample, metrics_path, monkeypatch):
    from app.services import webhook as webhook_service

    def broken_deliver(url, payload, *, delay_seconds=0):
        raise RuntimeError("odbiornik webhooków nie odpowiada")

    monkeypatch.setattr(webhook_service, "deliver", broken_deliver)
    job_id = client.post(
        "/api/v1/transcribe",
        json={
            "audio_url": f"{server['base']}/wiadomosc.wav",
            "webhook_url": f"{server['base']}/webhook",
        },
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()
    record = json.loads(metrics_path.read_text(encoding="utf-8").strip().splitlines()[-1])

    assert body["status"] == "completed"
    assert record["status"] == "completed"
    assert record["has_webhook"] is True


def test_failed_job_is_marked_and_logged(client, fetch_job, server, sample, metrics_path, monkeypatch):
    from app import tasks

    monkeypatch.setattr(tasks, "get_transcriber", lambda: FakeFailedTranscriber())
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": f"{server['base']}/wiadomosc.wav"}
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()
    record = json.loads(metrics_path.read_text(encoding="utf-8").strip().splitlines()[-1])

    assert body["status"] == JobStatus.FAILED
    assert "uszkodzony plik modelu" in body["error"]
    assert body["completed_at"] is not None
    assert record["status"] == "failed"
    assert "uszkodzony plik modelu" in record["error"]
    assert record["word_count"] == 0


def test_unreachable_audio_marks_job_as_failed(client, fetch_job, metrics_path):
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": "http://127.0.0.1:1/niedostepny.mp3"}
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()
    record = json.loads(metrics_path.read_text(encoding="utf-8").strip().splitlines()[-1])

    assert body["status"] == JobStatus.FAILED
    assert body["error"]
    assert record["status"] == "failed"


def test_html_url_is_reported_as_audio_error(client, server, tmp_path):
    (tmp_path / "strona.html").write_text("<html>hello</html>", encoding="utf-8")
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": f"{server['base']}/strona.html"}
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == JobStatus.FAILED
    assert "audio" in body["error"].lower()


def test_long_audio_is_split_into_multiple_chunks(client, server, tmp_path, fake_transcriber):
    write_wav(tmp_path / "dlugi.wav", seconds=12.0)
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": f"{server['base']}/dlugi.wav"}
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == JobStatus.COMPLETED
    assert fake_transcriber.calls == [1]


def test_worker_cuts_file_into_chunks_based_on_setting(client, server, tmp_path, fake_transcriber, monkeypatch):
    from app.config import settings

    write_wav(tmp_path / "dlugi.wav", seconds=10.0)
    monkeypatch.setattr(settings, "audio_chunk_seconds", 4)
    job_id = client.post(
        "/api/v1/transcribe", json={"audio_url": f"{server['base']}/dlugi.wav"}
    ).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == "completed"
    assert fake_transcriber.calls == [3]
