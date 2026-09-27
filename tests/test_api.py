import json

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.models import JobStatus

SAMPLE_URL = "https://static.prsa.pl/86623fd4-a991-43e2-841f-1f42e1ccb2bb.mp3"


@pytest.fixture
def client(monkeypatch):
    sent: list[str] = []

    def fake_delay(job_id: str):
        sent.append(job_id)
        return None

    monkeypatch.setattr(routes.transcribe_audio, "delay", fake_delay)
    with TestClient(app) as test_client:
        test_client.sent_jobs = sent
        yield test_client


def test_create_job_returns_queued_acceptance(client):
    response = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["message"] == "Transcription job created successfully"
    assert len(body["job_id"]) == 36
    assert client.sent_jobs == [body["job_id"]]


def test_language_defaults_to_polish(client, fetch_job):
    response = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL})

    job = fetch_job(response.json()["job_id"])
    assert job.language == "pl"


def test_language_is_taken_from_payload(client, fetch_job):
    response = client.post(
        "/api/v1/transcribe", json={"audio_url": SAMPLE_URL, "language": "en"}
    )

    job = fetch_job(response.json()["job_id"])
    assert job.language == "en"


def test_unsupported_language_is_rejected(client):
    response = client.post(
        "/api/v1/transcribe", json={"audio_url": SAMPLE_URL, "language": "xx"}
    )

    assert response.status_code == 400
    assert "nie jest obsługiwany" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"audio_url": "ftp://example.com/a.mp3"},
        {"audio_url": "not-a-url"},
        {"audio_url": SAMPLE_URL, "webhook_url": "javascript:alert(1)"},
    ],
)
def test_invalid_urls_are_rejected(client, payload):
    assert client.post("/api/v1/transcribe", json=payload).status_code == 422


def test_missing_audio_url_is_rejected(client):
    assert client.post("/api/v1/transcribe", json={"language": "pl"}).status_code == 422


def test_status_of_queued_job(client):
    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == JobStatus.QUEUED
    assert body["progress"] == 0
    assert body["transcription"] is None
    assert body["created_at"].endswith("Z")


def test_status_of_unknown_job(client):
    response = client.get("/api/v1/transcribe/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert "nie istnieje" in response.json()["detail"]


def test_status_does_not_leak_payload_before_completion(client, patch_job):
    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]
    patch_job(job_id, transcription="wynik", vtt_content="WEBVTT")

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["transcription"] is None
    assert body["vtt_content"] is None


def test_completed_job_returns_full_payload(client, patch_job):
    from app.utils import utcnow

    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]
    patch_job(
        job_id,
        status=JobStatus.COMPLETED,
        progress=100,
        transcription="Dzień dobry",
        vtt_content="WEBVTT\n",
        word_count=2,
        completed_at=utcnow(),
    )

    body = client.get(f"/api/v1/transcribe/{job_id}").json()

    assert body["status"] == "completed"
    assert body["progress"] == 100
    assert body["transcription"] == "Dzień dobry"
    assert body["vtt_content"].startswith("WEBVTT")
    assert body["word_count"] == 2
    assert body["completed_at"].endswith("Z")


def test_vtt_download(client, patch_job):
    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]
    patch_job(job_id, status=JobStatus.COMPLETED, vtt_content="WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nTekst\n")

    response = client.get(f"/api/v1/transcribe/{job_id}/vtt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vtt")
    assert f"transcription_{job_id}.vtt" in response.headers["content-disposition"]


def test_vtt_download_conflicts_while_job_runs(client):
    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]

    response = client.get(f"/api/v1/transcribe/{job_id}/vtt")

    assert response.status_code == 409


def test_list_jobs(client):
    for _ in range(3):
        client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL})

    body = client.get("/api/v1/transcribe?limit=2").json()

    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["limit"] == 2


def test_list_jobs_filters_by_status(client):
    client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL})

    assert client.get("/api/v1/transcribe?status=completed").json()["total"] == 0
    assert client.get("/api/v1/transcribe?status=queued").json()["total"] == 1


def test_list_jobs_rejects_unknown_status(client):
    assert client.get("/api/v1/transcribe?status=nope").status_code == 400


def test_health_reports_dependencies(client):
    body = client.get("/api/v1/health").json()

    assert body["status"] in ("ok", "degraded")
    assert body["checks"]["model"] == "small"
    assert "broker" in body["checks"]
    assert body["checks"]["database"] == "ok"


def test_metrics_endpoint_returns_summary(client, metrics_path):
    from app.services import metrics as metrics_service

    metrics_service.append_jsonl(
        metrics_path, {"job_id": "1", "status": "completed", "processing_time": 5.0, "word_count": 10}
    )

    body = client.get("/api/v1/metrics").json()

    assert body["summary"]["completed"] == 1
    assert body["recent"][0]["job_id"] == "1"


def test_openapi_documents_both_endpoints(client):
    schema = client.get("/openapi.json").json()

    assert "/api/v1/transcribe" in schema["paths"]
    assert "/api/v1/transcribe/{job_id}" in schema["paths"]
    assert schema["info"]["title"] == "Polskie Radio Transcription Service"


def test_docs_are_served(client):
    assert client.get("/docs").status_code == 200


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/docs"


def test_job_metrics_endpoint(client, fetch_job):
    job_id = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL}).json()["job_id"]

    body = client.get(f"/api/v1/transcribe/{job_id}/metrics").json()

    assert body["job_id"] == job_id
    assert body["status"] == JobStatus.QUEUED
    assert json.dumps(body)


def test_api_returns_503_when_queue_is_down(client, monkeypatch):
    def broken_delay(job_id: str):
        raise ConnectionError("broker niedostępny")

    monkeypatch.setattr(routes.transcribe_audio, "delay", broken_delay)

    response = client.post("/api/v1/transcribe", json={"audio_url": SAMPLE_URL})

    assert response.status_code == 503
    assert "kolejka" in response.json()["detail"].lower()
