import os
import tempfile
from pathlib import Path

# Zmienne ustawiamy przed importem aplikacji, bo settings czyta je raz przy starcie.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="transcription_tests_"))

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_ROOT / 'test.db'}"
os.environ["METRICS_LOG_PATH"] = str(_TMP_ROOT / "transcription_metrics.log")
os.environ["WEBHOOK_LOG_PATH"] = str(_TMP_ROOT / "webhook_deliveries.log")
os.environ["TMP_DIR"] = str(_TMP_ROOT / "tmp")
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"
os.environ["WEBHOOK_MAX_RETRIES"] = "2"
os.environ["WEBHOOK_BACKOFF_SECONDS"] = "0"

import pytest

from app.db import init_db, session_scope
from app.models import TranscriptionJob
from app.repository import get_job


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    init_db()


@pytest.fixture(autouse=True)
def _clean_jobs():
    with session_scope() as db:
        for job in db.query(TranscriptionJob).all():
            db.delete(job)
    yield


@pytest.fixture
def db_session():
    with session_scope() as db:
        yield db


@pytest.fixture
def metrics_path() -> Path:
    path = Path(os.environ["METRICS_LOG_PATH"])
    if path.exists():
        path.unlink()
    return path


@pytest.fixture
def job_factory(db_session):
    def _factory(**kwargs):
        from app.repository import create_job

        payload = {"audio_url": "https://example.com/a.mp3", "language": "pl"}
        payload.update(kwargs)
        job = create_job(db_session, **payload)
        return job.id

    return _factory


@pytest.fixture
def fetch_job():
    def _fetch(job_id: str):
        with session_scope() as db:
            return get_job(db, job_id)

    return _fetch


@pytest.fixture
def patch_job():
    def _patch(job_id: str, **fields):
        with session_scope() as db:
            job = get_job(db, job_id)
            for key, value in fields.items():
                setattr(job, key, value)
            db.commit()
        return job_id

    return _patch
