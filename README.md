# FNW Meeting Analyzer / TeamMeetingAI

TeamMeetingAI is a client-server meeting recorder and analyzer for employees who attend Google Meet meetings.

The system consists of a React frontend, FastAPI backend, and PostgreSQL database. It supports meeting audio uploads, transcription, AI-powered analysis, speaker review, action-item extraction, and exports.

The application supports a unified Docker deployment in which the React frontend and FastAPI backend run in one application container, alongside a separate PostgreSQL container.

## Architecture

```text
fnw-meeting-analyzer/
│
├── client/                    # Only frontend (React + Vite)
│
├── Meeting_analyzer/          # Backend only
│   ├── api/                   # FastAPI application and routers
│   ├── ai/                    # Sarvam, Whisper and Groq providers
│   ├── db/                    # SQLAlchemy models and migrations
│   ├── services/              # Ingestion and background jobs
│   ├── storage/               # Legacy storage implementation
│   ├── scripts/               # Backend utilities
│   └── tests/                 # Backend tests
│
├── Dockerfile                 # Unified frontend + backend image
├── docker-compose.yml         # PostgreSQL + unified application
├── docker-compose.dev.yml     # Development configuration
├── .env.example               # Environment configuration template
└── README.md
```

### Main components

| Component | Responsibility |
|---|---|
| `client/` | React + Vite frontend |
| `Meeting_analyzer/` | FastAPI backend, AI processing, database and services |
| `Dockerfile` | Builds React and packages it with FastAPI |
| `docker-compose.yml` | Runs PostgreSQL and the unified application |
| `docker-compose.dev.yml` | Enables frontend development and Vite hot reload |
| PostgreSQL | Persistent application data storage |

**Important:** `client/` is the only frontend. `Meeting_analyzer/` contains backend code only.

## Quick Start (Docker — One Command)

### Prerequisites

- Ubuntu 22.04+ or another supported Linux distribution
- Docker Engine
- Docker Compose plugin
- Git
- Sarvam API key
- Groq API key (optional, required for cloud analysis)

Verify Docker installation:

```bash
docker --version
docker compose version
```

### Step 1: Clone the repository

```bash
git clone <YOUR_REPOSITORY_URL>
cd fnw-meeting-analyzer
```

If the repository is already cloned:

```bash
cd ~/fnw-meeting-analyzer
```

### Step 2: Configure environment variables

Create the environment file:

```bash
cp .env.example .env
nano .env
```

Configure the following values:

| Variable | Purpose |
|---|---|
| `TEAMMEETINGAI_PG_USER` | PostgreSQL username |
| `TEAMMEETINGAI_PG_PASSWORD` | Strong PostgreSQL password |
| `TEAMMEETINGAI_PG_DB` | PostgreSQL database name |
| `TEAMMEETINGAI_PG_PORT` | Host-side PostgreSQL port, normally `5433` |
| `DATABASE_URL` | SQLAlchemy PostgreSQL connection string |
| `TEAMMEETINGAI_HOST_TOKEN` | Shared host authorization token |
| `TEAMMEETINGAI_SARVAM_API_KEY` | Sarvam transcription API key |
| `TEAMMEETINGAI_GROQ_API_KEY` | Groq analysis API key |
| `TEAMMEETINGAI_ANALYSIS_BACKEND` | `groq` or `none` |

Generate secure secrets:

```bash
openssl rand -hex 24
openssl rand -hex 32
```

Do not commit `.env` to Git or share database passwords, API keys, or host tokens.

### Step 3: Build and start the application

From the repository root:

```bash
docker compose up -d --build
```

This command:

1. Builds the unified application Docker image.
2. Builds the React frontend using Vite.
3. Packages the frontend build with the FastAPI backend.
4. Starts the PostgreSQL container.
5. Waits for the PostgreSQL health check.
6. Starts the application container.

### Step 4: Verify running containers

```bash
docker compose ps
```

Expected services:

```text
NAME           STATUS
tma-app        Up
tma-postgres   Up (healthy)
```

### Step 5: Access TeamMeetingAI

Application:

```text
http://localhost:8000
```

The same application server serves:

- React frontend
- FastAPI backend
- API endpoints
- System health and status endpoints

API compatibility:

```text
/api/*
```

PostgreSQL:

```text
Host: localhost
Port: 5433
Internal Docker port: 5432
```

The PostgreSQL database should remain internal to the Docker environment unless external access is explicitly required.

### Step 6: Verify application health

```bash
curl http://localhost:8000/health
```

The endpoint should report successful application and database health.

### Step 7: Verify PostgreSQL connectivity

Check PostgreSQL readiness:

```bash
docker compose exec db pg_isready
```

Execute a database query:

```bash
docker compose exec db psql \
  -U tma \
  -d teammeetingai \
  -c "SELECT NOW(), current_database();"
```

List database tables:

```bash
docker compose exec db psql \
  -U tma \
  -d teammeetingai \
  -c "\dt"
```

### Step 8: Verify database connectivity from the application container

```bash
docker compose exec app python -c '
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ["DATABASE_URL"])

with engine.connect() as connection:
    print("Database connected:", connection.execute(text("SELECT 1")).scalar())
    print("Database:", connection.execute(text("SELECT current_database()")).scalar())
    print("Time:", connection.execute(text("SELECT NOW()")).scalar())
'
```

Expected:

```text
Database connected: 1
Database: teammeetingai
Time: ...
```

This verifies actual communication between the FastAPI application container and PostgreSQL.

## Docker Management Commands

| Operation | Command |
|---|---|
| Start services | `docker compose up -d` |
| Build and start | `docker compose up -d --build` |
| Stop services | `docker compose stop` |
| Stop and remove containers | `docker compose down` |
| Restart services | `docker compose restart` |
| View containers | `docker compose ps` |
| Application logs | `docker compose logs -f app` |
| Database logs | `docker compose logs -f db` |
| Enter application container | `docker compose exec app bash` |
| Enter PostgreSQL | `docker compose exec db psql -U tma -d teammeetingai` |

**Important database safety rule:**

Do not use:

```bash
docker compose down -v
```

for normal shutdowns or restarts. The `-v` option removes associated volumes and can permanently delete PostgreSQL data.

### Database persistence

PostgreSQL data is stored in the persistent Docker volume:

```text
tma-pgdata
```

Rebuilding or restarting the application container does not normally delete this volume.

Always take a database backup before performing destructive operations.

## Unified Image Only (Without Docker Compose)

The unified application image can also be built independently:

```bash
docker build -t teammeetingai .
```

Run it with an externally accessible PostgreSQL instance:

```bash
docker run -d \
  --name tma-app \
  --env-file .env \
  -e DATABASE_URL="postgresql+psycopg://USER:PASS@HOST:5432/DB" \
  -p 8000:8000 \
  teammeetingai
```

Replace `USER`, `PASS`, `HOST`, and `DB` with the actual database connection details.

In this deployment, PostgreSQL must already be running and reachable from the application container.

## Development Setup

### Docker development mode

For frontend development with Vite hot reload:

```bash
docker compose -f docker-compose.dev.yml up --build
```

The development override is intended for frontend development while retaining the Docker-based backend and database environment.

### Backend development

```bash
cd Meeting_analyzer

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env

alembic upgrade head

uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
```

### Frontend development

Open another terminal:

```bash
cd client

npm install

npm run dev
```

Vite proxies `/api/*` to `VITE_PROXY_TARGET`.

Default:

```text
http://127.0.0.1:8000
```

The development proxy strips the `/api` prefix before forwarding requests to FastAPI.

In unified Docker production deployment, React and FastAPI are served from the same origin. The backend also supports `/api/*` compatibility routing.

## API / Routing

### Frontend API configuration

React uses:

```text
VITE_API_BASE=/api
```

### Routing behavior

| Environment | Routing |
|---|---|
| React frontend | `VITE_API_BASE` |
| Vite development | Proxy `/api/*` to backend |
| Unified Docker | FastAPI serves React and API |
| Backend API | Routes without `/api` prefix |
| Compatibility middleware | Supports `/api/*` |

### Important endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Application and database health |
| `GET /system/status` | System status and host permissions |
| `GET /users` | List user profiles |
| `POST /users` | Create user profile |
| `GET /meetings` | List meetings |
| `POST /meetings` | Upload meeting recording |
| `/transcripts` | Transcript operations |
| `/summaries` | Meeting summaries |
| `/jobs` | Background job status |
| `/files` | File operations |

Actual HTTP methods and route availability depend on the implemented router.

## Data & Safety

- PostgreSQL schema and Alembic migrations are preserved.
- No destructive database reset is performed during frontend/backend consolidation.
- Meeting audio and files are stored using PostgreSQL Large Objects.
- The original 48 kHz stereo WAV and derived 16 kHz mono WAV are retained.
- Mutating endpoints require host-token authorization.
- Non-host clients operate in read-only mode.
- PostgreSQL is not exposed to the network unnecessarily.
- Database credentials and API keys must not be committed to Git.
- The application uses database transactions for safe ingestion and deletion.

## Core Features

### Meeting recording and ingestion

- Manual meeting recording.
- Microphone and system audio capture.
- Meeting audio upload.
- Atomic audio ingestion.
- PostgreSQL Large Object storage.
- Background transcription job creation.

### Transcription

Primary provider:

```text
Sarvam Saaras v4
```

Configuration:

```text
saaras:v4
```

Features:

- Speech-to-text transcription.
- Transliteration.
- Speaker diarization.
- Language detection.
- Speaker-labelled transcript segments.

Fallback:

```text
Faster-Whisper
```

Exactly one automatic local Faster-Whisper fallback attempt is performed when Sarvam fails.

The transcript and job records preserve provider information and fallback details.

### AI meeting analysis

Analysis uses the Groq chat API.

Features:

- Meeting summaries.
- Key discussion points.
- Action-item extraction.
- Strict JSON schema validation.
- Individual action-item database records.

Analysis is optional and controlled by environment configuration.

### Speaker profiles and review

Voice profiling is optional and uses local Resemblyzer embeddings.

Features:

- Voice-profile enrollment.
- Speaker similarity suggestions.
- Manual speaker confirmation.
- Speaker reassignment.
- Speaker profile creation and renaming.
- Rejection of incorrect matches.
- Detection of potentially duplicated diarization labels.

Voice similarity never automatically establishes a person's identity. A user must confirm the match in the speaker-review interface.

Voice embeddings are computed locally and are not sent to external AI providers.

### Transcript metrics

Each successful transcription computes eight measured metrics:

1. Audio duration.
2. Transcription duration.
3. Real-time factor (RTF).
4. Number of detected speakers.
5. Speaker-labelled speech percentage.
6. Unknown-speaker speech percentage.
7. Segments requiring review.
8. Transcription status and errors.

Metrics are stored in the `transcript_metrics` table for each transcription run.

They are computed from actual transcript segments and job timings rather than fabricated confidence values.

## Background Job Processing

The application uses a background job pipeline.

Processing flow:

```text
Audio Upload
     |
     v
Atomic Ingestion
     |
     v
PostgreSQL Storage
     |
     v
Transcription Job
     |
     v
Sarvam Saaras v4
     |
     +---- Failure ----> Faster-Whisper
     |
     v
Transcript Storage
     |
     v
Analysis Job
     |
     v
Groq Analysis
     |
     v
Summary + Action Items
```

The job runner uses PostgreSQL:

```sql
FOR UPDATE SKIP LOCKED
```

This allows safe job claiming and reduces duplicate processing across workers.

Other capabilities:

- Cooperative job cancellation.
- Progress reporting.
- Throttled status updates.
- Job status polling.
- Failure tracking.

## Database Management

### Current database architecture

The current production deployment uses a single PostgreSQL database.

```text
React Frontend
      |
      v
FastAPI Backend
      |
      v
SQLAlchemy
      |
      v
PostgreSQL
```

Database responsibilities include:

- User profiles.
- Meetings.
- Audio Large Objects.
- Transcripts.
- Speaker profiles.
- Speaker assignments.
- Summaries.
- Action items.
- Background jobs.
- Transcript metrics.
- Backup history.

### Database migrations

Alembic is the authoritative database schema migration system.

Apply migrations:

```bash
cd Meeting_analyzer
alembic upgrade head
```

Check the current revision:

```bash
alembic current
```

View migration history:

```bash
alembic history
```

Do not manually modify production tables without corresponding migration scripts.

## Future Enhancement: Centralized Database Management with Supabase

### Objective

Introduce optional centralized PostgreSQL database management using Supabase.

The existing office/local PostgreSQL deployment should remain supported. Supabase will be an optional centralized database target.

**This is a planned enhancement and is not currently implemented.**

### Proposed architecture

```text
                TeamMeetingAI Clients
                         |
                         v
                  FastAPI Backend
                         |
                         v
                Database Connection Layer
                         |
                         v
                Database Configuration
                         |
               +---------+---------+
               |                   |
               v                   v
        Office PostgreSQL     Supabase PostgreSQL
        Local Database        Central Database
```

### Proposed configuration

Introduce:

| Variable | Purpose |
|---|---|
| `TEAMMEETINGAI_DATABASE_MODE` | `local` or `supabase` |
| `DATABASE_URL` | Local PostgreSQL connection |
| `SUPABASE_DATABASE_URL` | Supabase PostgreSQL connection |
| `TEAMMEETINGAI_DATABASE_SSLMODE` | Database SSL configuration |

Example:

```env
TEAMMEETINGAI_DATABASE_MODE=local

DATABASE_URL=postgresql+psycopg://tma:password@db:5432/teammeetingai

SUPABASE_DATABASE_URL=postgresql+psycopg://user:password@host:5432/postgres

TEAMMEETINGAI_DATABASE_SSLMODE=require
```

Credentials must remain in environment variables or a secure secret manager.

### Database schema comparison

Before centralization, the developer must connect to both databases and compare their actual schemas.

Source:

```text
Office Local PostgreSQL
```

Target:

```text
Supabase PostgreSQL
```

The comparison must include:

- Database schemas.
- Table names.
- Column names.
- Column data types.
- Primary keys.
- Foreign keys.
- Unique constraints.
- Indexes.
- Default values.
- Nullability.
- PostgreSQL extensions.
- Sequences.
- Alembic migration revisions.
- Large Object dependencies.

### Schema reconciliation

Generate a comparison report identifying:

- Tables missing from Supabase.
- Tables missing locally.
- Missing columns.
- Incompatible data types.
- Foreign-key differences.
- Missing indexes.
- Constraint differences.
- Migration revision differences.

Example:

| Database object | Local PostgreSQL | Supabase | Result |
|---|---|---|---|
| users | Present | Present | Compare |
| meetings | Present | Present | Compare |
| transcripts | Present | Missing | Requires migration |
| action_items | Present | Present | Compare |
| transcript_metrics | Present | Missing | Requires migration |
| backup_runs | Present | Present | Compare |

### Migration planning

After comparison, generate a migration plan.

Example:

```text
Database Reconciliation Report

Source: Office PostgreSQL
Target: Supabase PostgreSQL

Detected differences:

[+] transcripts table missing
[+] transcript_metrics table missing
[!] meetings column type mismatch
[!] action_items foreign key mismatch

Recommended operations:

1. Create missing tables.
2. Add missing columns.
3. Resolve incompatible data types.
4. Recreate missing constraints.
5. Validate indexes and sequences.

Status: Awaiting approval
```

No destructive changes should be applied automatically.

### Schema migration

Use Alembic as the authoritative migration mechanism.

Requirements:

- Generate migration scripts based on schema differences.
- Review migrations before execution.
- Apply migrations in dependency order.
- Preserve compatibility with application code.
- Record migration revisions.
- Support rollback where possible.
- Require approval for potentially destructive changes.

### Data migration

After schema reconciliation, migrate existing office data.

The migration process must:

1. Back up the local database.
2. Back up the Supabase target if it already contains data.
3. Transfer records in dependency order.
4. Preserve primary keys and foreign keys.
5. Correctly handle sequences.
6. Validate row counts.
7. Verify relationships.
8. Migrate PostgreSQL Large Objects and their references.
9. Record migration progress and failures.

Audio storage requires special consideration. Evaluate whether PostgreSQL Large Objects should remain in use or whether Supabase Storage or another object-storage provider is more suitable.

### Validation and cutover

Before switching production traffic:

- Verify required tables.
- Verify Alembic revision.
- Compare row counts.
- Verify foreign keys.
- Test audio retrieval.
- Test transcript retrieval.
- Test summaries and action items.
- Run PostgreSQL-backed automated tests.
- Verify backups and restoration.

Only switch the application to Supabase after successful validation.

### Proposed implementation modules

```text
Meeting_analyzer/
    db/
        connection.py
        models.py
        schema_inspector.py
        schema_comparator.py
        migration_planner.py
        migration_executor.py
        data_migrator.py
        validation.py

    scripts/
        compare_databases.py
        generate_migration_plan.py
        migrate_to_supabase.py
        validate_central_database.py
```

These are proposed modules, not existing implemented files.

### Safety requirements

- Local PostgreSQL remains the default.
- Supabase support is opt-in.
- Never delete the office database during migration.
- Never automatically drop existing tables or columns.
- Back up both databases before migration.
- Require operator approval for schema changes.
- Prevent duplicate records during retries.
- Avoid uncontrolled dual writes.
- Maintain one authoritative database at a time.
- Use encrypted database connections.
- Protect credentials and connection strings.

## Backups

Database backups use custom-format `pg_dump` files.

The backup includes PostgreSQL Large Objects.

### Create backup

```powershell
.\.venv\Scripts\python.exe scripts\backup_database.py
```

### Restore backup

```powershell
.\.venv\Scripts\python.exe scripts\restore_database.py backups\teammeetingai-YYYYMMDD-HHMMSS.dump --yes
```

Backup configuration:

```text
TEAMMEETINGAI_BACKUP_DIR
```

Backups are stored in:

```text
backups/
```

Each backup attempt is recorded in `backup_runs` and surfaced through:

```text
GET /system/status
```

Restore uses destructive PostgreSQL operations (`--clean --if-exists`) and requires explicit `--yes` confirmation.

Always verify a restore in a scratch database before trusting it.

## Privacy and Consent

- Recording requires explicit user action.
- A visible recording indicator and elapsed timer are maintained.
- Participants must consent before recording.
- Audio is sent to Sarvam for transcription when configured.
- Transcripts are sent to Groq for analysis when cloud analysis is enabled.
- Faster-Whisper provides a local transcription fallback.
- With `TEAMMEETINGAI_ANALYSIS_BACKEND=none`, no analysis is sent to Groq.
- PostgreSQL is not exposed to the network unnecessarily.
- Non-host clients are read-only.
- Logs must not contain audio, transcripts, tokens, or API keys.
- Spoken instructions in transcripts are treated as untrusted data.
- AI-generated results are validated before database insertion.

## Requirements

### Server

- Ubuntu 22.04+ or supported Linux distribution.
- Docker Engine and Docker Compose plugin.
- PostgreSQL 14+ (when running PostgreSQL outside Docker).
- Python 3.11 for non-Docker backend development.
- Sarvam API key.
- Groq API key and model when cloud analysis is enabled.
- `pg_dump` and `pg_restore` client tools for database backups.

### Client

- Windows 10 or Windows 11.
- Python 3.11.
- Working microphone.
- Working audio playback device.
- Google Meet audio playback device or headset.

## Configuration Reference

All configuration is environment-based.

Real environment variables take precedence over `.env` values.

### Server configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Local PostgreSQL URL | Database connection |
| `TEAMMEETINGAI_SARVAM_API_KEY` | Blank | Sarvam API key |
| `TEAMMEETINGAI_SARVAM_LANGUAGE_HINT` | Blank | Optional language hint |
| `TEAMMEETINGAI_SARVAM_NUM_SPEAKERS` | Blank | Optional speaker count |
| `TEAMMEETINGAI_HOST_TOKEN` | Blank | Shared host authorization |
| `TEAMMEETINGAI_ANALYSIS_BACKEND` | `none` | Analysis provider |
| `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS` | Unset | Cloud analysis permission |
| `TEAMMEETINGAI_GROQ_MODEL` | Blank | Groq model |
| `TEAMMEETINGAI_GROQ_TIMEOUT` | `60` | Request timeout |
| `TEAMMEETINGAI_GROQ_API_KEY` | Blank | Groq API key |
| `TEAMMEETINGAI_VOICE_PROFILING` | Unset | Enables voice profiling |
| `TEAMMEETINGAI_VOICE_MATCH_THRESHOLD` | `0.80` | Suggested match threshold


## Centralized Database Architecture – Future Supabase Integration

### 1. Objective

The long-term objective is to centralize the database infrastructure used by **FNW Task Manager** and **FNW Meeting Analyzer (TeamMeetingAI)**.

Currently, both applications can operate with their own PostgreSQL database configurations. This creates potential problems:

- Data duplication between applications.
- Task records being stored independently.
- Inconsistent task status and assignment information.
- Synchronization issues when a meeting generates new action items.
- Difficulty maintaining a unified view of organizational tasks.

To solve this, both applications will eventually connect to a **common Supabase-hosted PostgreSQL database** using a centralized PostgreSQL connection URL.

Supabase will act as the shared database infrastructure, while each application will continue to maintain its own backend, business logic, APIs, and responsibilities.

### 2. Proposed Architecture

The proposed architecture uses:

- One Supabase project.
- One centralized PostgreSQL database.
- Two independent backend applications.
- Separate PostgreSQL schemas for each application.
- A controlled integration between meeting-generated action items and Task Manager tasks.

```text
                   APPLICATION LAYER
          ┌──────────────────────────────┐
          │                              │
          │       FNW Task Manager       │
          │                              │
          │       FNW Meeting Analyzer   │
          │                              │
          └──────────────┬───────────────┘
                         │
                  BACKEND SERVICES
                         │
             ┌───────────┴───────────┐
             │                       │
      Task Manager API        Meeting Analyzer API
             │                       │
             └───────────┬───────────┘
                         │
                  SHARED DATABASE URL
                         │
                  SUPABASE POSTGRESQL
                         │
             ┌───────────┴───────────┐
             │                       │
      task_manager schema    meeting_analyzer schema
             │                       │
        employees                  meetings
        tasks                      transcripts
        task_history               speakers
        task_comments              action_items
                                   transcript_metrics
```

Both applications connect to the same PostgreSQL database, but their tables remain organized into separate schemas.

### 3. Database Schema Separation

We will use PostgreSQL schemas to maintain clear ownership of application data.

#### A. `task_manager` schema

This schema will contain the existing Task Manager application data.

Example tables:

- `employees`
- `tasks`
- `task_history`
- `task_comments`
- Other existing Task Manager tables.

The Task Manager will remain the authoritative owner of task records.

#### B. `meeting_analyzer` schema

This schema will contain Meeting Analyzer-specific information.

Example tables:

- `meetings`
- `transcripts`
- `speakers`
- `speaker_profiles`
- `action_items`
- `transcript_metrics`
- Other meeting-analysis tables.

The Meeting Analyzer will remain responsible for storing recordings, transcripts, speaker information, meeting summaries, and extracted action items.

**Important:** These are separate PostgreSQL schemas, not separate databases. Both schemas exist inside the same Supabase PostgreSQL database.

### 4. How Meeting Analyzer Will Create Tasks in Task Manager

This is the most important integration between the two applications.

When a meeting is processed, the Meeting Analyzer can extract action items such as:

- Complete the project documentation.
- Schedule the next client meeting.
- Review the deployment configuration.
- Submit the pending report.

These extracted items will initially be stored in the `meeting_analyzer.action_items` table.

However, an extracted action item should not automatically become an independent Task Manager task without validation.

The proposed workflow is:

1. Meeting Analyzer processes the meeting recording.
2. AI extracts action items from the transcript.
3. Extracted action items are stored in `meeting_analyzer.action_items`.
4. The user reviews and confirms the action items.
5. The system identifies the responsible employee using the available employee information.
6. The confirmed action item is submitted to the Task Manager integration.
7. Task Manager validates the information and creates the canonical record in `task_manager.tasks`.
8. The created Task Manager task ID is stored against the corresponding meeting action item.

Conceptually:

```text
Meeting Recording
       |
       v
Transcription and AI Analysis
       |
       v
Extract Action Items
       |
       v
meeting_analyzer.action_items
       |
       v
User Review and Confirmation
       |
       v
Task Manager Integration
       |
       v
Validate Employee and Task Details
       |
       v
task_manager.tasks
       |
       v
Return Task ID
       |
       v
Link Task ID to Meeting Action Item
```

This ensures that Task Manager remains the single source of truth for organizational tasks.

### 5. Common Supabase Connection URL

Both applications will eventually use the same Supabase PostgreSQL project.

The connection URL will be configured through environment variables rather than hardcoded into application code.

Example configuration:

**FNW Task Manager `.env`:**

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE
DB_SCHEMA=task_manager
```

**FNW Meeting Analyzer `.env`:**

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE
DB_SCHEMA=meeting_analyzer
```

Both connection URLs will point to the same Supabase PostgreSQL database. The schema configuration determines which application's tables are managed by its backend.

The actual connection URL and credentials must be obtained from the Supabase project dashboard.

**Security requirements:**

- Never commit the actual Supabase connection URL to GitHub.
- Keep database credentials in server-side environment variables.
- Do not expose database credentials through React or Vite environment variables.
- Use SSL for database connections.
- Prefer separate database roles and schema-level permissions for the two applications.
- Use a restricted runtime database role rather than the Supabase administrative credentials.
- Use a separate migration-capable role for schema changes.

### 6. Existing Database Reconciliation and Migration

Before connecting both applications to Supabase, we must inspect the existing PostgreSQL databases.

This is necessary because Task Manager and Meeting Analyzer may already contain tables with overlapping names, different column definitions, different data types, or different relationships.

We must not simply copy both databases into Supabase without reconciling their schemas.

#### Phase 1: Inspect existing databases

Export the existing database schemas and inspect:

- Table names.
- Column names and data types.
- Primary keys.
- Foreign keys.
- Unique constraints.
- Indexes.
- Relationships.
- Migration history.
- Existing records.
- PostgreSQL Large Objects, where applicable.

Example commands:

```bash
pg_dump --schema-only --no-owner --no-privileges \
  -d TASK_MANAGER_DATABASE_URL \
  -f task_manager_schema.sql
```

```bash
pg_dump --schema-only --no-owner --no-privileges \
  -d MEETING_ANALYZER_DATABASE_URL \
  -f meeting_analyzer_schema.sql
```

The actual connection strings should be supplied through secure environment variables or an appropriate PostgreSQL connection configuration.

#### Phase 2: Schema reconciliation

Compare both schemas and identify:

- Duplicate table names.
- Conflicting column definitions.
- Duplicate employee or user records.
- Different task status representations.
- Incompatible primary key types.
- Foreign key dependencies.
- Tables that should remain application-specific.
- Tables that should be shared through an integration boundary.

The existing Task Manager schema will be treated as authoritative for organizational task data.

The Meeting Analyzer schema will be treated as authoritative for meeting-specific data.

#### Phase 3: Create the centralized Supabase schemas

Create the two application schemas:

```sql
CREATE SCHEMA IF NOT EXISTS task_manager;

CREATE SCHEMA IF NOT EXISTS meeting_analyzer;
```

Then apply the reconciled table definitions using the respective application's database migrations.

#### Phase 4: Migrate existing records

Migrate the existing data into the appropriate schemas.

For example:

| Existing application | Destination |
|---|---|
| Task Manager employees | `task_manager.employees` |
| Task Manager tasks | `task_manager.tasks` |
| Meeting Analyzer meetings | `meeting_analyzer.meetings`