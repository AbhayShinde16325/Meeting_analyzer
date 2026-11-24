# TeamMeetingAI React Client

React/Vite frontend for the existing FastAPI + PostgreSQL backend.

## Dev (local)

```bash
cd client
npm install
npm run dev
```

Vite proxies `/api` to `http://localhost:8000` by default.

## Build

```bash
npm run build
npm run preview
```

## Docker

From repo root:

```bash
docker compose up --build
```

Client: `http://localhost:5173` (Nginx serves build, proxies `/api` to `server:8000`).

## Environment

- `VITE_API_BASE` - API base path/URL (default `/api`)
- `VITE_PROXY_TARGET` - Vite dev proxy target (default `http://localhost:8000`)

## Notes

- Recording uses browser microphone MediaRecorder and uploads to existing `/meetings` ingest endpoint.
- Select/create a profile to record and manage meetings.
- Backend behavior/DB schema unchanged.
