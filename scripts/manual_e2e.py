"""Ręczny test end-to-end na prawdziwym audio (uruchamiany poza pytestem).

Używa modelu Whispera lokalnie, bez Redisa - sprawdza sam pipeline:
pobranie pliku -> ffmpeg -> transkrypcja -> WebVTT -> metryki.
"""

from __future__ import annotations

import json
import sys
import time

from app.config import settings
from app.logging_config import configure_logging
from app.services import metrics as metrics_service
from app.services.transcriber import get_transcriber
from app.services.vtt import VttConfig, render_webvtt
from app.utils import strip_loops


def main(url: str) -> int:
    configure_logging()
    from app.services import audio as audio_service

    started = time.perf_counter()
    with audio_service.Workspace("manual-e2e") as work_dir:
        source, _wav, chunks = audio_service.prepare(url, work_dir, chunk_seconds=300)
        print(f"audio: {source.duration:.1f} s, {source.size_bytes} B, chunków: {len(chunks)}")

        result = get_transcriber().transcribe_chunks(
            chunks,
            "pl",
            chunk_seconds=settings.audio_chunk_seconds if len(chunks) > 1 else 0,
        )
        vtt = render_webvtt(result.segments, config=VttConfig(), notes=["job_id: manual-e2e"])

    text = strip_loops(result.text)
    metrics_service.log_transcription_metric(
        job_id="manual-e2e",
        status="completed",
        audio_url=url,
        language=result.language,
        word_count=len(text.split()),
        processing_time=time.perf_counter() - started,
        audio_duration=source.duration,
    )

    print("\n--- transkrypcja ---")
    print(text)
    print("\n--- WebVTT ---")
    print(vtt)
    print(f"\n--- metryki ({settings.metrics_log_path}) ---")
    print(json.dumps(metrics_service.read_metrics(limit=1)[-1], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("użycie: python -m scripts.manual_e2e <audio_url>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
