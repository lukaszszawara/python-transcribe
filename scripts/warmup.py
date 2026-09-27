"""Pobiera wagi modelu Whispera do katalogu cache."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.config import settings
from app.logging_config import configure_logging


def main() -> int:
    configure_logging()
    logger = logging.getLogger("warmup")

    try:
        import whisper
    except ImportError:
        logger.error("Brak pakietu openai-whisper - sprawdź requirements.txt")
        return 1

    target = Path(settings.whisper_model_dir)
    target.mkdir(parents=True, exist_ok=True)
    logger.info("model=%s -> %s", settings.whisper_model, target)

    model = whisper.load_model(settings.whisper_model, device="cpu", download_root=str(target))
    del model
    logger.info("model gotowy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
