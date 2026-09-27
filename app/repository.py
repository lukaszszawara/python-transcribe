"""Operacje na tabeli zadań - jedno miejsce odpowiedzialne za zapis stanu."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JobStatus, TranscriptionJob
from app.utils import utcnow

logger = logging.getLogger("transcription.repository")


def create_job(
    db: Session,
    *,
    audio_url: str,
    language: str,
    webhook_url: str | None = None,
    initial_prompt: str | None = None,
    callback_delay_seconds: int = 0,
) -> TranscriptionJob:
    job = TranscriptionJob(
        id=str(uuid.uuid4()),
        audio_url=audio_url,
        language=language,
        webhook_url=webhook_url,
        initial_prompt=initial_prompt,
        callback_delay_seconds=callback_delay_seconds,
        status=JobStatus.QUEUED,
        progress=0,
        current_stage="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("utworzono zadanie %s (%s)", job.id, job.language)
    return job


def get_job(db: Session, job_id: str) -> TranscriptionJob | None:
    return db.get(TranscriptionJob, job_id)


def list_jobs(
    db: Session,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[TranscriptionJob]]:
    filters = []
    if status:
        filters.append(TranscriptionJob.status == status)

    total = db.scalar(select(func.count()).select_from(TranscriptionJob).where(*filters)) or 0
    statement = (
        select(TranscriptionJob)
        .where(*filters)
        .order_by(TranscriptionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return total, list(db.scalars(statement))


def claim_job(db: Session, job_id: str, worker_host: str | None = None) -> TranscriptionJob | None:
    """Oznacza zadanie jako przetwarzane. Zwraca None, jeśli ktoś już je wziął."""
    job = get_job(db, job_id)
    if job is None or job.status != JobStatus.QUEUED:
        return None
    job.status = JobStatus.PROCESSING
    job.started_at = utcnow()
    job.progress = 1
    job.current_stage = "downloading"
    job.worker_host = (worker_host or "worker")[:64]
    job.attempts += 1
    db.commit()
    db.refresh(job)
    return job


def update_job(db: Session, job_id: str, **fields: Any) -> TranscriptionJob | None:
    job = get_job(db, job_id)
    if job is None:
        return None
    for key, value in fields.items():
        if hasattr(job, key):
            setattr(job, key, value)
    job.updated_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


def set_progress(db: Session, job_id: str, progress: int, stage: str) -> None:
    update_job(
        db, job_id, progress=max(0, min(99, int(progress))), current_stage=stage
    )


def complete_job(
    db: Session,
    job_id: str,
    *,
    transcription: str,
    vtt_content: str,
    word_count: int,
    detected_language: str,
    audio_duration: float,
    processing_time: float,
    model_name: str,
) -> TranscriptionJob:
    return update_job(
        db,
        job_id,
        status=JobStatus.COMPLETED,
        progress=100,
        current_stage="completed",
        transcription=transcription,
        vtt_content=vtt_content,
        word_count=word_count,
        detected_language=detected_language,
        audio_duration=audio_duration,
        processing_time=round(processing_time, 3),
        model_name=model_name,
        error=None,
        completed_at=utcnow(),
    )


def fail_job(
    db: Session, job_id: str, *, error: str, processing_time: float
) -> TranscriptionJob:
    return update_job(
        db,
        job_id,
        status=JobStatus.FAILED,
        current_stage="failed",
        error=error[:4000],
        processing_time=round(processing_time, 3),
        completed_at=utcnow(),
    )


def attach_metrics(db: Session, job_id: str, payload: dict[str, Any]) -> None:
    update_job(db, job_id, metrics=json.dumps(payload, ensure_ascii=False, default=str))
