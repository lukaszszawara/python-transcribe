"""Schematy request/response dla API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.config import settings
from app.models import JobStatus
from app.utils import to_iso_z

UrlStr = Annotated[str, Field(min_length=8, max_length=2048)]


def _validate_http_url(value: str, field: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field} musi zaczynać się od http:// lub https://")
    if not parsed.netloc:
        raise ValueError(f"{field} nie zawiera hosta")
    return value


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "audio_url": "https://static.prsa.pl/86623fd4-a991-43e2-841f-1f42e1ccb2bb.mp3",
                "language": "pl",
                "webhook_url": "https://your-app.com/webhook",
            }
        }
    )

    audio_url: UrlStr = Field(
        description="Publiczny URL pliku audio (MP3 lub inne formaty obsługiwane przez ffmpeg)."
    )
    language: str | None = Field(
        default=None,
        description=f"Kod języka ISO-639-1. Domyślnie: {settings.default_language}. "
        f"Dozwolone: {', '.join(settings.allowed_languages)}.",
    )
    webhook_url: UrlStr | None = Field(
        default=None,
        description="Adres, na który zostanie wysłany POST po zakończeniu transkrypcji.",
    )
    callback_delay_seconds: int | None = Field(
        default=None,
        ge=0,
        le=3600,
        description="Opóźnienie wysyłki webhooka (przydatne przy testach odbiorcy).",
    )
    initial_prompt: str | None = Field(
        default=None,
        max_length=1000,
        description="Kontekst dla Whispera - np. nazwa redakcji, typologia nazw własnych.",
    )

    @field_validator("audio_url")
    @classmethod
    def _check_audio_url(cls, value: str) -> str:
        return _validate_http_url(value, "audio_url")

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_http_url(value, "webhook_url")

    def resolved_language(self) -> str:
        return (self.language or settings.default_language).lower()


class TranscribeAccepted(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "6f1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d",
                "status": "queued",
                "message": "Transcription job created successfully",
            }
        }
    )

    job_id: str
    status: str = JobStatus.QUEUED
    message: str = "Transcription job created successfully"


class TranscriptionStatus(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "6f1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d",
                "status": "processing",
                "progress": 45,
            }
        }
    )

    job_id: str
    status: str
    progress: int
    stage: str | None = None
    language: str
    audio_url: str
    error: str | None = None
    word_count: int | None = None
    audio_duration: float | None = None
    processing_time: float | None = None
    detected_language: str | None = None
    model: str | None = None
    transcription: str | None = None
    vtt_content: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_serializer("created_at", "started_at", "completed_at")
    def _serialize_datetimes(self, value: datetime | None) -> str | None:
        return to_iso_z(value)

    @field_serializer("audio_duration", "processing_time")
    def _round_floats(self, value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    @field_serializer("transcription", "vtt_content")
    def _skip_empty_payloads(self, value: str | None, info) -> str | None:
        return value or None


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TranscriptionStatus]


class WebhookPayload(BaseModel):
    """Kontener budujący body wysyłane do webhooka."""

    event: str = "transcription.completed"
    job_id: str
    status: str
    language: str
    audio_url: str
    transcription: str | None
    vtt_content: str | None
    word_count: int | None
    audio_duration: float | None
    processing_time: float | None
    model: str | None
    detected_language: str | None
    error: str | None
    created_at: str | None
    completed_at: str | None
    metrics: dict[str, Any] | None = None
