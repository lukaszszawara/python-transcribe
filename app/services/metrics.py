"""Zapis metryk zakończonych transkrypcji w formacie JSON Lines."""

from __future__ import annotations

import json
import logging
import os
import platform
import socket
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from app.config import settings
from app.utils import utcnow

logger = logging.getLogger("transcription.metrics")

REQUIRED_FIELDS = (
    "job_id",
    "timestamp",
    "processing_time",
    "audio_url",
    "language",
    "word_count",
    "status",
    "error",
)


def _runtime_info() -> dict[str, Any]:
    return {
        "model": settings.whisper_model,
        "device": settings.whisper_device,
        "worker_host": socket.gethostname(),
        "python": platform.python_version(),
    }


def build_metric(
    *,
    job_id: str,
    status: str,
    audio_url: str,
    language: str,
    word_count: int,
    processing_time: float,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "processing_time": round(processing_time, 3),
        "audio_url": audio_url,
        "language": language,
        "word_count": word_count,
        "status": status,
        "error": error,
        "service": settings.app_name,
        "environment": settings.environment,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    payload.update(_runtime_info())
    return payload


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    """Dopisuje pojedynczy obiekt JSON w jednej linii.

    flock chroni przed sytuacją, w której dwa workery skończą zadanie
    w tej samej milisekundzie i połowa linii zostanie nadpisana.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with open(target, "a", encoding="utf-8") as handle:
        flock(handle.fileno(), LOCK_EX)
        try:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            flock(handle.fileno(), LOCK_UN)


def log_transcription_metric(**kwargs: Any) -> dict[str, Any]:
    payload = build_metric(**kwargs)
    append_jsonl(settings.metrics_log_path, payload)
    logger.info(
        "job=%s status=%s words=%s processing_time=%.2fs",
        payload["job_id"],
        payload["status"],
        payload["word_count"],
        payload["processing_time"],
    )
    return payload


def read_metrics(limit: int = 100, job_id: str | None = None) -> list[dict[str, Any]]:
    target = Path(settings.metrics_log_path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if job_id and record.get("job_id") != job_id:
                continue
            rows.append(record)
    return rows[-limit:]


def summarize(limit: int = 1000) -> dict[str, Any]:
    records = read_metrics(limit=limit)
    completed = [item for item in records if item.get("status") == "completed"]
    failed = [item for item in records if item.get("status") == "failed"]
    times = [item.get("processing_time", 0.0) for item in completed]
    audio_seconds = [item.get("audio_duration") for item in completed if item.get("audio_duration")]
    total_audio = sum(audio_seconds) if audio_seconds else 0.0
    total_time = sum(times) if times else 0.0
    words = [item.get("word_count", 0) for item in completed]

    return {
        "jobs_logged": len(records),
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": round(len(completed) / len(records), 4) if records else None,
        "total_words": sum(words) if words else 0,
        "avg_processing_time": round(total_time / len(times), 3) if times else None,
        "avg_audio_duration": round(total_audio / len(audio_seconds), 3) if audio_seconds else None,
        "realtime_factor": round(total_time / total_audio, 3) if total_audio else None,
    }
