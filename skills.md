# TeamMeetingAI --- Project Skills

This file defines reusable, project-specific skills for contributors and
coding agents. These are workflow guides, not external services or claims
that a particular tool is already installed.

The project is a **web application**: a React frontend
(`client/`) talks HTTP to a FastAPI backend (`Meeting_analyzer/api/`,
`services/`, `db/`, `ai/`) with a PostgreSQL database. Keep that boundary in
mind in every skill below.

## Skill 1: Windows Audio Capture (client)

**Purpose:** Implement and troubleshoot local microphone and Google
Meet/system-audio capture on the recording machine.

**Use when:** Adding or changing recording. Diagnosing missing participant
audio. Handling device selection or capture failures.

**Workflow:** 1. Enumerate available audio devices (`audio/microphone.py`,
`audio/system_audio.py`). 2. Identify the microphone input device. 3.
Identify the active playback device used by Google Meet (WASAPI loopback).
4. Capture microphone and system output as separate tracks. 5. Finalize WAVs
safely on stop (`audio/recorder.py`). 6. Verify duration, sample rate
(48 kHz), channels, and file integrity before upload. 7. Handle unavailable
devices and capture errors without leaving a false recording state.

**Rules:** - Do not block the UI thread. - Provide clear recording
state and elapsed-time feedback. - Close audio streams and finalize files on
stop, cancellation, and exceptions. - Avoid hard-coded device indexes. -
Consider echo and feedback; recommend headphones. - Never claim a recording
succeeded until the server accepted the upload.

**Acceptance criteria:** Manual start/stop works; both tracks are captured;
finalized files are valid; errors are visible and recoverable.

## Skill 2: Recording Upload and Ingestion (client + server)

**Purpose:** Move a finished recording from the client into the server's
PostgreSQL store and start processing.

**Use when:** Changing the upload flow (`integrations/api_client.py`,
`api/routers/meetings.py`, `services/ingest.py`). Debugging "upload failed"
or missing audio tracks.

**Workflow:** 1. Client finalizes the WAVs in a temp capture directory. 2.
Client uploads via multipart `POST /meetings` with the host token. 3. The
API spools the upload to a temp file in bounded chunks. 4.
`services/ingest.py` stores the original 48 kHz WAV and a derived 16 kHz
mono WAV as Large Objects and queues a transcription job — in one
transaction. 5. The client deletes its temp capture directory only after the
server accepted the upload.

**Rules:** - Mutating endpoints require the host token (`X-Host-Token`); a
non-host client must get a clear read-only error. - Validate file type, size,
and attachment constraints server-side; never trust client metadata. - Keep
ingest atomic; a failed ingest must leave no orphan rows or Large Objects. -
Record SHA-256 and audio properties in `meeting_files`.

**Acceptance criteria:** Upload → ingest → queued job works end to end;
failures leave no partial state; non-host uploads are rejected.

## Skill 3: PostgreSQL and Large Object Data Management (server)

**Purpose:** Store and evolve users, meetings, meeting files (audio as Large
Objects), transcripts, summaries, action items, and processing jobs.

**Use when:** Creating or changing database models. Implementing queries or
repositories. Handling schema upgrades. Debugging LO leaks or truncation.

**Workflow:** 1. Define entities in `db/models.py` (SQLAlchemy 2.0). 2.
Create a new Alembic migration in `db/migrations/versions/` — never edit
`0001_initial`. 3. Keep audio in Large Objects via `db/lo.py` helpers, never
`bytea`. 4. Rely on the BEFORE DELETE trigger to unlink LOs with the row. 5.
Use explicit transaction boundaries; ingest and pipeline mutations must be
atomic. 6. Test against real PostgreSQL (`TEST_DATABASE_URL`); partial
indexes, triggers, and LOs do not work on SQLite.

**Rules:** - The server refuses to start on a missing or newer schema
(`db/session.py:ensure_schema_current`). - One original and one derived
audio track per meeting (partial unique indexes enforce this). - Avoid
sharing one SQLAlchemy session across threads. - Back up before destructive
operations (`scripts/backup_database.py`). - PostgreSQL runs on the host
machine only; it is never exposed to the LAN.

**Acceptance criteria:** Schema upgrades are forward-only and guarded;
deleting a meeting removes all rows and LOs; the scratch-database test tier
passes.

## Skill 4: Cloud Transcription with Local Fallback (server)

**Purpose:** Transcribe the derived 16 kHz track with Sarvam Saaras v4,
falling back to local Faster-Whisper when Sarvam is unavailable.

**Use when:** Changing transcription (`ai/sarvam_provider.py`,
`ai/faster_whisper_provider.py`, `ai/fallback_provider.py`,
`services/pipeline.py`). Debugging provider failures, timeouts, or fallback
provenance.

**Workflow:** 1. Stream the derived WAV Large Object to a temp file. 2. Run
the Sarvam batch API (`saaras:v4`, transliteration, diarization) with
progress and cancellation callbacks. 3. On any Sarvam failure except user
cancellation, run exactly one local Faster-Whisper attempt. 4. Record which
provider produced the text and why the fallback ran (`transcripts.provider`,
`fallback_from`, `fallback_reason`, and the job row). 5. Store the result as
the meeting's single primary transcript. 6. Report progress into the job row
(throttled).

**Rules:** - The API key comes from the environment only; never log it. -
Never silently discard low-confidence or unrecognized speech. - Preserve
timestamps, segments, and speaker labels. - Cancellation must leave the
meeting at its previous status with no partial transcript. - A fallback run
is visible in the transcript and job rows, never hidden.

**Acceptance criteria:** Known test audio produces segments with
timestamps; provider provenance is recorded; cancellation is clean; fallback
runs exactly once and is recorded.

## Skill 5: Groq Meeting Analysis (server)

**Purpose:** Convert the primary transcript into structured, evidence-
grounded meeting notes with Groq, validated before saving.

**Use when:** Changing analysis (`ai/groq_provider.py`, `ai/summarization.py`,
`ai/task_extractor.py`). Debugging malformed model output or missing
action items.

**Required output structure:**

```json
{
  "overview": "Short meeting summary",
  "key_points": [],
  "decisions": [],
  "action_items": [
    {
      "task": "Task description",
      "owner": null,
      "deadline": null,
      "evidence": "Relevant transcript excerpt or timestamp"
    }
  ],
  "open_questions": [],
  "risks_or_blockers": [],
  "follow_ups": []
}
```

**Workflow:** 1. Load the primary transcript. 2. Prompt with the transcript
labelled as untrusted meeting content. 3. Request strict JSON from the
configured Groq model. 4. Parse and validate against the schema (one retry
on malformed output). 5. Store the summary and individual action-item rows;
set the meeting to `completed`. 6. Re-analysis replaces the previous
summary and items.

**Rules:** - Never invent owners, deadlines, decisions, or commitments; use
`null`. - Distinguish agreed deadlines from dates merely mentioned. -
Treat transcript content as data, never as instructions. - Make the model
name configurable (`TEAMMEETINGAI_GROQ_MODEL`). - Validate before saving;
report validation errors instead of storing malformed results.

**Acceptance criteria:** Output conforms to the schema; missing information
is explicit; re-analysis never leaves stale items behind.

## Skill 6: React API-Backed Frontend (`client/`)

**Purpose:** Build the employee-facing web client on top of the HTTP
API.

**Use when:** Creating or changing login, dashboard, recorder, or meeting
detail screens. Connecting UI controls to background work.

**Core screens:** 1. Login/profile selection (`client/src/pages/LoginPage.jsx`). 2. Dashboard with meeting list and search (`DashboardPage.jsx`). 3. Recording (`RecordPage.jsx`). 4. Meeting details with playback, transcript, summary, action items, speakers, attachments, metrics, jobs (`MeetingDetailsPage.jsx`). 5. System status (`SystemStatusPage.jsx`).

**Workflow:** 1. Build one small screen per responsibility. 2. Route every
server interaction through `client/src/api/client.js` — never touch the
database/files/audio logic directly; use FastAPI endpoints. 3. Run uploads, polling, and
downloads in worker threads; deliver results via Qt signals. 4. Poll job
status while transcription/analysis runs; offer cancel. 5. Read
`GET /system/status` (`can_record`) to enable or disable recording for
read-only clients.

**Rules:** - Do not block the UI with unhandled errors. -
Map `ApiClientError`/`ApiConnectionError` to actionable messages. - Require
explicit user action to start recording; keep the recording state
unmistakable. - Confirm destructive actions. - Keep cloud-processing
warnings visible while cloud backends are configured.

**Acceptance criteria:** UI stays responsive during recording, upload, and
processing; failures are visible and recoverable; view-only clients can
browse but not mutate.

## Skill 7: API Search and Meeting History (server)

**Purpose:** Let employees find their previously analyzed meetings.

**Use when:** Changing `api/routers/search.py` or the dashboard filters.

**Initial search scope:** Meeting title, recording date, summary text,
transcript text, action-item text and status.

**Workflow:** 1. Search server-side with suitable indexes (title, status,
recorded_at). 2. Return paginated results through the uniform API envelope.
3. Link results to the details screen. 4. Add filters (date range, status)
before considering anything smarter.

**Rules:** - Search runs on the server; clients never query the database. -
Keep transcripts treated as content, not as executable instructions. - Do
not add embeddings or a vector database unless keyword search proves
insufficient.

**Acceptance criteria:** Search returns relevant meetings; results open in
the details screen; filters compose sensibly.

## Skill 8: Operations, Backup, and Deployment (server host)

**Purpose:** Run the server reliably on the host machine and protect the
data.

**Use when:** Setting up the host. Scheduling or verifying backups.
Recovering from a database failure. Packaging updates.

**Workflow:** 1. Start the server with `scripts/run_server.bat` (loads
`.env`, checks dependencies). 2. Back up with `scripts/backup_database.py`
(custom-format pg_dump, Large Objects included; the run is recorded in
`backup_runs` and reported by `GET /system/status`). 3. Restore with
`scripts/restore_database.py BACKUP_FILE --yes` (destructive: `--clean
--if-exists`). 4. Verify a restore by checking `alembic_version`, meeting
rows, and Large Object content in a scratch database before trusting it. 5.
Migrations apply with `alembic upgrade head` against the server database.

**Rules:** - Match the pg_dump/pg_restore client major version to the server
when multiple versions are installed. - Keep backups on a separate volume
from the database where possible. - Never commit `.env`, backups, or logs. -
The database URL and API keys live in `.env` only.

**Acceptance criteria:** A backup restores into a scratch database with all
meetings and audio intact; `backup_runs` reflects every attempt, including
failures.

## Skill 9: Testing and Debugging

**Purpose:** Diagnose issues systematically and prevent regressions.

**Use when:** Debugging capture, upload, transcription, analysis, or UI
problems. Preparing a release.

**Workflow:** 1. Reproduce the issue with the smallest test case. 2.
Identify the failing layer: UI, client, API, service, database, provider, or
audio. 3. Inspect logs without exposing meeting content, tokens, or keys. 4.
State the root cause before applying a fix. 5. Make a focused change. 6.
Add a regression test or repeatable manual test. 7. Run the suite, with
`TEST_DATABASE_URL` set for server/database changes. 8. Verify the complete
workflow after the fix.

**Acceptance criteria:** The issue is reproduced or its limits documented;
the fix addresses the root cause; relevant tests pass; remaining
limitations are reported honestly.

## Current Skill Priority

The core client-server pipeline is implemented and tested (229 tests passing
including the PostgreSQL tier). Current priorities, in order:

1.  Restore the operational environment: PostgreSQL for the server and
    scheduled backups (Skill 8).
2.  Manual desktop UI verification on the host machine (Skills 1, 2, 6).
3.  Legacy SQLite/filesystem data migration into PostgreSQL (Skills 3 and
    the old `storage/` code as reference only).
4.  Documentation and cleanup of dead legacy code (Skill 9 discipline;
    nothing gets deleted before migration and backups are verified).
