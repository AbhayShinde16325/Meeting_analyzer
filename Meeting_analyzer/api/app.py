"""FastAPI application assembly.

Startup (lifespan):
  1. Build the SQLAlchemy engine from the environment (.env: DATABASE_URL).
  2. Refuse to start when the schema is missing or newer than this code.
  3. Start the background job runner (processing queue worker).

Routes depend on the engine through ``app.state``; PostgreSQL itself is never
exposed to the network - this API is the only database access layer.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import time
import uuid
from typing import Iterator

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from api import errors
from api.deps import ConnDep
from api.routers import (
    files,
    health,
    jobs,
    meetings,
    metrics,
    search,
    speakers,
    summaries,
    system,
    transcripts,
    users,
)
from db.session import build_engine, database_url_from_env, ensure_schema_current
from services.pipeline import TranscriptionPipeline
from services.runner import JobRunner

logger = logging.getLogger(__name__)

# Under `python -m uvicorn` uvicorn configures only its own loggers, so app
# INFO logs (startup timings) would be lost. Configure the root logger once,
# unless the embedding process already did.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s: %(message)s",
    )
    logging.getLogger("alembic").setLevel(logging.WARNING)


def build_app() -> FastAPI:
    app = FastAPI(
        title="TeamMeetingAI Server",
        version="0.2.0",
        description=(
            "LAN-hosted meeting server. Recordings, transcripts and summaries "
            "are stored in PostgreSQL on the host machine. Transcription runs "
            "through Sarvam (cloud): audio is uploaded to Sarvam's API. "
            "Participant consent for cloud transcription is assumed to have "
            "been obtained by the operator."
        ),
    )
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get(
            "TEAMMEETINGAI_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        ).split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Length", "X-File-SHA256"],
    )
    errors.register_error_handlers(app)
    _install_api_path_compat(app)

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(meetings.router)
    app.include_router(files.router)
    app.include_router(transcripts.router)
    app.include_router(summaries.router)
    app.include_router(summaries.action_router)
    app.include_router(jobs.router)
    app.include_router(search.router)
    app.include_router(metrics.router)
    app.include_router(speakers.router)
    app.include_router(speakers.profiles_router)
    app.include_router(system.router)
    _include_export(app)
    _mount_static_client(app)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> Iterator[dict]:
        started = time.perf_counter()
        engine = build_engine(database_url_from_env())
        logger.info("Startup: engine built in %.1fs.", time.perf_counter() - started)
        # Fail fast on a stale or missing schema rather than corrupting data.
        schema_started = time.perf_counter()
        revision = ensure_schema_current(engine)
        logger.info(
            "Startup: schema check (revision %s) took %.1fs.",
            revision,
            time.perf_counter() - schema_started,
        )
        providers_started = time.perf_counter()
        transcription_service = _build_transcription_service()
        summarizer = _build_summarizer()
        speaker_profiler = _build_speaker_profiler(engine)
        logger.info(
            "Startup: providers built in %.1fs.",
            time.perf_counter() - providers_started,
        )
        pipeline = TranscriptionPipeline(
            engine, transcription_service, summarizer, speaker_profiler
        )
        runner = JobRunner(engine, pipeline)
        runner.start()
        logger.info(
            "Startup: job runner started; lifespan took %.1fs total.",
            time.perf_counter() - started,
        )
        app.state.engine = engine
        app.state.job_runner = runner
        app.state.pipeline = pipeline
        app.state.speaker_profiler = speaker_profiler
        try:
            yield {"engine": engine}
        finally:
            runner.stop()
            engine.dispose()

    app.router.lifespan_context = lifespan  # type: ignore[assignment]
    return app


def _include_export(app: FastAPI) -> None:
    from fastapi import APIRouter

    router = APIRouter(tags=["export"])

    @router.get("/meetings/{meeting_id}/export")
    def export_meeting(
        meeting_id: uuid.UUID,
        conn: ConnDep,
        include_audio: bool = Query(default=True),
    ) -> StreamingResponse:
        from services.export import build_export

        return StreamingResponse(
            build_export(conn, meeting_id, include_audio=include_audio),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="meeting-{meeting_id}.zip"'
            },
        )

    app.include_router(router)


def _build_transcription_service():
    """Wire Sarvam (primary) with the local Whisper fallback."""

    from ai.fallback_provider import FallbackTranscriptionProvider
    from ai.sarvam_provider import SarvamTranscriptionProvider
    from ai.transcription import TranscriptionService
    from config import load_settings

    settings = load_settings()
    primary = SarvamTranscriptionProvider(settings)
    from ai.faster_whisper_provider import FasterWhisperProvider

    return TranscriptionService(
        settings,
        provider=FallbackTranscriptionProvider(primary, FasterWhisperProvider(settings)),
    )


def _build_summarizer():
    """Wire the configured analysis backend (Groq) when present."""

    from ai.provider_factory import build_analysis_provider
    from ai.summarization import MeetingSummarizer
    from config import load_settings

    settings = load_settings()
    try:
        provider = build_analysis_provider(settings)
    except Exception as exc:  # analysis is optional; log loudly, keep serving
        logger.warning("Analysis backend unavailable: %s", exc)
        return None
    if provider is None:
        return None
    return MeetingSummarizer(settings, provider=provider)


def _build_speaker_profiler(engine):
    """Wire the voice-profile service (inert unless enabled + resemblyzer)."""

    from config import load_settings
    from services.speaker_profiles import (
        SpeakerProfileService,
        encoder_import_error,
    )

    settings = load_settings()
    profiler = SpeakerProfileService(engine, settings)
    if profiler.enabled:
        problem = encoder_import_error()
        if problem:
            logger.warning("%s Profiling jobs will fail until it is fixed.", problem)
    return profiler


def _install_api_path_compat(app: FastAPI) -> None:
    """Allow frontend to call /api/* while keeping existing API paths."""

    class _ApiPrefixMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.scope.get("path", "")
            if path == "/api":
                request.scope["path"] = "/"
            elif path.startswith("/api/"):
                request.scope["path"] = path[4:] or "/"
            return await call_next(request)

    app.add_middleware(_ApiPrefixMiddleware)


def _mount_static_client(app: FastAPI) -> None:
    """Serve built React client when TEAMMEETINGAI_STATIC_DIR is set."""

    static_dir = os.environ.get("TEAMMEETINGAI_STATIC_DIR", "").strip()
    if not static_dir:
        return
    root = Path(static_dir)
    if not root.exists():
        logger.warning("Static client dir %s does not exist; API-only mode.", root)
        return
    assets = root / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index_file = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        # Do not swallow unknown API routes.
        if full_path.startswith("api/") or full_path == "api":
            raise errors.not_found("not_found", "Not found.")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and str(candidate).startswith(str(root.resolve())):
            return FileResponse(candidate)
        if index_file.exists():
            return FileResponse(index_file)
        raise errors.not_found("not_found", "Not found.")


def main() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("TEAMMEETINGAI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TEAMMEETINGAI_API_PORT", "8000"))
    uvicorn.run("api.app:app", host=host, port=port)


app = build_app()
