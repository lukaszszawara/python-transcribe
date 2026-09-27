#!/usr/bin/env bash
# Pełny przebieg lokalny bez Dockera: broker na filesystem, worker i API.
# Używa ffmpeg, modelu Whispera i lokalnego pliku audio z katalogu tmp/e2e.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-.venv/bin/python}
CELERY_BIN=${CELERY_BIN:-$(dirname "$PYTHON")/celery}
RUN_DIR=${RUN_DIR:-tmp/local-run}
AUDIO=${AUDIO:-tmp/e2e/wiadomosc.mp3}
PORT_API=${PORT_API:-8010}
PORT_AUDIO=${PORT_AUDIO:-8931}
PORT_WEBHOOK=${PORT_WEBHOOK:-8977}

export CELERY_BROKER_URL="filesystem://"
export CELERY_BROKER_TRANSPORT_OPTIONS='{"data_folder_in": "'"$PWD"'/'"$RUN_DIR"'/broker", "data_folder_out": "'"$PWD"'/'"$RUN_DIR"'/broker", "control_folder": "'"$PWD"'/'"$RUN_DIR"'/broker"}'
export CELERY_RESULT_BACKEND="file://$PWD/$RUN_DIR/results"
export DATABASE_URL="sqlite:///$PWD/$RUN_DIR/transcription.db"
export METRICS_LOG_PATH="$PWD/logs/transcription_metrics.log"
export WEBHOOK_LOG_PATH="$PWD/logs/webhook_deliveries.log"
export TMP_DIR="$PWD/tmp"
export ALLOW_PRIVATE_AUDIO_URLS=true
export LOG_LEVEL=INFO

mkdir -p "$RUN_DIR/broker" "$RUN_DIR/results" tmp/e2e

cleanup() {
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

if [ ! -f "$AUDIO" ]; then
    echo "Generuję brakujące audio testowe..."
    ./scripts/make_sample_audio.sh "$AUDIO"
fi

cd tmp/e2e && $OLDPWD/$PYTHON -m http.server "$PORT_AUDIO" >/dev/null 2>&1 &
PIDS+=($!)

$PYTHON -m scripts.webhook_sink "$PORT_WEBHOOK" > "$RUN_DIR/webhook_sink.log" 2>&1 &
PIDS+=($!)

$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT_API" > "$RUN_DIR/api.log" 2>&1 &
PIDS+=($!)

# Na macOS billiard startuje procesy potomne przez spawn i gubi wewnętrzne
# optymalizacje Celery (błąd "not enough values to unpack"), więc lokalnie
# używamy puli solo. W kontenerach działa zwykły prefork.
if [ "$(uname -s)" = "Darwin" ]; then
    POOL_ARGS=(--pool=solo)
else
    POOL_ARGS=(--concurrency="${WORKER_CONCURRENCY:-1}")
fi

"$CELERY_BIN" -A app.celery_app.celery_app worker --loglevel=INFO "${POOL_ARGS[@]}" > "$RUN_DIR/worker.log" 2>&1 &
PIDS+=($!)

echo "startuję API i worker, chwilka..."
for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$PORT_API/api/v1/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

curl -fsS "http://127.0.0.1:$PORT_API/api/v1/health" | $PYTHON -m json.tool

JOB_ID=$(curl -fsS -X POST "http://127.0.0.1:$PORT_API/api/v1/transcribe" \
    -H 'Content-Type: application/json' \
    -d "{\"audio_url\": \"http://127.0.0.1:$PORT_AUDIO/$(basename "$AUDIO")\", \"language\": \"pl\", \"webhook_url\": \"http://127.0.0.1:$PORT_WEBHOOK/webhook\"}" \
    | $PYTHON -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

echo "job_id=$JOB_ID"

for _ in $(seq 1 90); do
    STATUS=$(curl -fsS "http://127.0.0.1:$PORT_API/api/v1/transcribe/$JOB_ID")
    STATE=$(echo "$STATUS" | $PYTHON -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("progress"))')
    echo "  $STATE"
    case "$STATE" in
        completed*|failed*) break ;;
    esac
    sleep 2
done

echo
echo "--- wynik ---"
curl -fsS "http://127.0.0.1:$PORT_API/api/v1/transcribe/$JOB_ID" | $PYTHON -c '
import json, sys
data = json.load(sys.stdin)
print("status:", data["status"], "| słów:", data.get("word_count"), "| czas:", data.get("processing_time"), "s")
print("transkrypcja:", (data.get("transcription") or "")[:300])
print("vtt:", (data.get("vtt_content") or "")[:200].replace("\n", " | "))
'

echo
echo "--- ostatnia linia metryk ---"
tail -n 1 logs/transcription_metrics.log

echo
echo "--- odbiór webhooka ---"
cat "$RUN_DIR/webhook_sink.log"

echo
echo "logi: $RUN_DIR/{api,worker}.log"
