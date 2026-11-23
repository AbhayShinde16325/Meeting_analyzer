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
import time
import uuid
from typing import Iterator

from fastapi import Depends, FastAPI, Query
from fastapi.responses import StreamingResponse

from api import errors
from api.deps import ConnDep
from api.routers import (
    files,
    health,
    jobs,
    meetings,
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
    errors.register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(meetings.router)
    app.include_router(files.router)
    app.include_router(transcripts.router)
    app.include_router(summaries.router)
    app.include_router(summaries.action_router)
    app.include_router(jobs.router)
    app.include_router(search.router)
    app.include_router(speakers.router)
    app.include_router(speakers.profiles_router)
    app.include_router(system.router)
    _include_export(app)

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
        is_encoder_available,
    )

    settings = load_settings()
    profiler = SpeakerProfileService(engine, settings)
    if profiler.enabled and not is_encoder_available():
        logger.warning(
            "Voice profiling is enabled but the optional 'resemblyzer' package "
            "is not installed; profiling jobs will fail until it is added."
        )
    return profiler


def main() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("TEAMMEETINGAI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TEAMMEETINGAI_API_PORT", "8000"))
    uvicorn.run("api.app:app", host=host, port=port)


app = build_app()
