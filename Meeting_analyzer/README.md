# TeamMeetingAI Backend

`Meeting_analyzer/` contains the **FastAPI backend** for TeamMeetingAI. The sole frontend is the React application in `../client/`.

## Shared Supabase Schema Isolation

- Uses same `DATABASE_URL` as Task Manager.
- Task Manager tables stay in `public` (untouched).
- Meeting Analyzer tables live in `meeting_analyzer` schema.
- Meeting Analyzer Alembic version table: `meeting_analyzer.alembic_version`.
- PostgreSQL Large Objects and `meeting_files` LO unlink trigger are preserved.

## Responsibilities

- FastAPI API (`api/`) for meetings, files, transcripts, summaries, action items, jobs, speakers, metrics, search, and system status.
- PostgreSQL persistence (`db/`) with Large Object audio storage and Alembic migrations.
- Ingestion and background processing (`services/`, `ai/`) for transcription, analysis, profiling, metrics, and exports.
- Recording/management operations are available after profile selection.

## Running

### Backend (local)

```bash
cd Meeting_analyzer
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # set DATABASE_URL and provider keys
alembic upgrade head
uvicorn api.app:app --host 0.0.0.0 --port 8000
```

Or use `python main.py` as a convenience launcher for the API.

### Frontend (React)

```bash
cd ../client
npm install
npm run dev
```

The Vite dev server proxies `/api` to FastAPI. In production, Nginx serves the React build and proxies `/api/` to the backend.

## Docker

From repo root:

```bash
docker compose up --build
```

Services: PostgreSQL, FastAPI backend, React+Nginx frontend.

## Configuration

See `.env.example` for server/provider variables. Key variables:

- `DATABASE_URL` / `TEAMMEETINGAI_DATABASE_URL`
- `TEAMMEETINGAI_SARVAM_API_KEY`
- `TEAMMEETINGAI_ANALYSIS_BACKEND`, `TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS`, `TEAMMEETINGAI_GROQ_*`
- `TEAMMEETINGAI_VOICE_PROFILING*`
- `TEAMMEETINGAI_CORS_ORIGINS`

## Notes

- Legacy desktop UI and internal frontend client were removed; React in `client/` is the only frontend.
- `integrations/api_client.py` is a backend-side HTTP integration wrapper retained for scripts/tests; it is not a user-facing frontend.
