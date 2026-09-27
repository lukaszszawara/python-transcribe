#!/usr/bin/env bash
# Generuje audio testowe w katalogu tmp/e2e.
# Na macOS używa syntezatora głosu (prawdziwa mowa po polsku), wszędzie indziej
# ton 440 Hz wygenerowany przez ffmpeg - do testów samego potoku wystarczy.
set -euo pipefail

cd "$(dirname "$0")/.."

TARGET=${1:-tmp/e2e/wiadomosc.mp3}
SECONDS_LONG=${SECONDS_LONG:-6}
mkdir -p "$(dirname "$TARGET")"

TEXT="Dzień dobry. Witam w Polskim Radiu. Dzisiaj jest pięć stopni Celsjusza. Trzymajcie się ciepło. Zapowiadamy wiadomości sportowe i prognozę pogody na jutrzejszy dzień."

if [ "$(uname -s)" = "Darwin" ] && command -v say >/dev/null 2>&1; then
    VOICE=$(say -v '?' 2>/dev/null | awk '/pl_PL/ {print $1; exit}')
    LONGTEXT=""
    for _ in $(seq 1 "$SECONDS_LONG"); do
        LONGTEXT="$LONGTEXT $TEXT"
    done
    say -v "${VOICE:-Zosia}" -o tmp/e2e/raw.m4a --data-format=LEI16@22050 "$LONGTEXT"
    ffmpeg -hide_banner -loglevel error -y -i tmp/e2e/raw.m4a -ac 1 -ar 16000 -b:a 64k "$TARGET"
    rm -f tmp/e2e/raw.m4a
else
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "sine=frequency=440:duration=$((SECONDS_LONG * 12))" \
        -ac 1 -ar 16000 -b:a 64k "$TARGET"
fi

ffprobe -v error -show_entries format=duration -of csv=p=0 "$TARGET" | xargs -I{} echo "gotowe: $TARGET ({} s)"
