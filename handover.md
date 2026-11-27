# TeamMeetingAI / Meeting Analyzer — Comprehensive Handover

> **Document purpose:** onboarding and continuity handoff for developers and coding agents (including Claude).  
> **Generated:** 2026-11-27 (workspace inspection date).  
> **Repository root:** `D:\Meeting_analyzer\Meeting_analyzer`  
> **Scope:** project-owned files/folders. Dependency dirs (`node_modules`, `.venv`, `__pycache__`, caches, build outputs), generated artifacts, and secrets are excluded from deep documentation except where they affect runtime behavior.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Product Features and User Workflows](#2-product-features-and-user-workflows)
3. [Technology Stack](#3-technology-stack)
4. [Repository Structure](#4-repository-structure)
5. [Folder-by-Folder Documentation](#5-folder-by-folder-documentation)
6. [File-by-File Documentation](#6-file-by-file-documentation)
7. [Classes and Function Reference](#7-classes-and-function-reference)
8. [Architecture and Execution Flows](#8-architecture-and-execution-flows)
9. [Database and Migration Guide](#9-database-and-migration-guide)
10. [Audio Pipeline and Opus Implementation](#10-audio-pipeline-and-opus-implementation)
11. [API and Frontend Guide](#11-api-and-frontend-guide)
12. [Configuration and Environment Variables](#12-configuration-and-environment-variables)
13. [How to Run the Application](#13-how-to-run-the-application)
14. [Testing and Verification Results](#14-testing-and-verification-results)
15. [Git History and Development Timeline](#15-git-history-and-development-timeline)
16. [Current Project Status](#16-current-project-status)
    - [16.3 Missing / Incomplete Functionality Matrix](#163-missing--incomplete-functionality-matrix)
17. [Known Issues and Technical Debt](#17-known-issues-and-technical-debt)
18. [Recommended Next Steps](#18-recommended-next-steps)
19. [Claude Agent Onboarding Guide](#19-claude-agent-onboarding-guide)
20. [Glossary](#20-glossary)

---

## 1. Project Overview

**TeamMeetingAI** (also referred to as FNW Meeting Analyzer / Meeting Analyzer) is a client-server meeting recorder and analyzer. It supports uploading/recording meeting audio, storing audio in PostgreSQL Large Objects, transcribing speech, performing speaker profiling/matching, generating structured meeting analysis (summary, decisions, action items, risks, follow-ups), and exporting meeting artifacts.

### Current deployment model

- **Frontend:** React + Vite SPA in `client/`, served by FastAPI in unified Docker image or proxied in dev.
- **Backend:** FastAPI app in `Meeting_analyzer/api/app.py`, exposed under `/api` in unified deployment.
- **Database:** PostgreSQL. Meeting Analyzer tables live in schema `meeting_analyzer`; Task Manager tables remain in `public`. Alembic version table is `meeting_analyzer.alembic_version`.
- **Audio storage:** PostgreSQL Large Objects (`pg_largeobject`), metadata in `meeting_analyzer.meeting_files`. A `BEFORE DELETE` trigger unlinks LOs on file-row deletion.
- **Processing:** Background `JobRunner` polls `processing_jobs` using `FOR UPDATE SKIP LOCKED`; executes transcription/analysis/profiling pipeline.

### Explicit design constraints (from AGENTS/README/config)

- No audio on filesystem library as permanent storage; audio binaries in LOs.
- No soft delete for meetings/files; hard delete with LO unlink trigger.
- Schema changes through Alembic migrations; server refuses start on missing/newer schema.
- Secrets in `.env` only; never log raw audio/transcripts/tokens/API keys.
- Transcripts are untrusted input; never execute instructions from transcript content.
- Cloud processing is explicit opt-in/configured: Sarvam for transcription (primary), Groq for analysis (opt-in), Faster-Whisper local fallback.

### Current state summary (as inspected)

- Branch: `final-demo` (ahead of `origin/master` by 5 commits). No local `main` branch; `master` tracks `origin/master`.
- Working tree has extensive **uncommitted** changes (modified backend/client/tests; untracked migrations `0007/0008/0009`; opus evaluation artifacts; debug/temp files). See [Current Project Status](#16-current-project-status).
- **Migration head (code intent):** `0009_audio_opus` (chain 0001→...→0009). **Repo reality:** 0001–0006 tracked; **0007–0009 are untracked in working tree** and must be reviewed/committed before any clean deploy that expects them.
- Opus-only storage for *new* recordings is implemented in code; legacy WAV compatibility retained. Full acceptance verification not complete.
- Runtime diagnosis (from prior session): deployed `tma-app` on Supabase reported schema `0009_audio_opus`, unhealthy due stale-recovery SQL bug and stale job; `POST /meetings` 409 observed but underlying DB constraint not identified from app logs.

---

## 2. Product Features and User Workflows

| Feature | User-facing behavior | Primary implementation |
|---|---|---|
| Profile selection/login | User selects/creates display-name profile; stored in browser localStorage. | `client/src/pages/LoginPage.jsx`, `client/src/api/client.js`, `api/routers/users.py` |
| Meeting upload/record ingest | User uploads/records audio; backend converts to canonical Opus for new recordings, stores LO, queues transcription. | `client/src/pages/RecordPage.jsx`, `api/routers/meetings.py`, `services/ingest.py` |
| Multi-source ingest | Accepts microphone/system/browser/mixed sources; stores one canonical mixed Opus artifact for new ingests, preserves manifest metadata. | `POST /meetings/multi-source`, `services/ingest.py::ingest_multi_source_recording` |
| Transcription | Sarvam primary with Faster-Whisper fallback; stores primary transcript, segments, provider provenance. | `services/pipeline.py`, `ai/*`, `api/routers/transcripts.py` |
| Analysis | Groq analysis over primary transcript; validates JSON schema; stores summary and action items. | `services/pipeline.py`, `ai/summarization.py`, `ai/task_extractor.py`, `api/routers/summaries.py` |
| Speaker profiling | Extracts embeddings, matches against `speaker_reference_embeddings` only, stores diagnostics; user can confirm/reject/assign. | `services/speaker_profiles.py`, `api/routers/speakers.py`, `client/src/pages/MeetingDetailsPage.jsx` |
| Action item sync | Syncs extracted action items to Task Manager `public.tasks` via mapping table `meeting_analyzer.meeting_task_sync`. | `services/task_sync.py`, `db/models.py`, migration 0004 |
| Jobs/cancellation/requeue | Poll jobs; cancel queued/running (cooperative); manual requeue for failed/cancelled. | `api/routers/jobs.py`, `services/runner.py`, `services/pipeline.py` |
| Export | ZIP export with metadata/transcript/summary/action items/attachments/optional audio, streamed from LOs. | `services/export.py`, `api` export endpoint |
| Search | ILIKE search across titles, primary transcript, summary, action items. | `api/routers/search.py` |
| System/health | `/health` readiness + worker/read-only/stale checks; `/system/status` DB/storage/backup info. | `api/routers/health.py`, `api/routers/system.py` |

### Core user workflow

```mermaid
flowchart LR
  U[User/Profile] --> R[Record/Upload]
  R --> API[POST /api/meetings or /multi-source]
  API --> ING[Ingest: normalize -> Opus -> validate -> LO]
  ING --> DB[(PostgreSQL meeting_analyzer)]
  ING --> Q[processing_jobs: transcription queued]
  Q --> RUN[JobRunner FOR UPDATE SKIP LOCKED]
  RUN --> PIPE[Pipeline transcribe]
  PIPE --> STT[Sarvam primary / Whisper fallback]
  STT --> T[(primary transcript)]
  T --> PROF[Profiling job optional/auto-chained]
  T --> AN[Analysis job if configured]
  AN --> SUM[(summary + action_items)]
  SUM --> SYNC[Task sync to public.tasks mapping]
  DB --> UI[Dashboard/Meeting Details polling]
```

---

## 3. Technology Stack

| Area | Technology | Evidence |
|---|---|---|
| Language/runtime | Python 3.11 | `Dockerfile`, `Dockerfile.server`, `README.md` |
| Backend API | FastAPI + Uvicorn | `requirements.txt`, `api/app.py` |
| Frontend | React 18, Vite 6, React Router 6 | `client/package.json` |
| DB | PostgreSQL 16 (docker compose), Supabase-compatible external PG supported | `docker-compose.yml`, `README.md`, `db/session.py` |
| ORM/migrations | SQLAlchemy 2.0, Alembic 1.13+ | `requirements.txt`, `db/base.py`, `alembic.ini` |
| Audio conversion | ffmpeg/ffprobe CLI | `services/ingest.py`, `Dockerfile*` |
| Transcription | Sarvam Saaras (`sarvamai`), Faster-Whisper fallback | `ai/sarvam_provider.py`, `ai/faster_whisper_provider.py`, `ai/fallback_provider.py` |
| Analysis | Groq SDK (opt-in cloud) | `ai/groq_provider.py`, `ai/provider_factory.py` |
| Speaker embeddings | Resemblyzer (optional/feature-gated) | `requirements.txt`, `services/speaker_profiles.py`, `config.py` |
| Audio capture libs (client/local) | soundcard, numpy | `requirements.txt`, `audio/*` |
| Tests | pytest | `pytest.ini`, `requirements-dev.txt`, `tests/*` |

---

## 4. Repository Structure

### Top-level (project-owned/relevant)

| Path | Purpose |
|---|---|
| `client/` | React frontend source, build config, nginx config, Dockerfile. |
| `Meeting_analyzer/` | Main backend package/app root: API, AI, DB, services, scripts, tests, config. |
| `docker-compose.yml` | Unified prod-like stack: Postgres + app (builds frontend + serves FastAPI). |
| `docker-compose.dev.yml` | Dev stack: Postgres + backend server + Vite dev client. |
| `Dockerfile` | Unified image: builds client then installs Python deps and runs Alembic+Uvicorn. |
| `Dockerfile.server` | Backend-only image. |
| `README.md` | Primary runbook/architecture doc; partially historical in some sections - verify against code. |
| `.env.example` | Environment variable template (no secrets). |
| `.gitignore`, `.dockerignore` | Ignore rules. |
| `context.md`, `CONTEXT_HANDOFF.md` | Historical handoff/context notes. |
| `setup.ps1`, `run.sh` | Setup/run helpers (verify before use). |
| `opus_bitrate_evaluation/` | Standalone bitrate evaluation artifacts/results; not runtime code. |
| `clear_synthetic_meeting_10min.*` | Local synthetic audio/reference test artifact files (untracked). |
| `graphify-out/` | Knowledge-graph/report artifact directory; not application runtime. |
| `.commandcode/` | Tooling state directory (untracked); not app code. |

### `Meeting_analyzer/` (backend app root)

| Path | Purpose |
|---|---|
| `api/` | FastAPI app, deps, schemas, errors, routers. |
| `ai/` | Transcription/summarization contracts and providers. |
| `audio/` | Local capture utilities (microphone/system/recorder). |
| `db/` | SQLAlchemy base/models/session/LO helpers/Alembic migrations. |
| `integrations/` | Backend HTTP integration client wrapper. |
| `services/` | Ingest, pipeline, runner, export, speaker profiles, task sync, metrics. |
| `scripts/` | Backup/restore/migration/evaluation utilities. |
| `storage/` | Legacy storage/migration code; not wired into main app flow. |
| `tests/` | Pytest suite + pg harness/support. |
| `tools/` | Windows capture agent. |
| `docs/` | Focused implementation notes (multi-source recording). |
| `config.py` | Central settings/env parsing and data dirs. |
| `main.py` | CLI launcher for uvicorn/API with env loading and migration hints. |
| `alembic.ini` | Alembic config; URL resolved in `db/migrations/env.py`. |
| `requirements*.txt`, `pytest.ini` | Dependencies and test config. |

---

## 5. Folder-by-Folder Documentation

### 5.1 `client/`

- **Why:** User interface for login/profile, dashboard, recording/upload, meeting details, system status.
- **Responsibilities:** Browser audio capture/recording, API calls, job polling, transcript/speaker review UI, downloads.
- **Connections:** Talks to backend through `src/api/client.js` using `VITE_API_BASE` (default `/api`). Vite dev proxy rewrites `/api` to backend.
- **Important files:** `package.json`, `vite.config.js`, `src/App.jsx`, `src/api/client.js`, `src/pages/*`, `src/styles/app.css`, `src/utils/format.js`, `Dockerfile`, `nginx/default.conf`.

### 5.2 `Meeting_analyzer/api/`

- **Why:** HTTP boundary and request/response contracts.
- **Responsibilities:** App assembly, CORS/static serving, request-id middleware, error envelope, auth/profile dependency, routers for domain operations.
- **Connections:** Uses `db/session.py` engine, `services/*`, `ai/provider_factory.py`, `config.py`.
- **Important files:** `app.py`, `deps.py`, `errors.py`, `schemas.py`, `routers/*.py`.

### 5.3 `Meeting_analyzer/ai/`

- **Why:** Isolate AI provider contracts from API/services.
- **Responsibilities:** Transcription/summarization protocols, result validation, fallback, provider selection, segment postprocess, Groq/Sarvam/Whisper adapters.
- **Connections:** `services/pipeline.py` uses `TranscriptionService` and `MeetingSummarizer`; `provider_factory` builds providers from settings.
- **Important files:** `transcription.py`, `summarization.py`, `provider_factory.py`, `fallback_provider.py`, `task_extractor.py`, `segment_postprocess.py`, provider adapters.

### 5.4 `Meeting_analyzer/db/`

- **Why:** Persistence model, schema isolation, migrations, LO storage.
- **Responsibilities:** Declarative base with naming convention, SQLAlchemy models, engine/schema guard, LO read/write/stream, Alembic env and versions.
- **Connections:** All routers/services use engine/connections; migrations define `meeting_analyzer` schema evolution.
- **Important files:** `base.py`, `models.py`, `session.py`, `lo.py`, `migrations/env.py`, `migrations/versions/0001-0009`.

### 5.5 `Meeting_analyzer/services/`

- **Why:** Core business logic and background processing.
- **Responsibilities:** Audio ingest/normalization/Opus conversion, job runner, transcription/analysis/profiling pipeline, export, speaker profile matching, task sync, metrics.
- **Connections:** Called by routers and runner; uses DB, LO, AI, config.
- **Important files:** `ingest.py`, `pipeline.py`, `runner.py`, `speaker_profiles.py`, `task_sync.py`, `export.py`, `transcript_metrics.py`.

### 5.6 `Meeting_analyzer/scripts/`

- **Why:** Operational utilities.
- **Responsibilities:** DB backup/restore, legacy schema migration helper, golden metrics evaluation.
- **Connections:** Backup/restore interact with pg_dump/pg_restore and `backup_runs`; migration script for legacy public->meeting_analyzer data moves.

### 5.7 `Meeting_analyzer/tests/`

- **Why:** Regression and behavior verification.
- **Responsibilities:** Unit tests (no DB) and PostgreSQL integration tests (opt-in via `TEST_DATABASE_URL`).
- **Connections:** `conftest.py` + `pg_harness.py` create/migrate/truncate scratch DB.
- **Important files:** `conftest.py`, `pg_harness.py`, `support.py`, `test_*.py`.

### 5.8 `Meeting_analyzer/audio/`, `integrations/`, `storage/`, `tools/`, `docs/`

- **audio/**: Local capture abstractions; recorder/microphone/system audio. Platform-sensitive.
- **integrations/**: `TeamMeetingAIClient` HTTP wrapper; useful for scripts/internal integrations.
- **storage/**: Legacy SQLite/filesystem-era library/migration code; retained for reference/migration, not main server path.
- **tools/**: `windows_capture_agent.py` for Windows local multi-source capture/upload.
- **docs/**: `MULTI_SOURCE_RECORDING.md` implementation status and validation caveats.

---

## 6. File-by-File Documentation

> This section covers project-owned files. For large files, key symbols/behavior are documented; line-level exhaustive commentary is provided for critical paths.

### 6.1 Root files

| File | Purpose | Key details | Connections |
|---|---|---|---|
| `README.md` | Primary documentation/runbook. | Describes architecture, setup, Docker, env vars, endpoints, schema, backup/restore. Some sections are historical (e.g., older WAV ingest description in AGENTS; README has Opus notes). Verify commands before use. | References compose, env, scripts, API. |
| `docker-compose.yml` | Unified local/prod-like deployment. | `db` Postgres 16-alpine + `app` build from root Dockerfile; mounts backup dir; runs Alembic then uvicorn. | Uses `.env`, `Dockerfile`, `Meeting_analyzer/scripts/backup_database.py`. |
| `docker-compose.dev.yml` | Dev hot reload. | Separate `server` (Dockerfile.server) and `client` (node 20 alpine, Vite). | `client/vite.config.js` proxy target `server:8000`. |
| `Dockerfile` | Unified image. | Builds client with `VITE_API_BASE=/api`; installs ffmpeg/libsndfile/curl; installs Python deps; copies backend; serves static from client dist. | `api/app.py` static mount, Alembic startup. |
| `Dockerfile.server` | Backend-only image. | Installs ffmpeg/libsndfile; runs Alembic upgrade then uvicorn. | Dev compose/server-only deploy. |
| `.env.example` | Env template. | Includes DB, ports, Groq/Sarvam, voice profiling, CORS, logging. No secrets. | `config.py`, compose, scripts. |
| `.gitignore` | Ignore rules. | Ignores env, venv, caches, node_modules, data, backups, etc. | Repo hygiene. |
| `context.md`, `CONTEXT_HANDOFF.md` | Historical context. | Useful background; may be stale relative to current uncommitted work. | Handover context. |
| `setup.ps1` | Windows setup helper. | Inspect before running; may assume local paths/tools. | Onboarding. |
| `run.sh` | Linux/mac helper. | Inspect before running. | Onboarding. |

### 6.2 `client/`

| File | Purpose | Key symbols/responsibilities | Connections |
|---|---|---|---|
| `package.json` | Frontend deps/scripts. | React 18, react-router-dom 6, Vite 6; scripts `dev/build/preview`. | Vite build/dev. |
| `vite.config.js` | Dev server/proxy. | Proxies `/api` to `VITE_PROXY_TARGET` default `http://127.0.0.1:8000`, rewrites `/api` prefix. | Backend API in dev. |
| `index.html` | SPA HTML shell. | Mount point `#root`. | `src/main.jsx`. |
| `src/main.jsx` | React bootstrap. | Renders `App` in `BrowserRouter` and `StrictMode`. | `App.jsx`, `styles/app.css`. |
| `src/App.jsx` | App shell/routing/session. | `AppContext`, `useApp`, `Shell`, `Protected`; routes `/login`, `/`, `/record`, `/status`, `/meetings/:meetingId`; loads system status. | `api/client.js`, pages. |
| `src/api/client.js` | API client. | `ApiError`, `ApiConnectionError`, session localStorage (`tma_session`), `request`, `download`, `api.*` methods including upload, jobs, speakers, export, audio URL. | All pages. |
| `src/pages/LoginPage.jsx` | Profile selection/creation. | Lists/creates users via `/users`. | `api.listUsers/createUser`, session. |
| `src/pages/DashboardPage.jsx` | Meeting list/search/status. | Displays meetings, filters, navigation to details/record. | `api.listMeetings`. |
| `src/pages/RecordPage.jsx` | Recording/upload flow. | MediaRecorder/browser capture, multi-source options, upload progress, warnings. | `api.uploadRecording/uploadMultiSourceRecording`. |
| `src/pages/MeetingDetailsPage.jsx` | Meeting detail workspace. | Transcript, summary, action items, speaker review, jobs, audio playback, exports, attachments. Has polling/backoff logic; handles `cancelling`/stale warnings. | most `api.*` endpoints. |
| `src/pages/SystemStatusPage.jsx` | System diagnostics UI. | Displays health/system status. | `api.systemStatus/health`. |
| `src/utils/format.js` | Formatting helpers. | Date/time/size/duration formatting. | Pages. |
| `src/styles/app.css` | Global styles. | App layout, components, states. | UI. |
| `Dockerfile` | Frontend container build. | Nginx-based static serving (used in older split deployment patterns). | `nginx/default.conf`. |
| `nginx/default.conf` | Nginx SPA/API proxy config. | Serves static and proxies `/api` as configured. | container deploy. |

### 6.3 `Meeting_analyzer/` root files

| File | Purpose | Key details |
|---|---|---|
| `main.py` | CLI launcher. | Loads `.env`, configures logging, ensures data dirs, runs uvicorn for `api.app:app`; `--check` mode for config validation. |
| `config.py` | Settings/dataclass/env parsing. | Defines data dirs, audio/provider defaults, env var parsing, feature flags (voice profiling, cloud analysis, Groq/Sarvam settings), migration/schema expectations. |
| `alembic.ini` | Alembic config. | `script_location=db/migrations`; URL resolved by env.py. |
| `requirements.txt` | Runtime deps. | FastAPI, SQLAlchemy, Alembic, psycopg, faster-whisper, sarvamai, groq, resemblyzer, soundcard, numpy, python-dotenv, httpx, multipart. |
| `requirements-dev.txt` | Dev/test deps. | Includes pytest and runtime requirements. |
| `pytest.ini` | Pytest config. | `testpaths=tests`, `-q`. |
| `setup.ps1` | Windows setup. | Installs deps/creates env; verify before use. |
| `run.bat` / `scripts/run_server.bat` | Windows run helpers. | Convenience scripts; verify paths/env. |
| `AGENTS.md` / `agents.md` | Agent instructions/architecture rules. | Important constraints: schema isolation, no secrets, LO storage, migration discipline. |
| `README.md` | Backend-specific README. | May overlap with root README; verify freshness. |
| `docker-compose.yml` | Backend-local compose (inside package dir). | Check before use; root compose is primary. |

### 6.4 `Meeting_analyzer/api/`

| File | Purpose | Key symbols | Behavior/connections |
|---|---|---|---|
| `app.py` | FastAPI app assembly. | `create_app`, lifespan, middlewares, router registration, static mount. | Builds engine from env, checks schema current, starts `JobRunner`; mounts `/api` routers and SPA/static. |
| `deps.py` | Dependencies. | `get_engine`, `db_conn`, `current_profile`, `get_speaker_profiler`, `ProfilerDep`. | Reads `X-Profile-Id`; no host-token gating in current code. |
| `errors.py` | Error envelope/handlers. | `ApiError`, `not_found/bad_request/conflict`, handlers for validation/HTTP/SQLAlchemy/Integrity. | Returns `{"error":{"code","message"}}`; IntegrityError currently generic 409. |
| `schemas.py` | Pydantic models. | `MeetingOut`, `JobOut`, `TranscriptOut`, `SummaryOut`, `ActionItemOut/Patch`, `MeetingSpeakerOut`, etc. | Shapes API responses; `JobOut` includes reliability fields. |
| `routers/health.py` | `/health`. | DB check, schema version, read-only detection, stale job count, worker health. | Returns 503 when unhealthy; includes reasons. |
| `routers/system.py` | `/system/status`. | DB size, meeting count, last backup, disk free. | Reads `backup_runs`. |
| `routers/users.py` | Profile CRUD-lite. | list/create users. | `meeting_analyzer.users`. |
| `routers/meetings.py` | Meeting CRUD/ingest. | list/get/delete, create from upload, multi-source. | Uses `services.ingest`; stores notes; returns 409 on DB integrity via handler. |
| `routers/files.py` | File listing/stream/download/attachments. | Range streaming from LO, attachment validation, delete attachment. | Uses `db.lo`, attachment constraints from models/constants. |
| `routers/transcripts.py` | Transcript list and re-transcribe. | checks audio exists, queues transcription job. | Job fields include reliability metadata. |
| `routers/summaries.py` | Summary/action items/analyze. | validates analysis provider configured; queues analysis; patches action items and sync mapping. | `ai.provider_factory`, Task Manager `public.tasks` mapping awareness. |
| `routers/jobs.py` | Job list/cancel/requeue. | cancel queued->cancelled, running->cancelling; requeue failed/cancelled only; active conflict checks. | Cooperative cancellation model. |
| `routers/speakers.py` | Speaker review/profiles/references. | list/assign speakers, profiling enqueue, profile CRUD, enroll audio, reference list/delete, reference audit. | `services.speaker_profiles.SpeakerProfileService`. |
| `routers/metrics.py` | Metrics endpoints. | Exposes transcript metrics/golden metrics endpoints (inspect for exact routes). | `services.transcript_metrics/golden_metrics`. |
| `routers/search.py` | Search. | ILIKE over transcript/summary/action items. | Deterministic MVP search. |

### 6.5 `Meeting_analyzer/ai/`

| File | Key classes/functions | Purpose |
|---|---|---|
| `transcription.py` | `TranscriptSegment`, `TranscriptionResult`, `TranscriptionProvider`, `UnconfiguredTranscriptionProvider`, `TranscriptionService`, `_normalise_segments` | Provider contract; validation; normalization; postprocess dedupe; no engine import. |
| `summarization.py` | `AnalysisProvider`, `UnconfiguredAnalysisProvider`, `MeetingSummarizer`, `SummarizationError` | Prompting, JSON parse/validation, grounding/downgrade retry behavior. |
| `provider_factory.py` | `build_transcription_provider`, `build_analysis_provider`, `describe_analysis_mode`, `ProviderConfigurationError` | Selects providers from settings; Groq requires explicit cloud opt-in. |
| `fallback_provider.py` | `FallbackTranscriptionProvider` | Sarvam primary, Whisper fallback once; records fallback provenance; cancellation not fallback. |
| `segment_postprocess.py` | `normalize_for_comparison`, `sort_segments`, `dedupe_consecutive_segments`, `postprocess_segments` | Fixes order/echo duplication before storing transcript. |
| `task_extractor.py` | `parse_model_json`, `validate_analysis`, `find_grounding_issues`, `downgrade_ungrounded`, `deadline_is_plausible`, `AnalysisValidationError` | Strict schema validation and anti-hallucination safeguards. |
| `sarvam_provider.py` | Sarvam adapter (inspect for class name/methods). | Sends audio to Sarvam; batch API, diarization/language hints; cloud provider. |
| `faster_whisper_provider.py` | Faster-Whisper adapter. | Local transcription; model config from settings. |
| `groq_provider.py` | Groq analysis adapter. | Cloud analysis with opt-in; model/key from env. |

### 6.6 `Meeting_analyzer/db/`

| File | Purpose | Key details |
|---|---|---|
| `base.py` | Declarative base/schema. | `MEETING_ANALYZER_SCHEMA="meeting_analyzer"`, naming convention, `Base.metadata` schema-bound. |
| `models.py` | ORM tables. | `users`, `meetings`, `meeting_files`, `transcripts`, `summaries`, `action_items`, `processing_jobs`, `backup_runs`, speaker/profile/reference/assignment tables, `meeting_task_sync`. Includes checks, indexes, LO trigger DDL/listener. |
| `session.py` | Engine and schema guard. | Resolves DB URL from env; `build_engine`; `ensure_schema_current` compares Alembic head vs DB; refuses missing/newer schema. |
| `lo.py` | Large Object helpers. | `create_lo_from_*`, `open_lo`, `copy_lo_to_stream`, `read_lo_range`, `unlink_lo`; chunked streaming; SHA256. |
| `repositories/__init__.py` | Repository package marker/minimal helpers. | Check before adding repository abstractions. |
| `migrations/env.py` | Alembic env. | Uses `meeting_analyzer.alembic_version`, `include_schemas=True`, URL from env/x-arg. |
| `migrations/script.py.mako` | Migration template. | Standard Alembic template. |
| `migrations/versions/0001-0009*.py` | Schema evolution. | See [Migration Guide](#9-database-and-migration-guide). |

### 6.7 `Meeting_analyzer/services/`

| File | Key symbols | Purpose |
|---|---|---|
| `ingest.py` | `ingest_recording`, `ingest_multi_source_recording`, `normalize_uploaded_audio`, `IngestError` | Convert/validate audio, store canonical Opus LO, queue transcription, cleanup temp files. |
| `pipeline.py` | `TranscriptionPipeline`, `PipelineError`, retry/error helpers, `_materialize_processing_audio`, `_decode_audio_to_wav` | Executes jobs; handles retries, cancel, heartbeat, terminal status, transcript/summary persistence, profiling/analysis chaining. |
| `runner.py` | `JobRunner`, `STALE_RUNNING_SECONDS`, claim/recover/requeue methods | Polls queued jobs, claims with SKIP LOCKED, stale recovery, read-only backoff, worker health. Contains known stale-recovery SQL ambiguity bug. |
| `speaker_profiles.py` | `SpeakerProfileService`, `SpeakerProfilingError`, matching/diagnostics/reference methods | Reference-only embedding matching, diagnostics persistence, assignment provenance, enroll/reference audit. |
| `task_sync.py` | `sync_action_items_to_tasks`, `TaskSyncError` | Idempotent sync to Task Manager `public.tasks`; MA mapping in `meeting_task_sync`. |
| `export.py` | `export_meeting_zip` | Streams meeting artifacts and LOs into ZIP. |
| `transcript_metrics.py` | `compute_transcript_metrics` | Post-transcription metrics from stored transcript/job/audio. |
| `golden_metrics.py` | metrics helpers | Evaluation/golden metrics support. |

### 6.8 `Meeting_analyzer/scripts/`

| File | Purpose | Key behavior |
|---|---|---|
| `backup_database.py` | pg_dump custom-format backup. | Finds pg_dump matching server major, creates `backup_runs` row, stores dump, updates status. Never echoes DB URL/password. |
| `restore_database.py` | pg_restore destructive restore. | Requires `--yes`; `--clean --if-exists --no-owner`; handles benign version-mismatch SET errors. |
| `migrate_legacy_public_to_meeting_analyzer.py` | Legacy data move helper. | For pre-schema/public legacy tables; review carefully before use on shared DB. |
| `evaluate_golden.py`, `golden_metrics.py` | Evaluation tooling. | Golden-set metrics/evaluation workflows. |
| `run_server.bat` | Windows server launcher. | Convenience script. |

### 6.9 `Meeting_analyzer/tests/`

| File | Scope | Notes |
|---|---|---|
| `conftest.py` | Global fixtures. | Loads `TEST_DATABASE_URL` only; creates scratch DB via pg_harness; truncates before tests; sets isolated env. |
| `pg_harness.py` | PostgreSQL test harness. | Creates/drops scratch DB, migrates to head, truncates tables and LOs. |
| `support.py` | Test utilities. | WAV factories/helpers. |
| `test_db_schema.py` | DB schema/constraints/indexes. | PostgreSQL tier. |
| `test_lo_lifecycle.py` | LO create/stream/unlink behavior. | PostgreSQL tier. |
| `test_api.py` | API behavior. | PostgreSQL tier; includes transcription flow. |
| `test_ingest_pipeline.py` | Ingest + pipeline. | PostgreSQL tier; includes Opus compatibility/ingest tests. |
| `test_speaker_api.py` | Speaker/profile/reference/matching endpoints. | PostgreSQL tier; heavy behavior assertions. |
| `test_speaker_profiles.py` | Speaker profiling service unit/integration behavior. | Reference-only matching/diagnostics. |
| `test_task_sync.py` | Task sync mapping behavior. | PostgreSQL tier/integration depending fixtures. |
| `test_metrics_api.py` | Metrics endpoint behavior. | PostgreSQL tier. |
| `test_schema_guard.py` | Schema guard/startup checks. | May use migrations/harness. |
| `test_*provider*.py`, `test_task_extractor.py`, `test_segment_postprocess.py`, etc. | Unit/provider/validation tests. | Mostly no DB required. |
| `test_ui_smoke.py` | UI smoke/build contracts. | May require Node/client build context depending test. |

---

## 7. Classes and Function Reference

> This is a high-value reference map. For exhaustive signatures, inspect the cited files; this section captures critical behavior and call relationships.

### 7.1 Configuration / startup

| Symbol | File | Inputs | Outputs/side effects | Called by |
|---|---|---|---|---|
| `load_settings()` | `config.py` | env vars, optional defaults | `Settings` dataclass with paths/provider flags/audio defaults | `main.py`, `api.app`, providers, services |
| `ensure_directories(settings)` | `config.py` | settings | creates data/log/temp dirs | `main.py`, tests |
| `create_app()` | `api/app.py` | env/config | FastAPI app; mounts routers/static; startup/shutdown hooks | uvicorn import `api.app:app` / `main.py` |
| `_load_local_env()` | `main.py` | `.env` path | loads env vars if present | CLI startup |
| `main()` | `main.py` | argv | runs uvicorn or check mode | `python -m main` |

### 7.2 DB/session/LO

| Symbol | File | Purpose | Key behavior |
|---|---|---|---|
| `database_url_from_env()` | `db/session.py` | Resolve DB URL | Uses `TEAMMEETINGAI_DATABASE_URL`/`DATABASE_URL`; default local fallback. |
| `build_engine()` | `db/session.py` | Create SQLAlchemy engine | Future engine; pool/pre-ping settings. |
| `ensure_schema_current()` | `db/session.py` | Startup guard | Compares DB `meeting_analyzer.alembic_version` vs code head; raises on missing/newer. |
| `create_lo_from_path/bytes` | `db/lo.py` | Store binary in LO | Writes chunks, computes SHA256/size, returns oid/hash/size. |
| `open_lo/read_lo_range/copy_lo_to_stream` | `db/lo.py` | Read LO | Supports streaming and HTTP Range. |
| `unlink_lo` | `db/lo.py` | Manual LO cleanup | Normally trigger handles on row delete. |

### 7.3 API dependencies/errors

| Symbol | File | Purpose |
|---|---|---|
| `db_conn` dependency | `api/deps.py` | Yields SQLAlchemy connection per request/transaction scope. |
| `current_profile`/`ProfileDep` | `api/deps.py` | Resolves `X-Profile-Id` into profile context. |
| `get_speaker_profiler` | `api/deps.py` | Returns app-configured `SpeakerProfileService`. |
| `ApiError` and helpers | `api/errors.py` | Uniform error envelope. |
| exception handlers | `api/errors.py` | Validation 422, HTTPException, IntegrityError->409, SQLAlchemy/Unhandled->500. |

### 7.4 Ingest

| Symbol | File | Key inputs | Output/side effects | Errors |
|---|---|---|---|---|
| `ingest_recording(...)` | `services/ingest.py` | conn, user_id, title, original path, metadata | Creates meeting, converts to Opus, stores `audio_opus` LO+metadata, queues transcription job; deletes temp files. | `IngestError` on validation/ffmpeg/db issues. |
| `ingest_multi_source_recording(...)` | `services/ingest.py` | sources dict, manifest/meta | Validates sources, normalizes/mixes as needed, stores one canonical Opus + manifest metadata; queues transcription. | `IngestError`. |
| `normalize_uploaded_audio(...)` | `services/ingest.py` | uploaded path | ffprobe/ffmpeg normalize/convert; validates duration/codec/decode. | `IngestError`. |

### 7.5 Pipeline/runner

| Symbol | File | Purpose | Key behavior |
|---|---|---|---|
| `JobRunner` | `services/runner.py` | Background worker. | Poll loop, claim queued jobs via `FOR UPDATE SKIP LOCKED`, stale recovery, read-only backoff, health snapshot. |
| `JobRunner.recover_stale_jobs()` | `services/runner.py` | Mark stale running/cancelling jobs failed. | **Known bug:** `exclude_id` param ambiguous when NULL in SQL (`$5`); causes periodic recovery failure and `/health` stale-jobs/unhealthy. |
| `JobRunner._handle_dbapi_exception()` | `services/runner.py` | Classifies DB errors. | Detects read-only and applies backoff. |
| `TranscriptionPipeline.run_job()` | `services/pipeline.py` | Executes one claimed job. | Dispatches transcription/analysis/profiling; heartbeat/progress/cancel checks; retry/failure handling. |
| `_materialize_processing_audio()` | `services/pipeline.py` | Makes provider-usable temp audio. | Prefers `audio_opus`; copies LO to temp; decodes Opus to temp WAV via ffmpeg when needed. |
| `_is_transient_error/_error_code_for/_user_message_for` | `services/pipeline.py` | Retry classification. | Transient errors retry up to max attempts with backoff; permanent fail immediately; read-only treated transient. |
| `_apply_meeting_status_after_failure()` | `services/pipeline.py` | Preserve valid results. | If summary+transcript exist keep completed; if transcript exists keep transcribed; else failed. |

### 7.6 Speaker profiling

| Symbol | File | Purpose | Key behavior |
|---|---|---|---|
| `SpeakerProfileService` | `services/speaker_profiles.py` | Profile/reference/matching service. | Reference-only matching from `speaker_reference_embeddings`; threshold 0.70 inclusive; ambiguity margin 0.05 strict; diagnostics persistence (`all_scores`, best/second/gap/reason). |
| `list_meeting_speakers()` | `services/speaker_profiles.py` | API payload for meeting speaker rows. | Includes assignment status/origin, diagnostics, same-voice labels, candidates/scores. |
| `assign_speaker()` | `services/speaker_profiles.py` | Confirm/reject/reset assignment. | Preserves manual decisions; auto/suggested recalculated by profiling logic. |
| `enroll_profile_audio()` | `services/speaker_profiles.py` | Add reference embedding. | Stores in `speaker_reference_embeddings`; filename/original metadata preserved (recent fix). |
| `audit_reference_embeddings()` | `services/speaker_profiles.py` | Read-only integrity audit. | Used by `/profiles/reference-audit`. |

### 7.7 AI contracts/providers

| Symbol | File | Purpose |
|---|---|---|
| `TranscriptionService.transcribe()` | `ai/transcription.py` | Validates file, calls provider, normalizes segments, dedupes, rebuilds text. |
| `FallbackTranscriptionProvider.transcribe()` | `ai/fallback_provider.py` | Primary then one fallback; records fallback_from/reason. |
| `MeetingSummarizer.analyze()` | `ai/summarization.py` | Prompt LLM, parse/validate JSON, retry on validation/grounding issues, downgrade ungrounded fields. |
| `build_*_provider()` | `ai/provider_factory.py` | Provider selection and config validation. |
| `validate_analysis/find_grounding_issues` | `ai/task_extractor.py` | Anti-hallucination/schema safeguards. |

### 7.8 Export/task sync/metrics

| Symbol | File | Purpose |
|---|---|---|
| `export_meeting_zip()` | `services/export.py` | Build ZIP from DB rows and LO streams. |
| `sync_action_items_to_tasks()` | `services/task_sync.py` | Idempotent upsert/sync to `public.tasks`, mapping in `meeting_task_sync`. |
| `compute_transcript_metrics()` | `services/transcript_metrics.py` | Compute stored-data metrics for primary transcript. |

---

## 8. Architecture and Execution Flows

### 8.1 High-level architecture

```mermaid
flowchart TB
  subgraph Client[React Client]
    UI[Pages/Components]
    APIc[api/client.js]
  end
  subgraph API[FastAPI]
    Routers[Routers]
    Deps[Deps/Auth/Profile]
    Err[Error Envelope]
  end
  subgraph Services[Services]
    Ingest[ingest.py]
    Runner[runner.py JobRunner]
    Pipe[pipeline.py]
    Spk[speaker_profiles.py]
    Export[export.py]
    Sync[task_sync.py]
  end
  subgraph AI[AI Providers]
    Sarvam[Sarvam]
    Whisper[Faster-Whisper]
    Groq[Groq]
  end
  subgraph DB[PostgreSQL]
    MA[(meeting_analyzer schema)]
    LO[(Large Objects)]
    Public[(public.tasks - Task Manager)]
  end

  UI --> APIc --> Routers
  Routers --> Deps
  Routers --> Ingest
  Routers --> Pipe
  Routers --> Spk
  Runner --> Pipe
  Pipe --> Sarvam
  Pipe --> Whisper
  Pipe --> Groq
  Pipe --> Spk
  Pipe --> Sync
  Ingest --> MA
  Ingest --> LO
  Pipe --> MA
  Spk --> MA
  Export --> MA
  Export --> LO
  Sync --> MA
  Sync --> Public
```

### 8.2 Audio ingest and processing pipeline

```mermaid
sequenceDiagram
  participant U as User/Client
  participant M as /meetings API
  participant I as ingest.py
  participant F as ffmpeg/ffprobe
  participant DB as PostgreSQL+LO
  participant R as JobRunner
  participant P as pipeline.py
  participant S as STT Provider
  participant A as Analysis Provider

  U->>M: POST /meetings (audio)
  M->>I: ingest_recording()
  I->>F: normalize/convert to Ogg Opus 32kbps mono 16k
  F-->>I: validated opus file
  I->>DB: INSERT meeting + meeting_files(audio_opus LO metadata) + processing_jobs(transcription queued)
  I-->>M: meeting row
  M-->>U: 201 MeetingOut
  R->>DB: claim queued job (FOR UPDATE SKIP LOCKED)
  R->>P: run_job()
  P->>DB: read audio_opus LO
  P->>F: decode temp WAV if provider needs WAV
  P->>S: transcribe (Sarvam, fallback Whisper on failure)
  S-->>P: TranscriptionResult
  P->>DB: store primary transcript, update meeting/job
  P->>DB: queue profiling/analysis as configured
  P->>A: analyze primary transcript
  A-->>P: validated analysis
  P->>DB: store summary/action_items, sync tasks
  P->>F: cleanup temp files
```

### 8.3 Job lifecycle

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> running: claimed
  running --> succeeded: completed
  running --> failed: permanent error / retries exhausted
  running --> queued: transient error + retry backoff
  running --> cancelling: cancel requested
  queued --> cancelled: cancel requested before claim
  cancelling --> cancelled: worker confirms safe checkpoint
  failed --> queued: manual requeue
  cancelled --> queued: manual requeue
```

### 8.4 Complete meeting lifecycle trace

1. **Profile/session:** Client stores selected profile in localStorage; sends `X-Profile-Id` on requests.
2. **Upload:** `RecordPage`/client calls `POST /api/meetings` or `/multi-source`.
3. **Ingest:** `meetings.py` spools upload to temp, validates, calls `ingest_recording*`. `ingest.py` uses ffprobe/ffmpeg to normalize to canonical Ogg Opus (32kbps, mono, 16kHz, VBR, voip), validates non-empty/duration/decodability, stores LO in `meeting_files(kind='audio_opus', codec='opus', bitrate_kbps=32, container='ogg')`, inserts meeting, queues transcription job. Temp files removed.
4. **Claim:** `JobRunner` polls, recovers stale jobs, claims queued job with `FOR UPDATE SKIP LOCKED`, increments attempt, sets running/heartbeat.
5. **Transcription:** `pipeline.py` materializes audio from LO. For Opus, it decodes to temp WAV when needed. `TranscriptionService` calls primary provider via fallback wrapper (Sarvam then Whisper). Segments normalized/deduped. Primary transcript stored; meeting becomes `transcribed`.
6. **Profiling:** profiling job may be queued/processed; `SpeakerProfileService` evaluates embeddings against `speaker_reference_embeddings` only, stores diagnostics and auto/suggested assignments; manual confirmed/rejected preserved.
7. **Analysis:** if configured and transcript exists, analysis job runs `MeetingSummarizer` with Groq; validates JSON/grounding; stores summary and action items; may sync to `public.tasks`. Meeting becomes `completed`.
8. **Review/export:** MeetingDetails polls jobs, shows transcript/summary/speakers/action items; user can confirm/reject speakers, patch action items, download/export.
9. **Delete:** `DELETE /meetings/{id}` hard-deletes meeting; cascades remove rows; `meeting_files` trigger unlinks LOs.

---

## 9. Database and Migration Guide

### 9.1 Database technology and connection

- **DB:** PostgreSQL. Local docker default uses `postgres:16-alpine`; production/shared deployment can use Supabase PostgreSQL.
- **Connection env:** `DATABASE_URL` or `TEAMMEETINGAI_DATABASE_URL` (see `db/session.py` and `.env.example`). `.env.example` also documents local docker compose variables.
- **Schema isolation:** all app tables in `meeting_analyzer`. Alembic version table is `meeting_analyzer.alembic_version`. Task Manager remains in `public`.
- **Safety:** never use production DB for tests; tests require `TEST_DATABASE_URL` and create/drop scratch DB.

### 9.2 Important tables

| Table | Purpose | Key fields/constraints |
|---|---|---|
| `meeting_analyzer.users` | App profiles/users. | `id`, `display_name`, timestamps. |
| `meeting_analyzer.meetings` | Meeting metadata. | `id`, `user_id`, `title`, `status`, `recorded_at`, `duration_ms`, `language`, `notes`, capture manifest/mode fields. |
| `meeting_analyzer.meeting_files` | File metadata; binary in LO. | `meeting_id`, `kind`, `lo_oid`, `sha256`, `size_bytes`, mime/audio metadata, `codec/bitrate_kbps/container`. Unique `audio_opus` per meeting (0009). LO unlink trigger on delete. |
| `meeting_analyzer.transcripts` | Transcript versions. | `meeting_id`, `provider`, `model`, `text`, `segments` JSONB, `is_primary`, fallback fields. |
| `meeting_analyzer.summaries` | Analysis summary. | `meeting_id`, `transcript_id`, `overview`, `payload` JSONB, model. |
| `meeting_analyzer.action_items` | Extracted action items. | `meeting_id`, `task`, `owner`, `deadline`, `evidence`, `status`, `position`. |
| `meeting_analyzer.processing_jobs` | Background jobs. | `job_type`, `status`, progress/message/error, provider/model/fallback, attempts/max_attempts, next_run_at, heartbeat, cancel/error detail. Active unique index by meeting+type for queued/running/cancelling. |
| `meeting_analyzer.speaker_profiles` | Known speaker identities. | display name, timestamps. |
| `meeting_analyzer.speaker_embeddings` | Meeting-speaker embeddings and diagnostics. | embedding, best/second scores, gap, threshold, decision/reason, all_scores, provenance. |
| `meeting_analyzer.speaker_reference_embeddings` | Reference-only enrollment embeddings. | profile_id, embedding, source metadata, provider/model. Sole matching source. |
| `meeting_analyzer.speaker_assignments` | User/auto assignment decisions. | meeting/local label/profile mapping, status, origin, confidence. |
| `meeting_analyzer.meeting_task_sync` | Map MA action items to TM tasks. | meeting_id, task_id, extraction key, transcript/position metadata. |
| `meeting_analyzer.backup_runs` | Backup status. | started/finished/status/output/size/error. |

### 9.3 LO/audio storage behavior

- Binary audio/file content stored as PostgreSQL Large Objects; `meeting_files.lo_oid` references LO.
- `db/lo.py` streams reads/writes in chunks (4 MiB) and computes SHA256.
- `meeting_files` BEFORE DELETE trigger unlinks LO in same transaction (`tma_unlink_meeting_file_lo` / migration 0001). Hard delete of meeting cascades file rows and triggers LO unlink.
- New recordings should store only canonical `audio_opus`; legacy WAV kinds remain readable.

### 9.4 Migration history and order

| Revision | Down revision | Purpose | Key effect | Risk/notes |
|---|---|---|---|---|
| `0001_initial` | None | Initial schema/bootstrap | Creates `meeting_analyzer` schema/tables, LO unlink trigger; safety checks for legacy public tables. | Non-destructive downgrade. Stops if legacy MA tables in public. |
| `0002_speaker_profiles` | 0001 | Speaker profiling tables | Adds speaker profile/embedding/assignment tables; expands job_type check to include `profiling`. | Additive. |
| `0003_multi_source_audio` | 0002 | Multi-source metadata | Adds capture manifest/mode; expands meeting_files kind check for source/mixed kinds. | Additive. |
| `0004_meeting_task_sync` | 0003 | TM sync mapping | Creates `meeting_task_sync` mapping to `public.tasks` IDs. | No TM schema changes. |
| `0005_reference_voice` | 0004 | Reference-only matching | Adds `speaker_reference_embeddings`; adds embedding diagnostics/assignment origin; copies legacy enrollment embeddings idempotently. | Preserves original rows. |
| `0006_embedding_match_threshold` | 0005 | Persist threshold | Adds `speaker_embeddings.match_threshold`. | Additive. **Tracked.** |
| `0007_matching_diagnostics` | 0006 | Detailed diagnostics | Adds second-best/gap/margin/reason/all_scores fields. | Additive. **Untracked in working tree.** |
| `0008_job_reliability` | 0007 | Job reliability model | Adds heartbeat/attempt/retry/cancel/error fields; expands status to include `cancelling`; active unique/index updates. | Additive; backfills defaults. **Untracked in working tree.** |
| `0009_audio_opus` | 0008 | Canonical Opus artifact | Adds `audio_opus` kind, codec/bitrate/container cols, unique audio_opus per meeting. | Additive; legacy WAV rows remain valid. **Untracked in working tree.** |

**Current Alembic head (code intent):** `0009_audio_opus`. **Repo status:** 0007–0009 files exist locally but are **not committed/tracked** as of this audit; deploying code that depends on them requires committing/reviewing those migrations first.

### 9.5 Applying migrations safely

1. **Backup first** (especially shared/Supabase DB): `python scripts/backup_database.py`.
2. Confirm `DATABASE_URL` points to intended DB.
3. Run `python -m alembic upgrade head` from `Meeting_analyzer/` (or rely on Docker startup command).
4. Verify `/health` schema version and app startup logs.
5. Never run tests against production DB; use `TEST_DATABASE_URL` scratch DB only.

### 9.6 Local isolated test DB

- Set `TEST_DATABASE_URL` to a Postgres server URL the tests may administer (tests create/drop scratch DBs named like `tma_test_<pid>`). Optional `TEST_DATABASE_ADMIN_DB` defaults to `postgres`.
- `tests/conftest.py` loads only `TEST_DATABASE_URL` from env/.env; it intentionally does **not** fall back to `DATABASE_URL`.
- Harness: `tests/pg_harness.py` creates scratch DB, migrates to head, truncates tables and LOs between tests.

### 9.7 Known schema/migration issues

- 0001-0009 downgrades are intentionally non-destructive; manual reviewed rollback required.
- Any migration on shared Supabase DB must be coordinated; do not apply blindly in production.
- Runtime 409 on `POST /meetings` indicates a DB integrity constraint rejection on deployed Supabase DB; exact constraint not identified in logs because handler is generic. Confirm against actual deployed schema/data.

---

## 10. Audio Pipeline and Opus Implementation

### 10.1 Intended behavior

- New recordings converted immediately to canonical **Ogg Opus, 32 kbps, mono, 16 kHz, VBR, voip application**.
- Validate duration/non-empty/codec/successful decode.
- Store **only** compressed Opus for new recordings (`meeting_files.kind='audio_opus'`).
- Delete temp source/conversion files after successful storage.
- Never persist original/derived WAV for new recordings.
- Transcription/speaker profiling/downstream read stored Opus; if provider requires WAV, decode to temp WAV only and delete immediately.
- Retries reuse stored Opus; no permanent WAVs.
- Existing legacy recordings remain accessible/processable; do not delete/rewrite existing audio.

### 10.2 Implementation status (code inspection)

| Area | Status | Evidence | Notes |
|---|---|---|---|
| Migration 0009 additive | Implemented in code (untracked) | `db/migrations/versions/0009_audio_opus.py` | Adds kind/cols/index; non-destructive downgrade. |
| Model support | Implemented | `db/models.py` includes `audio_opus`, `codec`, `bitrate_kbps`, `container`, index/check updates | Uncommitted. |
| Ingest Opus-only for new recordings | Implemented | `services/ingest.py` constants/normalize/ffmpeg/LO store | Uncommitted; verify edge cases. |
| Multi-source canonical mixed Opus | Implemented | `ingest_multi_source_recording` | Manifest metadata retained. |
| Pipeline temp decode from stored Opus | Implemented | `services/pipeline.py::_materialize_processing_audio`, `_decode_audio_to_wav` | Prefers `audio_opus`. |
| Transcripts/files export/API schema/client playback | Implemented/updated | `routers/files.py`, `schemas.py`, `client/src/api/client.js`, export service | Verify UI playback with Ogg/Opus in target browsers. |
| Legacy WAV compatibility | Intended/present | kind checks include legacy kinds in queries | Needs regression verification on real legacy rows. |
| Frontend playback verification | Not verified in this pass | no E2E/browser run here | Manual verification needed. |
| Full acceptance tests | Partially verified | local DB tests run in prior session; some confirmation reruns incomplete | See [Testing](#14-testing-and-verification-results). |

### 10.3 Opus bitrate evaluation

- Standalone artifacts under `opus_bitrate_evaluation/` (untracked): `results.csv`, `results.json`, scripts/reports.
- Reported outcome (from prior session, not re-run here): 32 kbps met configured criteria (WER delta <= 5.00pp and cosine >= 0.950, heuristic). Treat as evaluation evidence, not production-quality benchmark.

---

## 11. API and Frontend Guide

### 11.1 API conventions

- Base path in unified deployment: `/api` (frontend uses `VITE_API_BASE=/api`).
- Error envelope: `{"error":{"code":"...","message":"..."}}`.
- Auth/profile: `X-Profile-Id` header from client session; host-token gating removed per current code/AGENTS note.
- Mutating endpoints generally expect selected profile where applicable.
- Range requests supported for file streaming (`/files/{id}`).

### 11.2 Key endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness/readiness; 503 on unhealthy. |
| GET | `/system/status` | DB/storage/backup status. |
| GET/POST | `/users` | List/create profiles. |
| GET/POST | `/meetings` | List/create meeting (upload ingest). |
| POST | `/meetings/multi-source` | Multi-source ingest. |
| GET/DELETE | `/meetings/{id}` | Details/delete. |
| GET | `/meetings/{id}/files` | File metadata. |
| POST | `/meetings/{id}/files` | Attachment upload. |
| GET | `/files/{id}`, `/files/{id}/download` | Stream/download. |
| GET/POST | `/meetings/{id}/transcripts`, `/transcribe` | Transcript list/requeue. |
| GET/POST | `/meetings/{id}/summary`, `/analyse` | Summary/analyze. |
| GET/PATCH | `/meetings/{id}/action-items`, `/action-items/{id}` | Action items. |
| GET/POST | `/meetings/{id}/jobs*` | Jobs, cancel, requeue. |
| GET/POST | `/meetings/{id}/speakers*`, `/profiles*` | Speaker review/profile/reference endpoints. |
| GET | `/search` | Content search. |

### 11.3 Frontend integration notes

- `client/src/api/client.js` is the single API wrapper; it handles JSON/FormData, timeout, download blob, and session headers.
- `MeetingDetailsPage.jsx` polls jobs; should handle `queued/running/cancelling/succeeded/failed/cancelled` and show stale/retry info.
- Audio playback uses `api.audioUrl(fileId)` -> `/api/files/{id}`. For Opus-in-Ogg, browser support is generally good in modern Chromium/Firefox; verify Safari/Edge behavior in target environment.
- Vite dev proxy strips `/api` prefix when forwarding to backend.

---

## 12. Configuration and Environment Variables

> **Never commit real secrets.** Use `.env.example` as template. Values below are names/purpose only.

| Variable | Purpose | Used by |
|---|---|---|
| `DATABASE_URL` / `TEAMMEETINGAI_DATABASE_URL` | PostgreSQL SQLAlchemy URL. | `db/session.py`, Alembic env, scripts. |
| `TEST_DATABASE_URL` | Isolated test admin/server URL for scratch DB create/drop. | `tests/conftest.py`, `pg_harness.py`. |
| `TEAMMEETINGAI_PG_*` | Local docker Postgres user/password/db/port. | docker compose. |
| `TEAMMEETINGAI_API_HOST/PORT` | Backend bind/port. | `main.py`, compose. |
| `TEAMMEETINGAI_*_PORT` | Compose port mappings. | compose files. |
| `TEAMMEETINGAI_LOG_LEVEL` | Logging level. | `main.py`/logging setup. |
| `TEAMMEETINGAI_CORS_ORIGINS` | CORS allowed origins. | `api/app.py`. |
| `TEAMMEETINGAI_ANALYSIS_BACKEND` | `none`/`groq` (ollama reserved/unsupported). | `ai/provider_factory.py`. |
| `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS` | Must be `1` for Groq. | provider factory/Groq. |
| `TEAMMEETINGAI_GROQ_API_KEY` | Groq API key (secret). | Groq provider. |
| `TEAMMEETINGAI_GROQ_MODEL/TIMEOUT` | Groq model/timeout. | Groq provider. |
| `SARVAM_API_KEY` / `TEAMMEETINGAI_SARVAM_API_KEY` | Sarvam key (secret). | Sarvam provider/config. |
| `TEAMMEETINGAI_SARVAM_*` | Sarvam model/mode/language settings. | Sarvam provider/config. |
| `TEAMMEETINGAI_WHISPER_*` | Local Whisper model/device/compute/threads/etc. | Faster-Whisper provider. |
| `TEAMMEETINGAI_VOICE_PROFILING` | Enable voice profiling. | config, speaker profiler wiring. |
| `TEAMMEETINGAI_EMBEDDING_SIMILARITY_THRESHOLD` | Match threshold (default 0.70). | speaker profiling. |
| `TEAMMEETINGAI_DATA_DIR` | Local app data dir. | `config.py`. |
| `TEAMMEETINGAI_BACKUP_DIR` | Backup output dir. | backup script/compose mount. |
| `TEAMMEETINGAI_PG_DUMP` / `TEAMMEETINGAI_PG_RESTORE` | Explicit pg tool paths. | backup/restore scripts. |
| `VITE_API_BASE` | Frontend API base. | client build/runtime. |
| `VITE_PROXY_TARGET` | Vite dev proxy target. | `client/vite.config.js`. |

---

## 13. How to Run the Application

### 13.1 Prerequisites

- Docker Desktop / Docker Compose (recommended).
- Or Python 3.11+, Node 20+ for dev split mode, PostgreSQL 16+, ffmpeg installed and on PATH.
- Valid `.env` based on `.env.example`.

### 13.2 Unified Docker (recommended)

1. `cp .env.example .env` and set required values. For local compose DB, set `TEAMMEETINGAI_PG_PASSWORD` and local DB vars; for Supabase, set `DATABASE_URL`.
2. `docker compose up --build`
3. App: `http://localhost:${TEAMMEETINGAI_APP_PORT:-8000}` (serves frontend and `/api`).
4. Verify: `GET /api/health` returns 200 when healthy; UI loads.

**Common issues:** missing ffmpeg in custom image (root Dockerfile includes it), DB URL wrong, schema mismatch, port conflict, stale job recovery bug causing unhealthy health.

### 13.3 Dev Docker (hot reload)

1. `docker compose -f docker-compose.dev.yml up --build`
2. Frontend: `http://localhost:${TEAMMEETINGAI_CLIENT_PORT:-5173}`
3. Backend: `http://localhost:${TEAMMEETINGAI_API_PORT:-8000}`
4. Vite proxies `/api` to server container.

### 13.4 Local backend (without Docker)

From `Meeting_analyzer/` (or repo root with proper PYTHONPATH; prefer `Meeting_analyzer/`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# set DATABASE_URL/TEST_DATABASE_URL as needed in .env
python -m alembic upgrade head
uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
```

Verify: `http://localhost:8000/health`, `http://localhost:8000/docs` (if enabled).

### 13.5 Local frontend

From `client/`:

```powershell
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Set `VITE_PROXY_TARGET` if backend not on default `127.0.0.1:8000`.

### 13.6 Database migrations

From `Meeting_analyzer/` with env loaded:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic heads
```

### 13.7 Backup/restore

```powershell
cd Meeting_analyzer
python scripts/backup_database.py --output-dir ..\backups
python scripts/restore_database.py ..\backups\<file>.dump --yes
```

Restore is destructive. Never point restore at production without approved backup/plan.

---

## 14. Testing and Verification Results

### 14.1 Test layout

- **Harness:** `tests/conftest.py`, `tests/pg_harness.py`, `tests/support.py`.
- **DB opt-in:** `TEST_DATABASE_URL` required; scratch DB created/dropped; never falls back to `DATABASE_URL`.
- **Unit/no-DB:** config/provider/validation/segment/postprocess/golden metrics/task extractor/transcription service/summarization service etc.
- **PostgreSQL tier:** `test_db_schema.py`, `test_lo_lifecycle.py`, `test_api.py`, `test_ingest_pipeline.py`, `test_speaker_api.py`, `test_metrics_api.py`, `test_task_sync.py`, some schema/startup tests.

### 14.2 Commands

```powershell
cd Meeting_analyzer
$env:TEST_DATABASE_URL="postgresql+psycopg://USER:PASS@localhost:5433/postgres"
python -m pytest -q
# Or exclude DB-heavy files for safe unit/smoke pass (as previously used):
python -m pytest -q --ignore=tests/test_api.py --ignore=tests/test_db_schema.py --ignore=tests/test_ingest_pipeline.py --ignore=tests/test_lo_lifecycle.py --ignore=tests/test_schema_guard.py --ignore=tests/test_speaker_api.py --ignore=tests/test_startup.py --ignore=tests/test_ui_smoke.py
```

### 14.3 Verification evidence (from prior session, not rerun in this documentation pass)

> These are **historical session results** reported in conversation context. Do not treat as fresh CI pass on current uncommitted working tree unless rerun.

| Scope | Result | Classification | Notes |
|---|---:|---|---|
| `py_compile` changed backend/tests | PASS | Verified (prior session) | Compile check only. |
| Safe unit/smoke suite (8 DB-heavy files excluded) | 260 collected, exit 0 | Verified earlier; **not rerun** | Prior session; do not rerun unless explicitly requested. |
| DB: `test_ingest_pipeline.py test_api.py test_db_schema.py test_lo_lifecycle.py` | 64 passed, 1 failed (initial) | Historical + follow-up | Initial failure: `test_api.py::TestTranscriptAndAnalysis::test_transcription_flow_creates_primary_transcript` (queued vs succeeded). Fixed by job-ID-specific assertion; later single rerun reported **PASSED**. |
| `TestOpusCompatibility` + `TestAudioOpusMigration` | 7 passed | Verified (prior session) | After retry-test fix. |
| `test_metrics_api.py` | 6 passed | Verified (prior session) | After tight duration `pytest.approx` fix. |
| `test_task_sync.py` | 3 passed | Verified (prior session) | After message-drift-tolerant assertion. |
| `test_speaker_api.py -k "ProfileManagement or ProfilingJob or AutomaticChaining"` | 9 passed, 1 failed, 18 deselected | Historical/partial | Failure: `test_profiling_failure_does_not_fail_transcription` adapted for retry/status semantics; **final confirmation rerun interrupted**. |
| `TestMatchingAndReview` batch | 14 passed, 1 failed | Historical/partial | Failure: `test_manual_assignment_preserved_and_auto_recalculated`; root cause: enroll endpoint discarding original filename (fixed in `api/routers/speakers.py`); **confirmation rerun interrupted**. |
| `TestDiagnosticsAndFalsePositiveRegression` | Not completed | Not run/incomplete | Pending confirmation rerun on isolated `TEST_DATABASE_URL`. |
| Frontend build/E2E | Not verified | Not run | `client/node_modules` missing in environment during prior session. |

### 14.4 Known test caveats

- DB tests create/drop scratch DBs; require Postgres access and `TEST_DATABASE_URL`.
- Some tests were modified in working tree (uncommitted). Results above may not map cleanly to committed HEAD.
- Do not rerun DB-heavy tests against Supabase/production.

---

## 15. Git History and Development Timeline

### 15.1 Branches/remotes examined

| Ref | Observed state |
|---|---|
| `master` (local) | Present at `7188a3c`; tracks `origin/master`. |
| `final-demo` (local, current) | At `dddad56`; ahead of `origin/master` by 5 commits. |
| `origin/master`, `origin/final-demo`, `origin/HEAD -> origin/master` | Present. |
| `main` (local) | **Not present**. No `main` ref found. |

### 15.2 Commit timeline (available history)

| Commit | Date | Author | What changed | Impact |
|---|---|---|---|---|
| `cd55870` | 2026-11-14 | Abhay Shinde | Initial V1 working recording/transcription/analysis. | Product baseline. |
| `6a8a218` | 2026-11-16 | Abhay Shinde | Checkpoint: PostgreSQL/FastAPI client-server architecture. | Major architecture migration. |
| `897da56` | 2026-11-16 | Abhay Shinde | Pipeline progress throttle moved to instance. | Bugfix: avoid cross-job/global throttle state. |
| `1490a98` | 2026-11-16 | Abhay Shinde | DB backup/restore scripts. | Operational safety. |
| `00d4044` | 2026-11-16 | Abhay Shinde | README rewrite for client-server architecture. | Docs alignment. |
| `d16242e` | 2026-11-16 | Abhay Shinde | One-command DB provisioning/docker compose/setup.ps1. | Dev ergonomics. |
| `25c5271` | 2026-11-23 | Abhay Shinde | ASR/analysis error reduction: dedupe, sanity checks, Sarvam language hint, eval metrics. | Quality/reliability. |
| `d8f7aff` | 2026-11-23 | Abhay Shinde | Voice Profiling V1: profiles, local embeddings, review UI. | Speaker identity feature. |
| `7188a3c` | 2026-11-23 | Abhay Shinde | Same-voice merge suggestions, Sarvam speaker-count hint, post-transcription metrics. | Profiling/metrics UX. |
| `bd73082` | 2026-11-24 | root | Final demo: meeting analyzer and speaker profiling updates. | Demo consolidation. |
| `9687c7d` | 2026-11-24 | root | Update meeting analyzer and documentation. | Docs/code sync. |
| `935ccfc` | 2026-11-24 | root | Reinitialized git repo to root folder. | Repo layout/history restructuring; explains single-root history appearance. |
| `dabf23a` | 2026-11-24 | root | Supabase schema isolation and meeting-to-task sync. | Multi-tenant/shared DB safety; TM integration. |
| `dddad56` | 2026-11-26 | Abhay Shinde | Fix reference-only speaker profiling and 70% auto-match threshold. | Matching correctness/safety. |

### 15.3 Uncommitted work (current working tree)

Major uncommitted themes (inspect before commit):

- **Job reliability:** runner heartbeat/stale recovery/read-only backoff, pipeline retry/cancel/error detail, jobs cancel/requeue API, health 503 semantics, migration 0008.
- **Opus storage:** ingest/pipeline/model/schema/api/client updates, migration 0009.
- **Speaker matching diagnostics/profiles:** migration 0007, schema/model/service/API/test updates.
- **Tests:** several DB/API/speaker/metrics/task-sync tests modified.
- **Artifacts:** `opus_bitrate_evaluation/`, synthetic audio/reference, debug text files, `.commandcode/` (should likely not be committed).

---

## 16. Current Project Status

### 16.1 Implemented (code present, audit-reconciled)

| Area | Status | Evidence | Commit/WT state |
|---|---|---|---|
| Schema isolation `meeting_analyzer` + Alembic `meeting_analyzer.alembic_version` | Implemented | `db/base.py`, `db/migrations/env.py`, 0001 | Committed |
| LO audio storage + unlink trigger | Implemented | `db/lo.py`, `db/models.py`, 0001 | Committed |
| Speaker profiling V1 + profiles/embeddings/assignments | Implemented | 0002, `services/speaker_profiles.py`, speakers router/UI | Committed base; uncommitted refinements |
| Multi-source ingest metadata | Implemented | 0003, ingest/meetings router | Committed base; uncommitted Opus-path changes |
| Meeting-to-task sync mapping | Implemented | 0004, `services/task_sync.py`, summaries router | Committed |
| Reference-only speaker matching, 70% threshold, 0.05 ambiguity, diagnostics fields | Implemented | 0005-0007, speaker service/API/schema/tests | 0005-0006 tracked; **0007 untracked**; service/router/tests modified uncommitted |
| Job reliability (heartbeat/attempts/backoff/cancelling/requeue/read-only/health 503) | Implemented in code | 0008, `runner.py`, `pipeline.py`, jobs/health routers, models/schemas | **0008 untracked**; code uncommitted |
| Canonical Opus-only storage for new recordings + legacy WAV compatibility | Implemented in code | 0009, `ingest.py`, `pipeline.py`, files/transcripts/export/schemas/client | **0009 untracked**; code uncommitted |
| 409/IntegrityError diagnosis logging | **Not implemented** | `api/errors.py` returns generic conflict | Open (KI-002/KI-006) |
| Stale recovery SQL ambiguity fix | **Not implemented** | `services/runner.py` still has `:exclude_id IS NULL ...` | Open (KI-001) |

### 16.2 Verified vs unverified (reconciled)

- **Verified (prior session, isolated local `TEST_DATABASE_URL`, not rerun in this audit):** `py_compile` PASS; safe unit/smoke suite 260 collected, exit 0 (earlier, not rerun); DB batch 64 passed/1 failed initial with single-test follow-up PASS (`transcription_flow_creates_primary_transcript` fixed by job-ID-specific assertion); Opus compatibility/migration 7 passed; metrics API 6 passed; task sync 3 passed.
- **Partially verified / interrupted:** speaker batch `-k "ProfileManagement or ProfilingJob or AutomaticChaining"` = 9 passed, 1 failed, 18 deselected (confirmation rerun interrupted after retry/status adaptation); `TestMatchingAndReview` = 14 passed, 1 failed (root cause fixed in `speakers.py` enroll filename preservation; confirmation rerun interrupted).
- **Not run / incomplete:** `TestDiagnosticsAndFalsePositiveRegression`; frontend build/E2E; browser Opus playback; live Supabase 409 constraint identification; post-fix stale-recovery deployment verification.
- **Runtime observed (prior session):** deployed `tma-app` (Supabase, schema `0009_audio_opus`) unhealthy - stale recovery `AmbiguousParameter $5`, `stale_jobs=1`, worker affected; browser `POST /meetings` returned 409.

### 16.3 Missing / Incomplete Functionality Matrix

| Capability | Expected | Current state | Gap / risk | Evidence | Next action |
|---|---|---|---|---|---|
| Stale job recovery | Stale running/cancelling jobs fail after 15 min; `/health` reflects reality | **Broken in deployed runtime** due SQL ambiguity when `exclude_id` NULL | Stale jobs accumulate; health 503; log spam | `services/runner.py`; runtime traceback | Cast `exclude_id` as uuid on both sides; regression test; redeploy; manual requeue per policy. |
| Upload/create meeting (`POST /meetings`) | Successful ingest or actionable error | **409 Conflict observed** on deployed Supabase env | Users blocked from uploading | Browser console + app access log; generic `api/errors.py` | Capture response body + Supabase Postgres constraint log; fix constraint cause; add safe server-side constraint logging. |
| Job reliability migration | 0008 applied wherever reliability code runs | **Migration untracked**; code uncommitted | Schema/code drift, deploy failure/confusion | git status; 0008 file present untracked | Review/commit 0008 with code; apply in controlled envs. |
| Opus storage migration | 0009 applied wherever Opus code runs | **Migration untracked**; code uncommitted | New uploads may fail or mismatch schema | git status; 0009 file present untracked | Review/commit 0009 with code; verify additive apply on Supabase. |
| Speaker diagnostics migration | 0007 applied for diagnostics fields | **Migration untracked** | API/model mismatch possible | git status; 0007 file present untracked | Review/commit 0007 in chain. |
| Speaker DB regression confirmation | Targeted speaker suites pass on isolated test DB | **Incomplete** (2 interrupted confirmation reruns; 1 suite not run) | Matching/profiling regressions may remain | Prior session counts | Rerun `TestMatchingAndReview`, `TestDiagnosticsAndFalsePositiveRegression`, fixed auto-chaining case on isolated `TEST_DATABASE_URL`. |
| Opus end-to-end acceptance | New recordings Opus-only; legacy readable; temp WAV cleanup; playback OK | **Partially implemented, not fully verified** | Storage bloat/regressions/playback issues | code inspection + partial DB tests | Verify no WAV rows for new ingest, temp cleanup, browser playback, legacy WAV path. |
| Frontend build/E2E | Client builds and core flows work | **Not verified** | UI regressions undetected | no node_modules/build run | `npm ci`, `npm run build`, manual smoke. |
| IntegrityError observability | 409s diagnosable without exposing secrets/content | **Missing** | Slow incident response | `api/errors.py` generic handler | Log exception class/DB constraint name/code safely; keep client message generic. |
| Clean release packaging | Reviewed commits/PRs, no debug artifacts | **Dirty working tree** | Accidental leakage/bad deploy | git status includes debug/untracked artifacts | Remove `tmp_spk_fix2.txt`, `.commandcode/`, evaluate opus artifacts; split commits. |

### 16.4 Release/deploy caution

The working tree is not clean. Migrations 0007–0009 are **untracked**, major reliability/Opus/speaker changes are **uncommitted**, and two runtime blockers (KI-001, KI-002) are unresolved. Do not deploy blindly. Split into reviewable changesets, verify on isolated DB, then stage deployment with backup/rollback plan.

---

## 17. Known Issues and Technical Debt

| ID | Severity | Issue | Evidence | Impact | Recommended action |
|---|---|---|---|---|---|
| KI-001 | P0 | Stale job recovery SQL has ambiguous parameter when `exclude_id` is NULL | `services/runner.py` stale recovery query: `(:exclude_id IS NULL OR id <> CAST(:exclude_id AS uuid))`; runtime logs show `psycopg.errors.AmbiguousParameter ... parameter $5` | `/health` can remain unhealthy; stale jobs not recovered; log spam. | Change to `CAST(:exclude_id AS uuid) IS NULL OR id <> CAST(:exclude_id AS uuid)` on both sides; add regression test; redeploy; manually requeue affected terminal jobs only per policy. |
| KI-002 | P0 | `POST /meetings` 409 Conflict on running Supabase deployment | Browser console 409; app log `POST /meetings HTTP/1.1 409 Conflict`; generic IntegrityError handler in `api/errors.py` | Upload/create meeting fails for user; root cause hidden. | Capture response body and Supabase Postgres logs; identify constraint; fix data/schema/code; improve logging (without secrets/content) to include constraint name in server logs. |
| KI-003 | P1 | Health reports stale_jobs=1 on deployed env | `/health` payload showed stale_jobs=1 and schema 0009; stale recovery failing due KI-001. | Deployment unhealthy/readiness blocked. | Fix KI-001, verify recovery behavior, then manually requeue if appropriate. |
| KI-004 | P1 | Speaker DB tests not fully confirmed | Prior session: interrupted reruns in `test_speaker_api.py` batches. | Matching behavior confidence incomplete. | Rerun targeted speaker DB tests on isolated `TEST_DATABASE_URL`. |
| KI-005 | P1 | Frontend build/E2E unverified | node_modules missing; no automated E2E run. | UI regressions possible, especially MeetingDetails/Opus playback. | Run `npm ci && npm run build`; manually verify upload/playback/speaker review/jobs. |
| KI-006 | P2 | Generic IntegrityError response hides actionable detail | `api/errors.py` returns static conflict message. | Harder diagnosis in prod. | Log DB error/constraint name safely; consider structured error code mapping for known constraints. |
| KI-007 | P2 | Repo working tree dirty/uncommitted; 0007-0009 migrations untracked | git status shows many modified/untracked files including required migrations. | Schema/code drift; accidental deploy/commit of debug artifacts. | Triage, split by feature; commit migrations 0007→0008→0009 with dependent code; remove debug artifacts. |
| KI-008 | P2 | Legacy/duplicate docs/config paths | root and `Meeting_analyzer/` both contain compose/readme/env-like files. | Operator confusion. | Document canonical files (root compose/Dockerfile, root README) and deprecate duplicates. |
| KI-009 | P3 | AGENTS/older docs mention outdated storage flow | AGENTS data flow text still mentions WAV 48k/16k ingest in one section. | Misleads future agents. | Update AGENTS/README after Opus rollout verified. |

---

## 18. Recommended Next Steps

### Priority 0 (stability/blockers)

1. **Fix stale-recovery SQL ambiguity** in `services/runner.py`; add regression test; rebuild/redeploy; confirm `/health` returns ok and stale job handled by policy.
2. **Diagnose `POST /meetings` 409** with actual response body and Supabase Postgres constraint/error log; implement targeted fix; add API/DB regression test.

### Priority 1 (verification)

3. On isolated `TEST_DATABASE_URL`, rerun pending speaker DB tests: `TestMatchingAndReview`, `TestDiagnosticsAndFalsePositiveRegression`, and the fixed automatic-chaining failure case. Report exact counts.
4. Run frontend build and manual smoke: login, upload, transcription progress, Opus playback, speaker review, export, cancel/requeue.

### Priority 2 (Opus acceptance)

5. Validate legacy WAV meeting playback/transcription path on existing data (no rewrite).
6. Validate new uploads create only `audio_opus` rows (no original/derived WAV rows) and temp files cleaned.
7. Confirm provider temp-WAV decode path cleans up on success/failure/retry.

### Priority 3 (hygiene)

8. Commit/review migrations `0007_matching_diagnostics`, `0008_job_reliability`, `0009_audio_opus` in chain with their dependent code/tests after verification.
9. Remove debug artifacts from commit candidates (`tmp_spk_fix2.txt`, local wav/reference if not intended, `.commandcode/`).
10. Split uncommitted work into logical commits/PRs with tests.
11. Update AGENTS/README to reflect Opus-only new-ingest behavior and current job lifecycle.

---

## 19. Claude Agent Onboarding Guide

### 19.1 Start here

1. `handover.md` (this file) - current state, risks, architecture.
2. `Meeting_analyzer/AGENTS.md` - hard constraints and conventions.
3. `README.md` - setup/run intent; verify against code.
4. `Meeting_analyzer/api/app.py` - runtime wiring.
5. `Meeting_analyzer/services/pipeline.py` and `services/runner.py` - job lifecycle core.
6. `Meeting_analyzer/services/ingest.py` - audio storage invariants.
7. `Meeting_analyzer/db/models.py` + migrations 0001-0009 - schema truth.
8. `client/src/api/client.js` + `client/src/pages/MeetingDetailsPage.jsx` - frontend contract.

### 19.2 Critical files before changes

| Change type | Files to understand first |
|---|---|
| Audio/storage | `services/ingest.py`, `services/pipeline.py`, `db/models.py`, `db/lo.py`, migrations 0003/0009, `routers/files.py` |
| Jobs/reliability | `services/runner.py`, `services/pipeline.py`, `routers/jobs.py`, `routers/health.py`, migration 0008 |
| Speaker profiling | `services/speaker_profiles.py`, `routers/speakers.py`, migrations 0002/0005/0006/0007, `MeetingDetailsPage.jsx` |
| Analysis/action items | `ai/summarization.py`, `ai/task_extractor.py`, `services/pipeline.py`, `routers/summaries.py`, `services/task_sync.py` |
| DB migrations | `db/base.py`, `db/migrations/env.py`, all versions, `db/session.py`, `tests/pg_harness.py` |

### 19.3 Safe testing rules

- Use `TEST_DATABASE_URL` only for DB tests; never `DATABASE_URL`.
- Do not run migrations against production/Supabase during debugging.
- Prefer targeted tests first, then broader suites.
- Record exact command, environment, and pass/fail/skip counts.

### 19.4 Common regressions

- Stale recovery SQL parameter typing issues (psycopg3 ambiguous NULL).
- Job status/attempt semantics drifting from test expectations.
- LO leaks when deleting files outside trigger path or failing mid-ingest.
- Opus decode temp-file cleanup on exceptions.
- Speaker matching regressions from threshold/ambiguity/reference-source changes.
- Frontend polling/backoff and job-state rendering mismatches.

### 19.5 Conventions to preserve

- Meeting Analyzer schema isolation: `meeting_analyzer`, not `public`.
- Additive migrations; no destructive downgrade in shared DB.
- No secrets in logs/docs/tests.
- Uniform API error envelope.
- Transcripts are untrusted data.
- Hard delete + LO unlink trigger for meeting files.

---

## 20. Glossary

| Term | Meaning |
|---|---|
| LO / Large Object | PostgreSQL binary object storage used for audio/file bytes. |
| `audio_opus` | Canonical compressed audio kind for new recordings (Ogg Opus 32kbps mono 16kHz). |
| Primary transcript | Single transcript row with `is_primary=true` used by analysis/UI. |
| Fallback provenance | `fallback_from`/`fallback_reason` recorded when Whisper fallback used. |
| Reference-only matching | Speaker auto-match uses `speaker_reference_embeddings` only, never meeting/synthetic embeddings. |
| Ambiguity margin | Required top1-top2 score gap; `< 0.05` means ambiguous. |
| Stale job | Running/cancelling job whose heartbeat/update exceeded threshold (15 min). |
| Cooperative cancellation | Running jobs move to `cancelling`; worker confirms `cancelled` at safe checkpoint. |
| Scratch DB | Temporary DB created/dropped by test harness; isolated from dev/prod. |

---

## Appendix A — Project-Owned File Inventory (git-tracked)

> Complete inventory of files returned by `git ls-files` for project-owned paths (root docs/config, client, Meeting_analyzer, compose/docker). Dependency/build/cache directories excluded.

| File | Purpose (concise) |
|---|---|
| `.env.example` | Root env template; no secrets. |
| `.gitignore` | Root ignore rules. |
| `CONTEXT_HANDOFF.md` | Historical handoff/context. |
| `Dockerfile` | Unified image: build client + run FastAPI/Alembic. |
| `Dockerfile.server` | Backend-only image. |
| `README.md` | Root runbook/architecture documentation. |
| `client/.dockerignore` | Client Docker ignore rules. |
| `client/.env.example` | Client env template (`VITE_API_BASE`, proxy target). |
| `client/Dockerfile` | Nginx-based client image for split deploy. |
| `client/index.html` | SPA HTML entry. |
| `client/nginx/default.conf` | Nginx SPA/proxy config. |
| `client/package.json` | Frontend package manifest/scripts. |
| `client/package-lock.json` | Locked frontend dependency graph. |
| `client/README.md` | Client-specific notes. |
| `client/src/App.jsx` | App shell, routing, session/system context. |
| `client/src/api/client.js` | API wrapper, session headers, errors, downloads. |
| `client/src/main.jsx` | React bootstrap. |
| `client/src/pages/DashboardPage.jsx` | Meeting list/search/dashboard. |
| `client/src/pages/LoginPage.jsx` | Profile selection/creation. |
| `client/src/pages/MeetingDetailsPage.jsx` | Meeting detail/review/jobs/export UI. |
| `client/src/pages/RecordPage.jsx` | Recording/upload/multi-source capture UI. |
| `client/src/pages/SystemStatusPage.jsx` | System/health status UI. |
| `client/src/styles/app.css` | Global styles. |
| `client/src/utils/format.js` | Formatting helpers. |
| `client/vite.config.js` | Vite config and `/api` dev proxy. |
| `context.md` | Historical project context. |
| `docker-compose.dev.yml` | Dev compose: db+server+vite client. |
| `docker-compose.yml` | Unified compose: db+app. |
| `Meeting_analyzer/.env.example` | Backend env template. |
| `Meeting_analyzer/.gitignore` | Backend ignore rules. |
| `Meeting_analyzer/README.md` | Backend README. |
| `Meeting_analyzer/agents.md` | Agent rules/architecture constraints. |
| `Meeting_analyzer/ai/fallback_provider.py` | Primary/fallback transcription wrapper. |
| `Meeting_analyzer/ai/faster_whisper_provider.py` | Local Faster-Whisper adapter. |
| `Meeting_analyzer/ai/groq_provider.py` | Groq analysis adapter. |
| `Meeting_analyzer/ai/provider_factory.py` | Provider selection/config validation. |
| `Meeting_analyzer/ai/sarvam_provider.py` | Sarvam transcription adapter. |
| `Meeting_analyzer/ai/segment_postprocess.py` | Segment sort/dedupe normalization. |
| `Meeting_analyzer/ai/summarization.py` | Analysis prompt/validation contract. |
| `Meeting_analyzer/ai/task_extractor.py` | Analysis JSON schema/grounding validation. |
| `Meeting_analyzer/ai/transcription.py` | Transcription contract/service normalization. |
| `Meeting_analyzer/alembic.ini` | Alembic config. |
| `Meeting_analyzer/api/__init__.py` | API package marker. |
| `Meeting_analyzer/api/app.py` | FastAPI app assembly/lifespan/static. |
| `Meeting_analyzer/api/deps.py` | Request dependencies/profile/profiler. |
| `Meeting_analyzer/api/errors.py` | Error envelope and exception handlers. |
| `Meeting_analyzer/api/routers/__init__.py` | Routers package marker. |
| `Meeting_analyzer/api/routers/files.py` | File list/stream/download/attachment endpoints. |
| `Meeting_analyzer/api/routers/health.py` | Health/readiness endpoint. |
| `Meeting_analyzer/api/routers/jobs.py` | Job list/cancel/requeue endpoints. |
| `Meeting_analyzer/api/routers/meetings.py` | Meeting CRUD/ingest endpoints. |
| `Meeting_analyzer/api/routers/metrics.py` | Metrics endpoints. |
| `Meeting_analyzer/api/routers/search.py` | Search endpoint. |
| `Meeting_analyzer/api/routers/speakers.py` | Speaker/profile/reference endpoints. |
| `Meeting_analyzer/api/routers/summaries.py` | Summary/analysis/action-item endpoints. |
| `Meeting_analyzer/api/routers/system.py` | System status endpoint. |
| `Meeting_analyzer/api/routers/transcripts.py` | Transcript/retranscribe endpoints. |
| `Meeting_analyzer/api/routers/users.py` | User/profile endpoints. |
| `Meeting_analyzer/api/schemas.py` | Pydantic request/response models. |
| `Meeting_analyzer/audio/microphone.py` | Microphone capture utilities. |
| `Meeting_analyzer/audio/recorder.py` | Recording orchestration/finalization. |
| `Meeting_analyzer/audio/system_audio.py` | System/loopback capture utilities. |
| `Meeting_analyzer/config.py` | Settings/env parsing/data dirs. |
| `Meeting_analyzer/db/__init__.py` | DB package marker. |
| `Meeting_analyzer/db/base.py` | Declarative base/schema naming. |
| `Meeting_analyzer/db/lo.py` | Large Object helpers. |
| `Meeting_analyzer/db/migrations/env.py` | Alembic environment; meeting_analyzer version table. |
| `Meeting_analyzer/db/migrations/script.py.mako` | Alembic migration template. |
| `Meeting_analyzer/db/migrations/versions/0001_initial.py` | Initial schema + LO trigger. |
| `Meeting_analyzer/db/migrations/versions/0002_speaker_profiles.py` | Speaker profile tables. |
| `Meeting_analyzer/db/migrations/versions/0003_multi_source_audio.py` | Multi-source audio metadata. |
| `Meeting_analyzer/db/migrations/versions/0004_meeting_task_sync.py` | Meeting-to-task sync mapping table. |
| `Meeting_analyzer/db/migrations/versions/0005_reference_voice.py` | Reference-only voice embeddings/diagnostics/provenance. |
| `Meeting_analyzer/db/migrations/versions/0006_embedding_match_threshold.py` | Persist match threshold. |
| `Meeting_analyzer/db/models.py` | ORM models/constraints/indexes/trigger. |
| `Meeting_analyzer/db/repositories/__init__.py` | Repositories package marker. |
| `Meeting_analyzer/db/session.py` | Engine creation and schema-version guard. |
| `Meeting_analyzer/docker-compose.yml` | Backend-local compose variant. |
| `Meeting_analyzer/docs/MULTI_SOURCE_RECORDING.md` | Multi-source implementation status/validation notes. |
| `Meeting_analyzer/integrations/__init__.py` | Integrations package marker. |
| `Meeting_analyzer/integrations/api_client.py` | Backend HTTP client wrapper. |
| `Meeting_analyzer/main.py` | Backend CLI launcher/check mode. |
| `Meeting_analyzer/package-lock.json` | Node lockfile inside backend dir (legacy/tooling context; inspect before use). |
| `Meeting_analyzer/pytest.ini` | Pytest config. |
| `Meeting_analyzer/requirements-dev.txt` | Dev/test dependencies. |
| `Meeting_analyzer/requirements.txt` | Runtime dependencies. |
| `Meeting_analyzer/run.bat` | Windows run helper. |
| `Meeting_analyzer/scripts/backup_database.py` | pg_dump backup + backup_runs tracking. |
| `Meeting_analyzer/scripts/evaluate_golden.py` | Golden evaluation script. |
| `Meeting_analyzer/scripts/golden_metrics.py` | Golden metrics script/helpers. |
| `Meeting_analyzer/scripts/migrate_legacy_public_to_meeting_analyzer.py` | Legacy schema/data migration helper. |
| `Meeting_analyzer/scripts/restore_database.py` | Destructive pg_restore helper. |
| `Meeting_analyzer/scripts/run_server.bat` | Windows server run helper. |
| `Meeting_analyzer/services/__init__.py` | Services package marker. |
| `Meeting_analyzer/services/export.py` | Meeting ZIP export. |
| `Meeting_analyzer/services/golden_metrics.py` | Golden metrics service logic. |
| `Meeting_analyzer/services/ingest.py` | Audio ingest/normalize/Opus storage. |
| `Meeting_analyzer/services/pipeline.py` | Job execution/retry/cancel/processing pipeline. |
| `Meeting_analyzer/services/runner.py` | Background job polling/claim/stale recovery. |
| `Meeting_analyzer/services/speaker_profiles.py` | Speaker profile/reference/matching service. |
| `Meeting_analyzer/services/task_sync.py` | Sync action items to Task Manager public.tasks. |
| `Meeting_analyzer/services/transcript_metrics.py` | Post-transcription metrics. |
| `Meeting_analyzer/setup.ps1` | Windows setup helper. |
| `Meeting_analyzer/skills.md` | Internal skills/notes doc. |
| `Meeting_analyzer/storage/__init__.py` | Legacy storage package marker. |
| `Meeting_analyzer/storage/library.py` | Legacy filesystem/SQLite-era storage abstraction. |
| `Meeting_analyzer/storage/migration.py` | Legacy storage migration helpers. |
| `Meeting_analyzer/tests/conftest.py` | Pytest fixtures; TEST_DATABASE_URL-only DB harness. |
| `Meeting_analyzer/tests/pg_harness.py` | Scratch Postgres create/migrate/truncate helpers. |
| `Meeting_analyzer/tests/support.py` | Test support utilities. |
| `Meeting_analyzer/tests/test_api.py` | API integration tests. |
| `Meeting_analyzer/tests/test_config.py` | Config tests. |
| `Meeting_analyzer/tests/test_db_schema.py` | DB schema tests. |
| `Meeting_analyzer/tests/test_env_file.py` | Env loading tests. |
| `Meeting_analyzer/tests/test_faster_whisper_provider.py` | Faster-Whisper provider tests. |
| `Meeting_analyzer/tests/test_golden_metrics.py` | Golden metrics tests. |
| `Meeting_analyzer/tests/test_groq_provider.py` | Groq provider tests. |
| `Meeting_analyzer/tests/test_ingest_pipeline.py` | Ingest/pipeline/Opus tests. |
| `Meeting_analyzer/tests/test_lo_lifecycle.py` | Large Object lifecycle tests. |
| `Meeting_analyzer/tests/test_metrics_api.py` | Metrics API tests. |
| `Meeting_analyzer/tests/test_provider_factory.py` | Provider factory tests. |
| `Meeting_analyzer/tests/test_recorder_files.py` | Recorder/file tests. |
| `Meeting_analyzer/tests/test_sarvam_provider.py` | Sarvam provider tests. |
| `Meeting_analyzer/tests/test_schema_guard.py` | Schema guard tests. |
| `Meeting_analyzer/tests/test_segment_postprocess.py` | Segment postprocess tests. |
| `Meeting_analyzer/tests/test_speaker_api.py` | Speaker API tests. |
| `Meeting_analyzer/tests/test_speaker_profiles.py` | Speaker profile service tests. |
| `Meeting_analyzer/tests/test_startup.py` | Startup tests. |
| `Meeting_analyzer/tests/test_summarization_service.py` | Summarization tests. |
| `Meeting_analyzer/tests/test_task_extractor.py` | Task extraction/validation tests. |
| `Meeting_analyzer/tests/test_task_sync.py` | Task sync tests. |
| `Meeting_analyzer/tests/test_transcript_metrics.py` | Transcript metrics tests. |
| `Meeting_analyzer/tests/test_transcription_fallback.py` | Transcription fallback tests. |
| `Meeting_analyzer/tests/test_transcription_service.py` | Transcription service tests. |
| `Meeting_analyzer/tests/test_ui_smoke.py` | UI smoke tests. |
| `Meeting_analyzer/tools/windows_capture_agent.py` | Windows local multi-source capture/upload agent. |

## Appendix B — Untracked/Working-Tree Files (relevant, inspect before commit)

| Path | Status | Notes |
|---|---|---|
| `Meeting_analyzer/db/migrations/versions/0007_matching_diagnostics.py` | Untracked | Required for current Opus/reliability stack head chain; review and commit if intended. |
| `Meeting_analyzer/db/migrations/versions/0008_job_reliability.py` | Untracked | Required for 0009 and job reliability code. |
| `Meeting_analyzer/db/migrations/versions/0009_audio_opus.py` | Untracked | Required for Opus storage. |
| `opus_bitrate_evaluation/` | Untracked | Evaluation artifacts/results; keep if useful evidence, exclude from production image. |
| `.commandcode/` | Untracked | Tooling state; do not commit. |
| `clear_synthetic_meeting_10min.wav`, `.txt` | Untracked | Local test artifacts; do not commit unless intentionally added as fixtures with license/provenance clarity. |
| `tmp_spk_fix2.txt` | Untracked | Debug artifact; remove before commit. |

## Appendix C — Validation Checklist (this handover)

- [x] Project-owned folders represented at top level and major subfolders documented.
- [x] Project-owned git-tracked source files enumerated in Appendix A; key files described in detail in sections 5-7.
- [x] Important classes/functions documented with behavior and connections.
- [x] Both `master` and `final-demo` history examined; no local `main` branch present (explicitly stated).
- [x] Run commands based on current compose/Dockerfile/scripts/config; caveats included where verification needed.
- [x] Test results classified as verified/historical/not run/incomplete; no unverified claims of pass.
- [x] No secrets/credential values included.
- [x] Only `handover.md` created/modified by this documentation task.