# TeamMeetingAI

TeamMeetingAI is a **client-server meeting recorder and analyzer** for
employees who attend Google Meet meetings. A PySide6 desktop client records
the meeting's microphone and system audio and talks to a FastAPI server over
HTTP; the server stores everything in PostgreSQL and runs transcription
(Sarvam Saaras v4, with a local Faster-Whisper fallback) and meeting analysis
(Groq) in a background job pipeline.

One machine (the *host*) runs the server and the PostgreSQL database, LAN-
only. Other computers on the LAN can run the client in read-only mode.

## Current status

Implemented in this repository:

- **FastAPI server** (`api/`) with a uniform `{"error": {...}}` envelope,
  routers for meetings, files, transcripts, summaries, jobs, search, users,
  and system status, and host-token authorization for all mutating
  endpoints.
- **PostgreSQL persistence** (`db/`): meeting audio is stored as Large
  Objects (original 48 kHz stereo WAV plus a derived 16 kHz mono WAV), with
  a delete trigger that unlinks each Large Object in the same transaction as
  the metadata row — hard deletes never leak storage.
- **Ingestion and processing pipeline** (`services/`): uploads are ingested
  atomically; a background job runner claims jobs with
  `FOR UPDATE SKIP LOCKED` and runs transcription and analysis with
  cooperative cancellation and throttled progress reporting.
- **Transcription**: Sarvam Saaras v4 batch API (`saaras:v4`, transliteration,
  diarization) as the mandatory primary, with exactly one automatic local
  Faster-Whisper fallback attempt on any Sarvam failure. The transcript and
  job rows record which provider produced the text and why.
- **Analysis**: Groq chat API with strict JSON schema validation before
  anything is saved; action items are stored as individual rows.
- **PySide6 client** (`ui/`, `client/`): profile selection, dashboard with
  search, manual recording with device selection, live job-status polling
  with cancel, audio playback (streamed from the server), and meeting
  details with the full analysis.
- **Database backups**: `scripts/backup_database.py` (custom-format pg_dump,
  Large Objects included, recorded in `backup_runs` and surfaced by
  `GET /system/status`) and `scripts/restore_database.py`.
- **Automated tests**: 229 tests, including a PostgreSQL tier (schema, Large
  Object lifecycle, API, ingestion pipeline) and UI smoke tests that drive
  real windows against a live server.

## Requirements

- Windows 10 or 11 (client audio capture is Windows-only)
- Python 3.11 (the server uses `audioop`, removed in Python 3.13)
- Server host: PostgreSQL 14+ reachable at `DATABASE_URL`; `pg_dump`/
  `pg_restore` client tools for backups
- Server host: a Sarvam API key (transcription); a Groq API key and model
  for analysis
- Client: a working microphone and playback device

## Setup

### Server host

1. Install PostgreSQL and create a database and role for the app.
2. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in at least:

   | Variable | Purpose |
   | --- | --- |
   | `DATABASE_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg://user@localhost:5432/teammeetingai` |
   | `TEAMMEETINGAI_SARVAM_API_KEY` | Sarvam transcription API key |
   | `TEAMMEETINGAI_HOST_TOKEN` | Shared secret; only holders may mutate data (recommended) |
   | `TEAMMEETINGAI_ANALYSIS_BACKEND` | `groq` to enable analysis (plus the variables below) |
   | `TEAMMEETINGAI_GROQ_MODEL` / `TEAMMEETINGAI_GROQ_API_KEY` | Groq analysis settings |

4. Apply the schema and start the server:

   ```powershell
   .\.venv\Scripts\python.exe -m alembic upgrade head
   scripts\run_server.bat
   ```

   The server listens on `0.0.0.0:8000` and refuses to start if the database
   schema is missing or newer than the code.

### Client

1. Install dependencies into a virtual environment (same `requirements.txt`).
2. Point the client at the server and, if it is the recording machine, share
   the host token:

   | Variable | Purpose |
   | --- | --- |
   | `TEAMMEETINGAI_SERVER_URL` | Server base URL (default `http://localhost:8000`) |
   | `TEAMMEETINGAI_HOST_TOKEN` | Must match the server's value to enable recording |

3. Start the client with `run.bat` (or `python main.py`).

## Using the application

1. Select or enter a profile name.
2. Select **Start new recording**, enter a meeting title, and choose the
   microphone and the speaker/headset Google Meet plays through.
3. Click **Start recording** after confirming all participants have
   consented. The recording indicator and elapsed timer stay visible.
4. Click **Stop recording**. The client uploads the recording, the server
   queues transcription, and analysis (if configured) runs automatically
   afterwards.
5. The details screen polls job status; a **Cancel** button is available
   while processing. Cancelling never leaves a partial transcript behind.
6. Deleting a meeting removes its audio, transcripts, summaries, and action
   items in one transaction.

## Backups

```powershell
.\.venv\Scripts\python.exe scripts\backup_database.py
.\.venv\Scripts\python.exe scripts\restore_database.py backups\teammeetingai-YYYYMMDD-HHMMSS.dump --yes
```

Backups are custom-format pg_dump files (Large Objects included) written to
`backups/` (override with `--output-dir` or `TEAMMEETINGAI_BACKUP_DIR`).
Every attempt is recorded in `backup_runs` and reported by
`GET /system/status`. Restore is destructive (`--clean --if-exists`) and
therefore requires `--yes`. Verify a restore in a scratch database before
trusting it.

## Configuration reference

All configuration is environment-based (`.env` or real environment
variables; real variables win). Server-only variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://localhost/teammeetingai` | Server database (also `TEAMMEETINGAI_DATABASE_URL`) |
| `TEAMMEETINGAI_SARVAM_API_KEY` | blank | Sarvam transcription key |
| `TEAMMEETINGAI_HOST_TOKEN` | blank | Shared host secret for mutating endpoints |
| `TEAMMEETINGAI_ANALYSIS_BACKEND` | `none` | `groq` enables analysis |
| `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS` | unset | Must be `1` to enable cloud analysis |
| `TEAMMEETINGAI_GROQ_MODEL` | blank | Groq model (required with `groq`) |
| `TEAMMEETINGAI_GROQ_TIMEOUT` | `60` | Groq request timeout (seconds) |
| `TEAMMEETINGAI_GROQ_API_KEY` | blank | Groq key (required with `groq`) |

Client and transcription-fallback variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEAMMEETINGAI_SERVER_URL` | `http://localhost:8000` | Client → server base URL |
| `TEAMMEETINGAI_SAMPLE_RATE` | `48000` | Capture sample rate |
| `TEAMMEETINGAI_WHISPER_MODEL` | `small` | Whisper fallback model |
| `TEAMMEETINGAI_WHISPER_DEVICE` / `_COMPUTE_TYPE` | `cpu` / `int8` | Whisper fallback tuning |
| `TEAMMEETINGAI_WHISPER_LANGUAGE` | auto | Pin the language (e.g. `hi`) for reliability |
| `TEAMMEETINGAI_WHISPER_INITIAL_PROMPT` | blank | Script/vocabulary hint for the fallback |
| `TEAMMEETINGAI_WHISPER_BEAM_SIZE` | `1` | Greedy decoding (fastest) |
| `TEAMMEETINGAI_WHISPER_VAD_MIN_SILENCE_MS` | `500` | Skip silence instead of inventing text |
| `TEAMMEETINGAI_WHISPER_CPU_THREADS` | engine default | CPU threads for the fallback |
| `TEAMMEETINGAI_DATA_DIR` | `%LOCALAPPDATA%\TeamMeetingAI` | Client data directory |

## Architecture

```text
ui/                   PySide6 screens (login, dashboard, recorder, details, status)
client/api_client.py  The single HTTP client wrapper (httpx)
audio/                Microphone + WASAPI loopback capture, WAV finalization
main.py               Client startup and window wiring
config.py             Client configuration and paths
api/                  FastAPI app, routers, schemas, host-token dependencies
services/             Ingestion, job pipeline, job runner, ZIP export
db/                   SQLAlchemy models, Large Object helpers, Alembic migrations
ai/                   Sarvam/Whisper/Groq providers, fallback, JSON validation
scripts/              run_server.bat, backup_database.py, restore_database.py
storage/              Legacy (pre-PostgreSQL) file-backed store; not wired in
tests/                Unit + PostgreSQL + UI smoke tiers (tests/conftest.py)
```

Data flow: record locally → `POST /meetings` (host-only) → ingest stores
both WAV tracks as Large Objects and queues a transcription job → the job
runner transcribes (Sarvam, Whisper fallback) → an analysis job stores the
summary and action items → clients poll job status and refresh.

## Privacy and consent

- Recording always requires an explicit user action, with a visible
  recording indicator and elapsed timer.
- **Cloud processing is part of the design**: meeting audio is sent to
  Sarvam for transcription, and the primary transcript is sent to Groq for
  analysis. The client shows a clear notice before recording and keeps a
  visible warning while cloud backends are configured. Obtain participant
  consent and follow organizational policy.
- The Whisper fallback keeps processing fully local when Sarvam is
  unavailable; with `TEAMMEETINGAI_ANALYSIS_BACKEND=none` nothing is sent to
  an analysis service.
- The API is LAN-only; PostgreSQL is never exposed to the network.
- Non-host clients are read-only (`GET /system/status` reports
  `can_record`).
- Logs never contain audio, transcripts, tokens, or API keys. Transcripts
  are treated as untrusted data: spoken instructions cannot cause the
  application to execute commands or access files.
- Model output is validated against the schema before anything is saved.

## Development and testing

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                                # unit tier
$env:TEST_DATABASE_URL = "postgresql+psycopg://user@localhost:5432/postgres"
.\.venv\Scripts\python.exe -m pytest                                # full tier
```

The PostgreSQL tier creates and destroys a scratch database (`tma_test_*`)
on the `TEST_DATABASE_URL` server and migrates it to head. Tests never
record real audio and never call Sarvam or Groq; providers are faked.

## Known limitations

- Windows-only audio capture; no echo cancellation.
- Speaker identification is limited to Sarvam's diarization labels.
- No per-user accounts or passwords: identity is a display-name profile plus
  the shared host token (JWT auth is a future seam).
- No automatic recording, calendar integration, or meeting bots.
- Legacy SQLite/filesystem-era meeting data is not yet migrated; the old
  `storage/` code is kept as the conversion reference.
- Packaging (PyInstaller) for non-developer machines is not done yet.
