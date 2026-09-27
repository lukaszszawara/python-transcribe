"""Konfiguracja logowania wspólna dla API i workerów."""

from __future__ import annotations

import logging
import logging.config
import warnings

from app.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _silence_noisy_warnings() -> None:
    # Whisper na CPU krzyczy o fp16, a torch o torch.load przy każdym starcie workera.
    # Oba ostrzeżenia są szumem w logach kolejki.
    warnings.filterwarnings("ignore", message=r".*FP16 is not supported on CPU.*")
    warnings.filterwarnings("ignore", category=FutureWarning, message=r".*torch\.load.*")


def configure_logging(level: str | None = None) -> None:
    _silence_noisy_warnings()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": LOG_FORMAT, "datefmt": DATE_FORMAT},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {
                "handlers": ["console"],
                "level": (level or settings.log_level).upper(),
            },
            "loggers": {
                "celery": {"level": "INFO", "propagate": True, "handlers": []},
                "httpx": {"level": "WARNING", "propagate": True},
                "httpcore": {"level": "WARNING", "propagate": True},
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
                "numba": {"level": "WARNING", "propagate": True},
                "whisper": {"level": "WARNING", "propagate": True},
            },
        }
    )
