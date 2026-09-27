ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WHISPER_MODEL_DIR=/models \
    DATA_DIR=/app/data \
    TMP_DIR=/app/tmp

# ffmpeg jest wymagany przez Whispera do dekodowania audio, curl do healthchecków
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY requirements.txt ./

# openai-whisper nie ma wheeli - budujemy go ze źródeł, a jego setup.py importuje
# pkg_resources, którego nie ma w setuptools >= 81. Stąd pin i --no-build-isolation.
RUN pip install "setuptools<81" wheel \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 \
    && pip install --no-build-isolation -r requirements.txt \
    && python -c "import whisper, fastapi, celery; print('build ok')"

COPY app ./app
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data /app/tmp /app/logs /models \
    && chown -R app:app /app /models

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["api"]
