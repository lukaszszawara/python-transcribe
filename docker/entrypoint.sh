#!/usr/bin/env sh
# Zmienna WHISPER_WARMUP steruje tym, czy kontener pobiera wagę modelu przy starcie.
# Dla workera ustawiamy ją na 1 - wtedy pierwsze zlecenie nie czeka na download.
set -e

if [ "${WHISPER_WARMUP:-0}" = "1" ]; then
    echo "Pobieranie modelu whisper=${WHISPER_MODEL:-small} do ${WHISPER_MODEL_DIR:-/models}"
    python -m scripts.warmup
fi

case "$1" in
    api)
        exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${API_WORKERS:-1}" --proxy-headers
        ;;
    worker)
        exec celery -A app.celery_app.celery_app worker \
            --loglevel="${LOG_LEVEL:-info}" \
            --concurrency="${WORKER_CONCURRENCY:-1}" \
            --hostname="transcription-worker@%h"
        ;;
    beat)
        exec celery -A app.celery_app.celery_app beat --loglevel="${LOG_LEVEL:-info}"
        ;;
    warmup)
        exec python -m scripts.warmup
        ;;
    *)
        exec "$@"
        ;;
esac
