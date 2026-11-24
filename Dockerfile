# Unified TeamMeetingAI image: builds React client + runs FastAPI server
# One command build/run:
#   docker build -t teammeetingai .
#   docker run --env-file .env -p 8000:8000 teammeetingai
#
# Requires DATABASE_URL in env/.env pointing to Postgres.

FROM node:20-alpine AS client-build
WORKDIR /app/client
COPY client/package*.json ./
RUN npm ci
COPY client ./
# Same-origin API; FastAPI serves both /api/* and /* API paths.
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY Meeting_analyzer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY Meeting_analyzer ./Meeting_analyzer
COPY --from=client-build /app/client/dist ./client-dist

ENV TEAMMEETINGAI_STATIC_DIR=/app/client-dist
WORKDIR /app/Meeting_analyzer
EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn api.app:app --host 0.0.0.0 --port 8000"]
