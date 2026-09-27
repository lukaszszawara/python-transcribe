import json

from app.config import settings
from app.services import metrics as metrics_service
from app.services.metrics import (
    REQUIRED_FIELDS,
    append_jsonl,
    build_metric,
    read_metrics,
    summarize,
)


def test_metric_contains_all_required_fields(metrics_path):
    payload = build_metric(
        job_id="job-1",
        status="completed",
        audio_url="https://example.com/a.mp3",
        language="pl",
        word_count=42,
        processing_time=12.5,
    )

    for field in REQUIRED_FIELDS:
        assert field in payload
    assert payload["processing_time"] == 12.5
    assert payload["model"] == settings.whisper_model


def test_append_jsonl_writes_one_object_per_line(metrics_path):
    append_jsonl(metrics_path, {"job_id": "a", "status": "completed"})
    append_jsonl(metrics_path, {"job_id": "b", "status": "failed", "error": "boom"})

    lines = metrics_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["job_id"] == "a"
    assert json.loads(lines[1])["error"] == "boom"


def test_log_transcription_metric_persists_error_for_failed_job(metrics_path):
    metrics_service.log_transcription_metric(
        job_id="job-fail",
        status="failed",
        audio_url="https://example.com/a.mp3",
        language="pl",
        word_count=0,
        processing_time=1.0,
        error="HTTP 404",
    )

    records = read_metrics()
    assert records[-1]["status"] == "failed"
    assert records[-1]["error"] == "HTTP 404"


def test_read_metrics_filters_by_job_id(metrics_path):
    append_jsonl(metrics_path, {"job_id": "x", "status": "completed"})
    append_jsonl(metrics_path, {"job_id": "y", "status": "completed"})

    assert len(read_metrics(job_id="y")) == 1


def test_summary_ignores_failed_jobs_in_averages(metrics_path):
    append_jsonl(
        metrics_path,
        {"job_id": "1", "status": "completed", "processing_time": 10.0, "word_count": 100,
         "audio_duration": 60.0},
    )
    append_jsonl(
        metrics_path,
        {"job_id": "2", "status": "failed", "processing_time": 2.0, "word_count": 0, "error": "x"},
    )

    summary = summarize()
    assert summary["jobs_logged"] == 2
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["avg_processing_time"] == 10.0
    assert summary["success_rate"] == 0.5


def test_read_metrics_skips_broken_lines(metrics_path):
    metrics_path.write_text('{"job_id": "a"}\nnot json\n', encoding="utf-8")

    assert len(read_metrics()) == 1
