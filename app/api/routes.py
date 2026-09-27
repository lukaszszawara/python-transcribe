"""Warstwa HTTP - endpointy zgodne ze specyfikacją zadania."""

from __future__ import annotations

import json
import logging
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.db import get_db
from app.models import JobStatus, TranscriptionJob
from app.repository import create_job, get_job, list_jobs
from app.schemas import (
    JobListResponse,
    TranscribeAccepted,
    TranscribeRequest,
    TranscriptionStatus,
)
from app.services import audio as audio_service
from app.services import metrics as metrics_service
from app.tasks import transcribe_audio

logger = logging.getLogger("transcription.api")

router = APIRouter(tags=["transcription"])


def to_status(job: TranscriptionJob, *, include_payload: bool = False) -> TranscriptionStatus:
    return TranscriptionStatus(
        job_id=job.id,
        status=job.status,
        progress=job.progress,
        stage=job.current_stage,
        language=job.language,
        audio_url=job.audio_url,
        error=job.error,
        word_count=job.word_count,
        audio_duration=job.audio_duration,
        processing_time=job.processing_time,
        detected_language=job.detected_language,
        model=job.model_name,
        transcription=job.transcription if include_payload else None,
        vtt_content=job.vtt_content if include_payload else None,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@router.post(
    "/api/v1/transcribe",
    response_model=TranscribeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Zleć transkrypcję pliku audio",
)
def create_transcription(
    payload: TranscribeRequest,
    db: Session = Depends(get_db),
) -> TranscribeAccepted:
    language = payload.resolved_language()
    if language not in settings.allowed_languages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Język '{language}' nie jest obsługiwany. "
                f"Dozwolone: {', '.join(settings.allowed_languages)}."
            ),
        )

    job = create_job(
        db,
        audio_url=payload.audio_url,
        language=language,
        webhook_url=payload.webhook_url,
        initial_prompt=payload.initial_prompt,
        callback_delay_seconds=payload.callback_delay_seconds or 0,
    )

    try:
        transcribe_audio.delay(job.id)
    except Exception as exc:  # broker padł - zadanie zostaje w kolejce po restarcie workera
        logger.exception("nie udało się wysłać zadania %s do kolejki", job.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Kolejka zadań jest niedostępna: {exc}",
        ) from exc

    return TranscribeAccepted(job_id=job.id, status=JobStatus.QUEUED)


@router.get(
    "/api/v1/transcribe/{job_id}",
    response_model=TranscriptionStatus,
    summary="Sprawdź status transkrypcji",
)
def read_transcription(job_id: str, db: Session = Depends(get_db)) -> TranscriptionStatus:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Zadanie {job_id} nie istnieje"
        )

    if job.status == JobStatus.PROCESSING:
        # stan z brokera bywa świeższy niż zapis w bazie przy twardym przerwaniu pracy
        async_result = AsyncResult(job.id, app=celery_app)
        state = async_result.state
        if state == "PROGRESS" and isinstance(async_result.info, dict):
            job.progress = int(async_result.info.get("progress", job.progress))
            job.current_stage = async_result.info.get("stage", job.current_stage)

    return to_status(job, include_payload=job.status == JobStatus.COMPLETED)


@router.get(
    "/api/v1/transcribe",
    response_model=JobListResponse,
    summary="Lista zadań transkrypcji",
)
def list_transcriptions(
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"One z: {', '.join(JobStatus.ALL)}",
    ),
    limit: int = Query(default=None, ge=1, le=settings.list_jobs_max_limit),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> JobListResponse:
    if status_filter and status_filter not in JobStatus.ALL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Nieznany status '{status_filter}'",
        )
    effective_limit = min(limit or settings.list_jobs_default_limit, settings.list_jobs_max_limit)
    total, jobs = list_jobs(db, status=status_filter, limit=effective_limit, offset=offset)
    return JobListResponse(
        total=total,
        limit=effective_limit,
        offset=offset,
        items=[to_status(job) for job in jobs],
    )


@router.get(
    "/api/v1/transcribe/{job_id}/vtt",
    response_class=PlainTextResponse,
    summary="Pobierz gotowy plik WebVTT",
)
def download_vtt(job_id: str, db: Session = Depends(get_db)) -> Response:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Zadanie {job_id} nie istnieje"
        )
    if job.status != JobStatus.COMPLETED or not job.vtt_content:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Zadanie {job_id} nie ma jeszcze pliku VTT (status: {job.status})",
        )
    return Response(
        content=job.vtt_content,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transcription_{job_id}.vtt"'},
    )


@router.get(
    "/api/v1/transcribe/{job_id}/metrics",
    summary="Metryki pojedynczego zadania",
)
def read_job_metrics(job_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    job = get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Zadanie {job_id} nie istnieje"
        )
    return {
        "job_id": job.id,
        "status": job.status,
        "word_count": job.word_count,
        "audio_duration": job.audio_duration,
        "processing_time": job.processing_time,
        "model": job.model_name,
        "worker_host": job.worker_host,
        "logged_metrics": json.loads(job.metrics) if job.metrics else None,
    }


@router.get("/api/v1/metrics", summary="Podsumowanie metryk z pliku logów")
def read_metrics_summary(limit: int = Query(default=1000, ge=1, le=10_000)) -> dict[str, Any]:
    return {
        "log_file": settings.metrics_log_path,
        "summary": metrics_service.summarize(limit=limit),
        "recent": metrics_service.read_metrics(limit=min(limit, 20)),
    }


@router.get("/api/v1/health", summary="Stan usługi i zależności")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    try:
        connection = celery_app.connection()
        try:
            connection.ensure_connection(max_retries=0, timeout=3)
        finally:
            connection.release()
        checks["broker"] = "ok"
    except Exception as exc:
        checks["broker"] = f"error: {exc}"

    checks["ffmpeg"] = "ok" if audio_service.ffmpeg_available() else "missing"
    checks["model"] = settings.whisper_model

    healthy = all(
        value == "ok" for key, value in checks.items() if key in ("database", "broker", "ffmpeg")
    )
    return {
        "status": "ok" if healthy else "degraded",
        "version": settings.app_version,
        "environment": settings.environment,
        "broker": settings.celery_broker_url.split("@")[-1],
        "checks": checks,
    }
