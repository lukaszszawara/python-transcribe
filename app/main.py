"""Punkt wejścia aplikacji FastAPI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.routes import router
from app.config import settings
from app.db import init_db
from app.logging_config import configure_logging
from app.services import audio as audio_service

configure_logging()
logger = logging.getLogger("transcription.api")

DESCRIPTION = """
Usługa zamienia pliki audio na tekst oraz napisy WebVTT.

Transkrypcja działa asynchronicznie: `POST /api/v1/transcribe` zakłada zadanie
w kolejce i zwraca `job_id`, a wynik sprawdzasz w `GET /api/v1/transcribe/{job_id}`.
Po zakończeniu można też podać `webhook_url` - wtedy gotowy wynik i treść WebVTT
przyjdą POSTem na Twój adres.

Każde ukończone zadanie ląduje w `logs/transcription_metrics.log` w formacie
JSON Lines, niezależnie od tego, czy webhook został podany.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not audio_service.ffmpeg_available():
        logger.warning("ffmpeg nie jest w PATH - transkrypcja nie ruszy")
    logger.info(
        "%s v%s startuje (%s), model=%s, device=%s",
        settings.app_name,
        settings.app_version,
        settings.environment,
        settings.whisper_model,
        settings.whisper_device,
    )
    yield
    logger.info("%s zatrzymany", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Polskie Radio SA"},
    license_info={"name": "MIT"},
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("nieobsłużony błąd przy %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Błąd wewnętrzny serwera", "path": request.url.path},
    )
