"""Central application settings, loaded from environment variables and .env."""

import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Kolejność jest tu celowa - bez tego Whisper przyjmuje "auto" jako kod języka.
WHISPER_DEFAULT_LANGUAGES = (
    "pl",
    "en",
    "de",
    "fr",
    "es",
    "it",
    "ru",
    "uk",
    "cs",
    "sk",
    "sl",
    "lt",
    "lv",
    "et",
    "ro",
    "hu",
    "nl",
    "da",
    "sv",
    "no",
    "fi",
    "el",
    "tr",
    "id",
    "vi",
    "af",
    "ca",
    "hr",
    "sr",
    "bg",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Polskie Radio Transcription Service"
    app_version: str = "1.0.0"
    environment: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'transcription.db'}"
    sql_echo: bool = False

    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_broker_transport_options: dict = {}
    task_soft_time_limit: int = 21_600
    task_time_limit: int = 21_900
    worker_max_tasks_per_child: int = 200

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_model_dir: str = str(BASE_DIR / "models")
    whisper_force_single_thread: bool = False
    default_language: str = "pl"
    allowed_languages: tuple[str, ...] = WHISPER_DEFAULT_LANGUAGES
    word_timestamps: bool = True
    temperature: float = 0.0
    initial_prompt: str | None = None

    audio_chunk_seconds: int = 300
    audio_max_duration_seconds: int = 14_400
    audio_max_size_mb: int = 300
    download_timeout_seconds: int = 120
    download_connect_timeout_seconds: int = 15
    temp_dir: str = str(BASE_DIR / "tmp")
    allow_private_audio_urls: bool = True

    webhook_timeout_seconds: int = 10
    webhook_max_retries: int = 3
    webhook_backoff_seconds: float = 1.5
    webhook_secret: str | None = None

    metrics_log_path: str = str(BASE_DIR / "logs" / "transcription_metrics.log")
    webhook_log_path: str = str(BASE_DIR / "logs" / "webhook_deliveries.log")
    list_jobs_default_limit: int = 50
    list_jobs_max_limit: int = 200

    cors_origins: tuple[str, ...] = ()

    @field_validator("allowed_languages", mode="before")
    @classmethod
    def _split_languages(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("celery_broker_transport_options", mode="before")
    @classmethod
    def _parse_transport_options(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return json.loads(value)
        return value or {}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
