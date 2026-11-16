"""Real HTTP server harness for UI tests.

The PySide6 clients are tested against a live uvicorn server (real HTTP
sockets, real streaming) wired exactly like production: engine, schema guard,
pipeline.  Only the AI providers and the background job runner are faked, so
tests drive the pipeline synchronously through ``server.run_job(job_id)``.

Requires ``TEST_DATABASE_URL`` (the scratch PostgreSQL cluster); the fixture
chain skips cleanly without it.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class UiServer:
    """A running TeamMeetingAI server plus handles for driving its pipeline."""

    base_url: str
    app: Any  # FastAPI instance
    _uvicorn_server: Any = None
    _thread: Any = None
    _patchers: Any = None

    def run_job(self, job_id: str) -> None:
        """Drive one queued job synchronously (same code the runner uses)."""

        # FastAPI closes request-scoped connections (which commit the INSERT)
        # after the response is sent, so a job id can reach the client a few
        # milliseconds before the row is visible on another connection.
        self._wait_for_job(job_id)
        self.app.state.pipeline.run_job(job_id)

    def _wait_for_job(self, job_id: str, timeout: float = 5.0) -> None:
        from sqlalchemy import text

        deadline = time.monotonic() + timeout
        while True:
            with self.app.state.engine.connect() as conn:
                found = conn.execute(
                    text("SELECT 1 FROM processing_jobs WHERE id = :id"),
                    {"id": job_id},
                ).scalar_one_or_none()
            if found is not None:
                return
            if time.monotonic() >= deadline:
                raise LookupError(f"Job {job_id} was never committed.")
            time.sleep(0.02)

    def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        # The provider patches must stay active for the server's whole life:
        # the lifespan (which builds the providers) runs inside the server
        # thread, not inside ``build_app()``.
        if self._patchers:
            for patcher in self._patchers:
                patcher.stop()
            self._patchers = None


class _NullRunner:
    """Stands in for JobRunner so tests drive the pipeline synchronously."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.engine = args[0] if args else kwargs.get("engine")
        self.pipeline = args[1] if len(args) > 1 else kwargs.get("pipeline")

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _build_fake_transcription() -> Any:
    from ai.transcription import TranscriptionService
    from config import load_settings

    class _Provider:
        provider_name = "fake"
        model_name = "fake-model"
        transcript_mode = None

        def transcribe(self, audio_path, *, language=None, progress_cb=None,
                       cancel_cb=None):
            from ai.transcription import TranscriptionResult

            return TranscriptionResult(
                text="Fake primary transcript content.",
                language="en",
                segments=[{"start": 0.0, "end": 1.0,
                           "text": "Fake primary transcript content."}],
            )

    return TranscriptionService(load_settings(), provider=_Provider())


def _build_fake_summarizer() -> Any:
    from ai.summarization import MeetingSummarizer
    from config import load_settings
    from support import valid_analysis

    class _AnalysisProvider:
        model_name = "fake-analysis"

        def analyze(self, *, system_prompt, user_prompt):
            return valid_analysis()

    return MeetingSummarizer(load_settings(), provider=_AnalysisProvider())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_ui_server(database_url: str) -> UiServer:
    """Build the app (providers faked) and serve it over real HTTP.

    The patches stay active until :meth:`UiServer.stop`: ``build_app()`` only
    assembles the FastAPI object, while the providers are constructed by the
    lifespan handler that runs inside the server thread.  Ending the patches
    when ``build_app()`` returns would silently fall back to the real Sarvam /
    Whisper providers.
    """

    import uvicorn

    import api.app as app_module
    from unittest.mock import patch

    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    patchers = [
        patch.object(
            app_module, "_build_transcription_service", _build_fake_transcription
        ),
        patch.object(app_module, "_build_summarizer", _build_fake_summarizer),
        patch.object(app_module, "JobRunner", _NullRunner),
    ]
    try:
        for patcher in patchers:
            patcher.start()
        app = app_module.build_app()
    finally:
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30.0
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            for patcher in patchers:
                patcher.stop()
            raise RuntimeError("Test uvicorn server did not start.")
        time.sleep(0.05)
    return UiServer(
        base_url=f"http://127.0.0.1:{port}", app=app,
        _uvicorn_server=server, _thread=thread, _patchers=patchers,
    )
