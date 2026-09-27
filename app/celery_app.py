"""Konfiguracja Celery."""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from app.config import settings

celery_app = Celery(
    "transcription",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=86_400,
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=settings.worker_max_tasks_per_child,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    task_soft_time_limit=settings.task_soft_time_limit,
    task_time_limit=settings.task_time_limit,
    task_default_queue="transcription",
    broker_connection_retry_on_startup=True,
    broker_transport_options=settings.celery_broker_transport_options
    or {"visibility_timeout": 86_400},
    worker_send_task_events=True,
)


@setup_logging.connect
def _configure_logging(**_kwargs) -> None:
    from app.logging_config import configure_logging

    configure_logging()
