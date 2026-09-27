"""Wysyłka webhooków po zakończeniu transkrypcji."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.models import TranscriptionJob
from app.schemas import WebhookPayload
from app.services import metrics as metrics_service

logger = logging.getLogger("transcription.webhook")


def _signature(body: bytes) -> str | None:
    if not settings.webhook_secret:
        return None
    digest = hmac.new(
        settings.webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def build_payload(job: TranscriptionJob) -> WebhookPayload:
    return WebhookPayload(
        job_id=job.id,
        status=job.status,
        language=job.language,
        audio_url=job.audio_url,
        transcription=job.transcription,
        vtt_content=job.vtt_content,
        word_count=job.word_count,
        audio_duration=job.audio_duration,
        processing_time=job.processing_time,
        model=job.model_name,
        detected_language=job.detected_language,
        error=job.error,
        created_at=job.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if job.created_at else None,
        completed_at=(
            job.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if job.completed_at else None
        ),
        metrics=json.loads(job.metrics) if job.metrics else None,
    )


def deliver(
    url: str,
    payload: WebhookPayload,
    *,
    delay_seconds: int = 0,
) -> dict[str, Any]:
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    body = payload.model_dump_json().encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"{settings.app_name}/{settings.app_version}",
        "X-Job-Id": payload.job_id,
        "X-Webhook-Event": payload.event,
    }
    signature = _signature(body)
    if signature:
        headers["X-Webhook-Signature"] = signature

    attempts = max(1, settings.webhook_max_retries)
    result: dict[str, Any] = {"delivered": False, "attempts": 0, "status_code": None}

    with httpx.Client(timeout=settings.webhook_timeout_seconds) as client:
        for attempt in range(1, attempts + 1):
            result["attempts"] = attempt
            try:
                response = client.post(url, content=body, headers=headers)
                result["status_code"] = response.status_code
                if response.status_code < 400:
                    result["delivered"] = True
                    result["error"] = None
                    break
                result["error"] = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("webhook %s próba %s/%s: %s", url, attempt, attempts, result["error"])

            if attempt < attempts:
                time.sleep(settings.webhook_backoff_seconds * (2 ** (attempt - 1)))

    record = {
        "timestamp": payload.completed_at,
        "job_id": payload.job_id,
        "webhook_url": url,
        "event": payload.event,
        "delivered": result["delivered"],
        "attempts": result["attempts"],
        "status_code": result["status_code"],
        "error": result.get("error"),
    }
    metrics_service.append_jsonl(settings.webhook_log_path, record)
    return result
