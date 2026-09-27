"""Zadanie Celery realizujące transkrypcję od początku do końca."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.config import settings
from app.db import session_scope
from app.models import JobStatus
from app.repository import (
    attach_metrics,
    claim_job,
    complete_job,
    fail_job,
    get_job,
    set_progress,
    update_job,
)
from app.services import audio as audio_service
from app.services import metrics as metrics_service
from app.services import webhook as webhook_service
from app.services.transcriber import get_transcriber
from app.services.vtt import VttConfig, render_webvtt
from app.utils import strip_loops, to_iso_z, utcnow

logger = logging.getLogger("transcription.task")

DOWNLOAD_PROGRESS_END = 10
TRANSCRIBE_PROGRESS_START = 12
TRANSCRIBE_PROGRESS_END = 95


class TranscriptionTask(Task):
    """Wspólne ustawienia zadań transkrypcyjnych."""

    max_retries = 0
    ignore_result = False

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if not job_id:
            return
        logger.error("zadanie %s przerwane przez workera: %s", job_id, exc)
        with session_scope() as db:
            job = get_job(db, job_id)
            if job is None or job.is_finished:
                return
            elapsed = (utcnow() - job.started_at).total_seconds() if job.started_at else 0.0
            fail_job(db, job_id, error=f"Worker przerwał zadanie: {exc}", processing_time=elapsed)
        write_metrics(job_id)


def _progress_reporter(job_id: str, stage: str) -> Callable[[float], None]:
    def report(percent: float) -> None:
        with session_scope() as db:
            set_progress(db, job_id, percent, stage)

    return report


def _chunk_reporter(job_id: str, total_chunks: int) -> Callable[[int, int], None]:
    span = TRANSCRIBE_PROGRESS_END - TRANSCRIBE_PROGRESS_START

    def report(done: int, total: int) -> None:
        ratio = (done / total) if total else 1.0
        with session_scope() as db:
            set_progress(
                db,
                job_id,
                TRANSCRIBE_PROGRESS_START + ratio * span,
                f"transcribing ({done}/{total})" if total > 1 else "transcribing",
            )

    return report


def write_metrics(job_id: str) -> None:
    """Loguje metryki niezależnie od statusu zadania i webhooka."""
    with session_scope() as db:
        job = get_job(db, job_id)
        if job is None:
            return
        payload = metrics_service.log_transcription_metric(
            job_id=job.id,
            status=job.status,
            audio_url=job.audio_url,
            language=job.language,
            word_count=job.word_count or 0,
            processing_time=job.processing_time or 0.0,
            error=job.error,
            audio_duration=job.audio_duration,
            audio_bytes=job.audio_bytes,
            audio_format=job.audio_format,
            detected_language=job.detected_language,
            model=job.model_name,
            has_webhook=bool(job.webhook_url),
            worker_host=job.worker_host,
        )
        attach_metrics(db, job.id, payload)


def notify_webhook(job_id: str) -> None:
    with session_scope() as db:
        job = get_job(db, job_id)
        if job is None or not job.webhook_url:
            return
        payload = webhook_service.build_payload(job)
        url = job.webhook_url
        delay = job.callback_delay_seconds

    try:
        result = webhook_service.deliver(url, payload, delay_seconds=delay)
        logger.info(
            "webhook %s: delivered=%s attempts=%s", url, result["delivered"], result["attempts"]
        )
    except Exception:
        logger.exception("webhook do %s nie powiódł się", url)


def fail(job_id: str, error: str, processing_time: float) -> dict:
    with session_scope() as db:
        fail_job(db, job_id, error=error, processing_time=processing_time)
    write_metrics(job_id)
    notify_webhook(job_id)
    return {"job_id": job_id, "status": JobStatus.FAILED, "error": error}


@celery_app.task(bind=True, base=TranscriptionTask, name="app.tasks.transcribe_audio")
def transcribe_audio(self, job_id: str) -> dict:
    started = time.perf_counter()

    with session_scope() as db:
        job = claim_job(db, job_id, worker_host=socket.gethostname())
        if job is None:
            logger.warning("zadanie %s jest już przetwarzane albo nie istnieje", job_id)
            return {"job_id": job_id, "status": "skipped"}
        audio_url = job.audio_url
        language = job.language
        initial_prompt = job.initial_prompt

    try:
        transcriber = get_transcriber()
        transcriber.load()

        with audio_service.Workspace(job_id) as work_dir:
            source, chunks = _download(job_id, audio_url, work_dir)
            result = _transcribe(
                job_id,
                transcriber,
                chunks,
                language,
                initial_prompt=initial_prompt,
            )
            vtt = render_webvtt(
                result.segments,
                config=VttConfig(),
                notes=[
                    f"job_id: {job_id}",
                    f"language: {result.language}",
                    f"model: {settings.whisper_model}",
                    f"generated: {to_iso_z(utcnow())}",
                ],
            )
            text = strip_loops(result.text)

        processing_time = time.perf_counter() - started
        with session_scope() as db:
            complete_job(
                db,
                job_id,
                transcription=text,
                vtt_content=vtt,
                word_count=len(text.split()),
                detected_language=result.language,
                audio_duration=source.duration,
                processing_time=processing_time,
                model_name=result.model,
            )

        logger.info(
            "zadanie %s zakończone w %.1f s (%s s audio)", job_id, processing_time, source.duration
        )

    except SoftTimeLimitExceeded as exc:
        logger.warning("zadanie %s przekroczyło limit czasu", job_id)
        return fail(job_id, f"Przekroczono limit czasu zadania: {exc}", time.perf_counter() - started)
    except Exception as exc:
        logger.exception("zadanie %s nie powiodło się", job_id)
        return fail(job_id, f"{type(exc).__name__}: {exc}", time.perf_counter() - started)

    write_metrics(job_id)
    notify_webhook(job_id)
    return {
        "job_id": job_id,
        "status": JobStatus.COMPLETED,
        "word_count": len(text.split()),
        "processing_time": round(processing_time, 3),
    }


def _download(job_id: str, audio_url: str, work_dir: Path):
    source, _wav, chunks = audio_service.prepare(
        audio_url,
        work_dir,
        on_progress=_progress_reporter(job_id, "downloading"),
    )
    with session_scope() as db:
        update_job(
            db,
            job_id,
            audio_bytes=source.size_bytes,
            audio_duration=source.duration,
            audio_format=source.format_name,
            progress=DOWNLOAD_PROGRESS_END + 1,
            current_stage="transcribing",
        )
    return source, chunks


def _transcribe(job_id: str, transcriber, chunks: list[Path], language: str, *, initial_prompt):
    return transcriber.transcribe_chunks(
        chunks,
        language,
        chunk_seconds=settings.audio_chunk_seconds if len(chunks) > 1 else 0,
        initial_prompt=initial_prompt,
        on_chunk=_chunk_reporter(job_id, len(chunks)),
    )


@celery_app.task(name="app.tasks.healthcheck")
def healthcheck() -> dict:
    return {
        "status": "ok",
        "model": settings.whisper_model,
        "model_loaded": get_transcriber().is_loaded,
    }
