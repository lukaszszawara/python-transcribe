# Usługa transkrypcji STT - Polskie Radio SA

Serwis, który pobiera plik audio z URL, transkrybuje go modelem **OpenAI Whisper (small)**
i zwraca tekst oraz napisy w formacie **WebVTT**. Przetwarzanie jest asynchroniczne -
API przyjmuje zlecenie i natychmiast odpowiada `job_id`, a właściwa robota dzieje się
w kolejce Celery.

```
POST /api/v1/transcribe  ->  202 + job_id
        |
        v
   Redis (kolejka)  ->  worker Celery  ->  pobieranie -> ffmpeg -> Whisper -> WebVTT
                                                        |                        |
                                                  logs/transcription_metrics.log   webhook
```

## Spis treści

- [Uruchomienie w Dockerze](#uruchomienie-w-dockerze)
- [Wywołanie API](#wywołanie-api)
- [Webhook](#webhook)
- [Log metryk](#log-metryk)
- [Uruchomienie bez Dockera](#uruchomienie-bez-dockera)
- [Testy](#testy)
- [Konfiguracja](#konfiguracja)
- [Struktura projektu](#struktura-projektu)
- [Jak to działa](#jak-to-działa)
- [Dobre praktyki i ograniczenia](#dobre-praktyki-i-ograniczenia)

## Uruchomienie w Dockerze

Wymagany Docker z Compose v2. Reszta (ffmpeg, Redis, model) dostaje się w obrazie.

```bash
cp .env.example .env      # opcjonalnie, wartości domyślne działają
docker compose up --build
```

Pierwszy start workera pobiera wagi modelu `small` (~460 MB) do wolumenu `model-cache`,
więc trwa to kilka minut. API i Redis są gotowe szybciej - sprawdzenie stanu:

```bash
curl http://localhost:8000/api/v1/health
```

```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "development",
  "broker": "redis:6379/0",
  "checks": {"database": "ok", "broker": "ok", "ffmpeg": "ok", "model": "small"}
}
```

Dokumentacja API (Swagger): <http://localhost:8000/docs>

Zatrzymanie i sprzątanie:

```bash
docker compose down          # zostawia wagi modelu i logi
docker compose down -v       # usuwa też wolumeny (model trzeba pobrać od nowa)
```

## Wywołanie API

### 1. Zlecenie transkrypcji

```bash
curl -X POST http://localhost:8000/api/v1/transcribe \
  -H 'Content-Type: application/json' \
  -d '{
        "audio_url": "https://static.prsa.pl/86623fd4-a991-43e2-841f-1f42e1ccb2bb.mp3",
        "language": "pl",
        "webhook_url": "https://your-app.com/webhook"
      }'
```

Odpowiedź `202 Accepted`:

```json
{
  "job_id": "9d3a1f0c-1d2e-4f3a-9c8b-2a1b3c4d5e6f",
  "status": "queued",
  "message": "Transcription job created successfully"
}
```

| Pole | Wymagane | Opis |
| --- | --- | --- |
| `audio_url` | tak | Publiczny URL z plikiem audio (MP3, WAV, M4A, FLAC, OGG) |
| `language` | nie | Kod ISO-639-1, domyślnie `pl` |
| `webhook_url` | nie | Adres dla powiadomienia POST po zakończeniu |
| `initial_prompt` | nie | Kontekst dla Whispera, np. `Polskie Radio, spiker, Warszawa` |
| `callback_delay_seconds` | nie | Opóźnienie webhooka, przydatne przy testach |

### 2. Odpytywanie statusu

```bash
curl http://localhost:8000/api/v1/transcribe/9d3a1f0c-1d2e-4f3a-9c8b-2a1b3c4d5e6f
```

W trakcie:

```json
{
  "job_id": "9d3a1f0c-1d2e-4f3a-9c8b-2a1b3c4d5e6f",
  "status": "processing",
  "progress": 45,
  "stage": "transcribing (2/4)"
}
```

Po zakończeniu (pełny wynik):

```json
{
  "job_id": "9d3a1f0c-1d2e-4f3a-9c8b-2a1b3c4d5e6f",
  "status": "completed",
  "progress": 100,
  "transcription": "Dzień dobry, witam w Polskim Radiu...",
  "vtt_content": "WEBVTT\n\nNOTE\njob_id: 9d3a...\n\n1\n00:00:00.000 --> 00:00:04.880\nDzień dobry, witam w Polskim Radiu,\n",
  "word_count": 412,
  "audio_duration": 183.4,
  "processing_time": 47.9,
  "detected_language": "pl",
  "model": "small",
  "created_at": "2024-01-15T10:30:00Z",
  "completed_at": "2024-01-15T10:32:15Z"
}
```

Statusy: `queued` → `processing` → `completed` albo `failed`
(w razie porażki pole `error` zawiera przyczynę).

### Pozostałe endpointy

| Metoda | Ścieżka | Do czego |
| --- | --- | --- |
| `GET` | `/api/v1/transcribe?status=completed&limit=50` | Lista zadań z filtrami |
| `GET` | `/api/v1/transcribe/{job_id}/vtt` | Plik WebVTT jako załącznik |
| `GET` | `/api/v1/transcribe/{job_id}/metrics` | Metryki jednego zadania |
| `GET` | `/api/v1/metrics?limit=1000` | Podsumowanie z pliku logów |
| `GET` | `/api/v1/health` | Stan bazy, brokera i ffmpeg |

## Webhook

Po zakończeniu zadania (sukces **i** porażka) worker wysyła `POST` na podany adres:

```json
{
  "event": "transcription.completed",
  "job_id": "9d3a1f0c-1d2e-4f3a-9c8b-2a1b3c4d5e6f",
  "status": "completed",
  "language": "pl",
  "audio_url": "https://static.prsa.pl/86623fd4-a991-43e2-841f-1f42e1ccb2bb.mp3",
  "transcription": "Dzień dobry, witam w Polskim Radiu...",
  "vtt_content": "WEBVTT\n\n1\n00:00:00.000 --> ...",
  "word_count": 412,
  "audio_duration": 183.4,
  "processing_time": 47.9,
  "model": "small",
  "detected_language": "pl",
  "error": null,
  "created_at": "2024-01-15T10:30:00Z",
  "completed_at": "2024-01-15T10:32:15Z",
  "metrics": {"job_id": "...", "status": "completed", "...": "..."}
}
```

Nagłówki: `X-Job-Id`, `X-Webhook-Event: transcription.completed`, a przy ustawionym
`WEBHOOK_SECRET` także `X-Webhook-Signature: sha256=<hmac>` (HMAC-SHA256 z body).

Dostawa ma `WEBHOOK_MAX_RETRIES` prób z wykładniczym odstępem. Niepowodzenia nie blokują
zadania - każda próba ląduje w `logs/webhook_deliveries.log`.

Do lokalnych testów jest gotowy odbiornik:

```bash
python -m scripts.webhook_sink 8977     # zapis do logs/webhook_received.jsonl
```

## Log metryk

Po **każdym** zakończeniu zadania, niezależnie od webhooka, dopisywana jest jedna linia
JSON do `logs/transcription_metrics.log`:

```json
{"job_id":"9d3a1f0c-...","timestamp":"2024-01-15T10:32:15.412Z","processing_time":47.912,"audio_url":"https://static.prsa.pl/...mp3","language":"pl","word_count":412,"status":"completed","error":null,"service":"Polskie Radio Transcription Service","environment":"development","audio_duration":183.4,"audio_bytes":2914112,"audio_format":"mp3","detected_language":"pl","model":"small","device":"cpu","has_webhook":true,"worker_host":"pr-worker-1","python":"3.11.15"}
```

Wymagane pola to `job_id`, `timestamp`, `processing_time`, `audio_url`, `language`,
`word_count`, `status`, `error`; reszta to informacje dodatkowe. Zapis idzie pod `flock`,
więc kilka workerów może dopisywać równolegle, a `tail -f` czy `jq` czytają plik w locie:

```bash
tail -f logs/transcription_metrics.log | jq -r '[.job_id, .status, .processing_time] | @tsv'
jq -s 'group_by(.status) | map({status: .[0].status, count: length})' logs/transcription_metrics.log
```

## Uruchomienie bez Dockera

Potrzebne: Python 3.11+, `ffmpeg`, Redis (albo dowolny broker obsługiwany przez Kombu).

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # po instalacji trzeba mieć "setuptools<81", patrz niżej

docker run -d --name redis -p 6379:6379 redis:7-alpine   # albo lokalny redis-server
./scripts/local_run.sh                 # podnosi API + workera i robi pełny przebieg
```

`scripts/local_run.sh` uruchamia serwer plików dla audio, odbiornik webhooków, API
i workera, wysyła zlecenie, odpytuje status i wypisuje wynik, metrykę oraz webhook.
Skrypt używa brokera na katalogu (`filesystem://`), więc Redis nie jest tu konieczny.

> `openai-whisper` nie ma gotowych wheeli, a jego `setup.py` importuje `pkg_resources`,
> które zniknęło w `setuptools>=81`. Dlatego w `Dockerfile` jest pin `setuptools<81`
> i instalacja `--no-build-isolation`. Lokalnie: `pip install "setuptools<81" wheel`
> przed instalacją albo `pip install --no-build-isolation -r requirements.txt`.

Na macOS worker musi pracować z `--pool=solo` (billiard startuje potomków przez `spawn`
i gubi optymalizacje Celery). Skrypt robi to automatycznie; w kontenerach działa zwykły
prefork.

Pojedynczy przebieg pipeline'u bez API i kolejki - wygodne do testowania samego Whispera:

```bash
./scripts/make_sample_audio.sh        # audio testowe w tmp/e2e/wiadomosc.mp3
python -m scripts.manual_e2e http://127.0.0.1:8931/wiadomosc.mp3
```

## Testy

```bash
pip install -r requirements-dev.txt
pytest                                  # 79 testów, bez pobierania wag modelu
pytest --cov=app --cov-report=term-missing
ruff check app tests scripts            # lint (konfiguracja w ruff.toml)
```

Testy nie ładują Whispera - model w taskach jest podmieniany na atrapę, dzięki czemu
zestaw uruchamia się w kilka sekund. Testy ffmpegowe (`test_audio.py`, `test_pipeline.py`)
same się pomijają, gdy w systemie nie ma `ffmpeg`. Prawdziwy przebieg z wagami modelu
opisywane jest wyżej (`scripts/local_run.sh`, `scripts/manual_e2e.py`).

| Plik | Zakres |
| --- | --- |
| `tests/test_api.py` | Kontrakt endpointów, walidacja, kody błędów, OpenAPI |
| `tests/test_vtt.py` | Dzielenie na cue, łamanie wierszy, czasy, brak nakładania |
| `tests/test_metrics.py` | Pola logu, JSON Lines, podsumowania |
| `tests/test_audio.py` | Pobieranie, limity, konwersja 16 kHz mono, cięcie WAV |
| `tests/test_transcriber.py` | Offsety chunków, ucinanie słów, parametry Whispera |
| `tests/test_pipeline.py` | Cały przebieg: API → kolejka → zadanie → metryki → webhook |

## Konfiguracja

Wszystko przez zmienne środowiskowe (plik `.env`, patrz `.env.example`).

| Zmienna | Domyślnie | Opis |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/transcription.db` | Baza zadań; dowolny URL SQLAlchemy 2.x |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Broker kolejki |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/1` | Backend wyników Celery |
| `CELERY_BROKER_TRANSPORT_OPTIONS` | `{}` | Opcje transportu brokera (JSON) |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large` |
| `WHISPER_DEVICE` | `cpu` | `cpu` albo `cuda` |
| `WHISPER_MODEL_DIR` | `./models` | Katalog cache wag |
| `DEFAULT_LANGUAGE` | `pl` | Język domyślny w API |
| `ALLOWED_LANGUAGES` | 30 języków | Lista języków przyjmowanych w API |
| `AUDIO_CHUNK_SECONDS` | `300` | Długość fragmentu audio (5 min) |
| `AUDIO_MAX_DURATION_SECONDS` | `14400` | Limit długości pliku (4 h) |
| `AUDIO_MAX_SIZE_MB` | `300` | Limit rozmiaru pobieranego pliku |
| `ALLOW_PRIVATE_AUDIO_URLS` | `true` | `false` blokuje URL-e z sieci wewnętrznej |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | Timeout pojedynczej próby webhooka |
| `WEBHOOK_MAX_RETRIES` | `3` | Liczba prób dostawy |
| `WEBHOOK_SECRET` | - | Włącza podpis `X-Webhook-Signature` |
| `METRICS_LOG_PATH` | `logs/transcription_metrics.log` | Plik metryk |
| `LOG_LEVEL` | `INFO` | Poziom logowania |

## Struktura projektu

```
app/
├── main.py              # aplikacja FastAPI, lifespan, obsługa błędów
├── api/routes.py        # endpointy
├── config.py            # ustawienia (pydantic-settings)
├── celery_app.py        # konfiguracja Celery
├── tasks.py             # zadanie transkrypcyjne
├── repository.py        # zapis i odczyt stanu zadań
├── models.py            # model SQLAlchemy
├── schemas.py           # schematy request/response
├── db.py                # silnik, sesje, inicjalizacja
├── utils.py             # czas, czyszczenie tekstu, formatowanie znaczników
├── logging_config.py    # wspólna konfiguracja logów
└── services/
    ├── audio.py         # pobieranie, ffmpeg, cięcie WAV
    ├── transcriber.py   # Whisper: ładowanie modelu, transkrypcja chunków
    ├── vtt.py           # budowa pliku WebVTT
    ├── metrics.py       # zapis i odczyt metryk
    └── webhook.py       # dostawa webhooków z retry
scripts/                 # warmup modelu, odbiornik webhooków, próbki audio, skrypty pomocnicze
tests/                   # 79 testów
docker/entrypoint.sh     # punkt wejścia obrazu (api / worker / beat / warmup)
```

## Jak to działa

**1. Zlecenie.** `POST /api/v1/transcribe` waliduje URL (tylko `http`/`https`), sprawdza
język wobec listy dozwolonych, zakłada wiersz w bazie ze statusem `queued` i wysyła
`job_id` do brokera. Odpowiedź wraca natychmiast, przed jakąkolwiek pracą z audio.
Gdy broker jest niedostępny, API zwraca `503` zamiast przyjmować zadania, które nikt
nie wykona.

**2. Pobranie.** Worker odbiera zadanie i od razu oznacza je jako `processing` przez
warunkowy update w bazie (`claim_job`) - dzięki temu dwaj workerzy nie wezmą tego
samego zadania. Plik jest pobierany strumieniowo, z kontrolą rozmiaru, typu zawartości
i czasu; katalog roboczy dostaje unikalny prefiks per zadanie i jest usuwany po zadaniu.

**3. Przygotowanie audio.** `ffmpeg` zamienia plik na 16 kHz mono WAV (dokładnie to,
czego wymaga Whisper), a `ffprobe` czyta długość. Dłuższe nagrania są cięte na
fragmenty po granicach próbek - bez ponownego kodowania, więc czasy są dokładne.

**4. Transkrypcja.** Model `small` ładuje się raz w procesie workera i zostaje w pamięci
dla kolejnych zadań (`WORKER_MAX_TASKS_PER_CHILD` odpowiada za cykliczne odświeżenie
procesu). Każdy fragment dostaje `word_timestamps=True`, a czasy słów są przesuwane
o `numer_fragmentu * AUDIO_CHUNK_SECONDS`. Między fragmentami kontekst nie jest
przenoszony - Whisper czasie wpada w pętlę powtarzania zdania, więc zaczynamy czysto
i usuwamy takie pętle z wyniku. Słowo leżące na krawędzi cięcia (Whisper zapisuje je
wtedy jako `wito...`) jest wyrzucane, bo i tak jest urwane.

**5. WebVTT.** Z segmentów i słów budowane są cue: maks. 6 s, maks. 42 znaki w wierszu,
maks. dwa wiersze, cięcie na przerwie w mowie lub po interpunkcji. Kolejne cue nigdy na
siebie nie nachodzą. Jeśli Whisper nie zwrócił znaczników słów, segment jest dzielony
proporcjonalnie po czasie. Wynik ma nagłówek `WEBVTT` i blok `NOTE` z `job_id`, językiem
i modelem.

**6. Metryki i webhook.** Metryka powstaje zawsze, także gdy zadanie się nie uda -
liczy `processing_time`, `word_count` i zapisuje linię JSON z `flock`. Dopiero potem
idzie webhook, więc awaria odbiorcy nie zaburza statystyk. Kolejne próby dostawy
zapisują się osobno w `logs/webhook_deliveries.log`.

## Dobre praktyki i ograniczenia

- **Baza.** Domyślnie SQLite (WAL), wystarcza do jednego workera. Do wielu instancji
  albo trafniejszych metryk podmień `DATABASE_URL` na PostgreSQL i doinstaluj
  `psycopg[binary]` - reszta kodu nie wymaga zmian.
- **Konkurencja workera.** Każdy proces workera trzyma własną kopię modelu (~1,5 GB RAM).
  Domyślnie `WORKER_CONCURRENCY=1`; kolejność zadań jest wtedy przewidywalna, a pamięć
  pod kontrolą.
- **Limity.** `AUDIO_MAX_DURATION_SECONDS`, `AUDIO_MAX_SIZE_MB` i `TASK_TIME_LIMIT`
  chronią workera przed zbyt długim lub zbyt dużym materiałem.
- **SSRF.** Domyślnie dopuszczamy URL-e z sieci wewnętrznej, żeby dało się testować
  lokalnie. Na produkcji ustaw `ALLOW_PRIVATE_AUDIO_URLS=false` - wtedy adresy
  prywatne, loopback i link-local są odrzucane przed pobraniem.
- **Granice fragmentów.** Cięcie na 5-minutowe fragmenty daje realny postęp i mały
  pobór pamięci kosztem lekkiego gubienia kontekstu na granicach. Dłuższe nagrania
  można podzielić z nachodzącymi fragmentami - wtedy ryzyko powtórzeń na styku.
- **Jakość polskiego tekstu.** Whisper `small` daje ~85-90% poprawności słów. Dla emisji
  najlepiej dodać własny model fine-tuned na polskim korpusie radiowym.
- **Migracje.** Schemat tworzy się przy starcie (`create_all`). Przy produkcyjnym wdrożeniu
  warto dołożyć Alembic.
