"""Model tabeli zadań transkrypcji."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (QUEUED, PROCESSING, COMPLETED, FAILED)
    TERMINAL = (COMPLETED, FAILED)


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    audio_url: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="pl")
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    initial_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobStatus.QUEUED, index=True
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_stage: Mapped[str | None] = mapped_column(String(48), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    vtt_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True)

    detected_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    audio_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    worker_host: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    @property
    def is_finished(self) -> bool:
        return self.status in JobStatus.TERMINAL
