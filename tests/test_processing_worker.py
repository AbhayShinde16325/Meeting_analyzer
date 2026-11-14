"""Processing-worker orchestration tests with fake providers."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt

from ai.summarization import MeetingSummarizer
from ai.transcription import TranscriptionResult, TranscriptionService
from main import MeetingProcessingWorker
from storage import library as ops
from support import (
    BlockingFakeTranscriptionProvider,
    FakeAnalysisProvider,
    FakeTranscriptionProvider,
    valid_analysis,
)


def _run_worker(qapp, db, settings, meeting_id, transcription, analysis, stage="auto"):
    worker = MeetingProcessingWorker(
        db, settings, meeting_id, transcription, analysis, stage=stage
    )
    completed: list[str] = []
    failed: list[str] = []
    progress: list[str] = []
    worker.completed.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.progress.connect(progress.append)
    worker.run()
    return completed, failed, progress


def test_worker_transcribes_and_analyses_recording(
    qapp, db, settings, recorded_meeting
):
    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, progress = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    assert progress

    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "completed"
    assert details["transcript"]["text"].startswith("Alice:")
    assert details["summary"]["overview"]
    assert details["action_items"][0]["task"] == "Ship the release"
    assert len(transcription.calls) == 1
    assert len(analysis.calls) == 1


def test_worker_reuses_existing_transcript(qapp, db, settings, recorded_meeting):
    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )
    transcription = FakeTranscriptionProvider(error=RuntimeError("must not be called"))
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    assert transcription.calls == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["transcript"]["text"] == "Existing transcript"


def test_worker_fails_when_recording_file_is_missing(
    qapp, db, settings, recorded_meeting
):
    (Path(recorded_meeting["meeting_dir"]) / "audio.wav").unlink()
    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == []
    assert failed and "missing" in failed[0].lower()
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"
    assert details["error_message"]


def test_worker_keeps_transcript_when_analysis_fails(
    qapp, db, settings, recorded_meeting
):
    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider(["not valid json"])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == []
    assert failed
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"
    assert details["transcript"] is not None
    assert details["summary"] is None


def test_worker_reports_unconfigured_providers(
    qapp, db, settings, recorded_meeting
):
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings),
        MeetingSummarizer(settings),
    )

    assert completed == []
    assert failed and "No local transcription provider" in failed[0]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"


def test_worker_reports_transcription_provider_failure(
    qapp, db, settings, recorded_meeting
):
    transcription = FakeTranscriptionProvider(error=RuntimeError("engine failed"))
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == []
    assert failed and "Transcription failed" in failed[0]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"
    assert details["transcript"] is None


def test_worker_saves_transcript_without_analysis_backend(
    qapp, db, settings, recorded_meeting
):
    """With no analysis provider, transcription alone ends in 'transcribed'."""

    transcription = FakeTranscriptionProvider()
    completed, failed, progress = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings),  # no analysis provider configured
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    assert progress

    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "transcribed"
    assert details["transcript"]["text"].startswith("Alice:")
    assert details["summary"] is None
    assert details["action_items"] == []


def test_worker_saves_analysis_model_name(qapp, db, settings, recorded_meeting):
    class NamedFakeAnalysisProvider(FakeAnalysisProvider):
        model_name = "groq/test-model"

    transcription = FakeTranscriptionProvider()
    analysis = NamedFakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["summary"]["model"] == "groq/test-model"


def test_worker_analysis_unavailable_keeps_existing_transcript(
    qapp, db, settings, recorded_meeting
):
    """A meeting already transcribed completes cleanly with no analysis."""

    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=FakeTranscriptionProvider()),
        MeetingSummarizer(settings),
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "transcribed"
    assert details["transcript"]["text"] == "Existing transcript"


def test_worker_explicit_transcribe_stage_skips_analysis(
    qapp, db, settings, recorded_meeting
):
    """Stage 'transcribe' never runs analysis, even when one is configured."""

    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
        stage="transcribe",
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    assert analysis.calls == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "transcribed"
    assert details["summary"] is None


def test_worker_explicit_analyse_stage_skips_transcription(
    qapp, db, settings, recorded_meeting
):
    """Stage 'analyse' uses the saved transcript and never touches audio."""

    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )
    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
        stage="analyse",
    )

    assert completed == [recorded_meeting["id"]]
    assert failed == []
    assert transcription.calls == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "completed"
    assert details["summary"]["overview"]


def test_worker_analyse_stage_without_transcript_fails(
    qapp, db, settings, recorded_meeting
):
    transcription = FakeTranscriptionProvider()
    analysis = FakeAnalysisProvider([valid_analysis()])
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=analysis),
        stage="analyse",
    )

    assert completed == []
    assert failed and "no transcript" in failed[0].lower()
    assert transcription.calls == []
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"
    assert details["transcript"] is None


def test_worker_analyse_stage_without_analysis_backend_fails(
    qapp, db, settings, recorded_meeting
):
    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=FakeTranscriptionProvider()),
        MeetingSummarizer(settings),
        stage="analyse",
    )

    assert completed == []
    assert failed and "No analysis backend" in failed[0]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "failed"
    assert details["transcript"] is not None


def test_worker_transcribe_stage_with_existing_transcript_fails(
    qapp, db, settings, recorded_meeting
):
    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=FakeTranscriptionProvider()),
        MeetingSummarizer(settings),
        stage="transcribe",
    )

    assert completed == []
    assert failed and "already has a transcript" in failed[0].lower()
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["transcript"]["text"] == "Existing transcript"


def test_worker_rejects_unknown_stage(qapp, db, settings, recorded_meeting):
    import pytest

    with pytest.raises(ValueError):
        MeetingProcessingWorker(
            db,
            settings,
            recorded_meeting["id"],
            TranscriptionService(settings),
            MeetingSummarizer(settings),
            stage="everything",
        )


# ---------------------------------------------------------------------------
# Re-transcription (force)
# ---------------------------------------------------------------------------


def test_worker_force_retranscribe_replaces_transcript_and_clears_analysis(
    qapp, db, settings, processed_meeting
):
    """force=True replaces the transcript and deletes the stale analysis."""

    transcription = FakeTranscriptionProvider(
        result=TranscriptionResult(
            text="New transcript text.",
            language="en",
            segments=[{"start": 0.0, "end": 1.0, "text": "New transcript text."}],
        )
    )
    completed, failed, _ = _run_worker(
        qapp,
        db,
        settings,
        processed_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings, provider=FakeAnalysisProvider([valid_analysis()])),
        stage="transcribe",
    )
    # Default force is False, so the guarded path refuses instead.
    assert completed == []
    assert failed and "already has a transcript" in failed[0].lower()

    forced_worker = MeetingProcessingWorker(
        db,
        settings,
        processed_meeting["id"],
        TranscriptionService(settings, provider=transcription),
        MeetingSummarizer(settings),
        stage="transcribe",
        force=True,
    )
    completed: list[str] = []
    failed: list[str] = []
    forced_worker.completed.connect(completed.append)
    forced_worker.failed.connect(failed.append)
    forced_worker.run()

    assert completed == [processed_meeting["id"]]
    assert failed == []
    details = ops.get_meeting_details(db, processed_meeting["id"])
    assert details["transcript"]["text"] == "New transcript text."
    # The old analysis is gone so it cannot disagree with the new transcript.
    assert details["summary"] is None
    assert details["action_items"] == []
    assert details["status"] == "transcribed"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def _run_worker_in_thread(worker: MeetingProcessingWorker, signals: list[str]):
    """Run ``worker.run`` on a thread with direct-connected signal capture."""

    worker.cancelled.connect(
        lambda *_: signals.append("cancelled"), Qt.DirectConnection
    )
    worker.completed.connect(
        lambda *_: signals.append("completed"), Qt.DirectConnection
    )
    worker.failed.connect(
        lambda *_: signals.append("failed"), Qt.DirectConnection
    )
    thread = threading.Thread(target=worker.run)
    thread.start()
    return thread


def test_worker_cancel_during_transcription_restores_status(
    qapp, db, settings, recorded_meeting
):
    worker = MeetingProcessingWorker(
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(
            settings, provider=BlockingFakeTranscriptionProvider()
        ),
        MeetingSummarizer(settings),
        stage="transcribe",
    )
    signals: list[str] = []
    thread = _run_worker_in_thread(worker, signals)
    provider = worker.transcription_service._provider
    assert provider.started.wait(2.0)

    worker.cancel()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    assert signals == ["cancelled"]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    # The meeting returns to its previous status; nothing was saved.
    assert details["status"] == "recorded"
    assert details["transcript"] is None


def test_worker_cancel_during_retranscribe_restores_completed(
    qapp, db, settings, processed_meeting
):
    """Cancelling a re-transcription keeps the old transcript and analysis."""

    worker = MeetingProcessingWorker(
        db,
        settings,
        processed_meeting["id"],
        TranscriptionService(
            settings, provider=BlockingFakeTranscriptionProvider()
        ),
        MeetingSummarizer(settings),
        stage="transcribe",
        force=True,
    )
    signals: list[str] = []
    thread = _run_worker_in_thread(worker, signals)
    provider = worker.transcription_service._provider
    assert provider.started.wait(2.0)

    worker.cancel()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    assert signals == ["cancelled"]
    details = ops.get_meeting_details(db, processed_meeting["id"])
    assert details["status"] == "completed"
    assert details["transcript"]["text"].startswith("Alice:")
    assert details["summary"] is not None
    assert len(details["action_items"]) == 1


def test_worker_cancel_during_analysis_discards_result(
    qapp, db, settings, recorded_meeting
):
    """An in-flight analysis is discarded; the transcript is kept."""

    ops.save_transcript(
        db,
        recorded_meeting["id"],
        text="Existing transcript",
        segments=[],
        language="en",
    )

    class CancelAwareAnalysisProvider:
        is_cloud = False

        def __init__(self) -> None:
            self.cancel_requested = threading.Event()
            self.cancel_observed = threading.Event()
            self.calls = 0

        def analyze(self, *, system_prompt: str, user_prompt: str):
            self.calls += 1
            # Ask the test main thread to cancel, then wait until it did, so
            # the worker observes the cancel flag after the request returns.
            self.cancel_requested.set()
            self.cancel_observed.wait(timeout=2.0)
            return valid_analysis()

    analysis_provider = CancelAwareAnalysisProvider()
    worker = MeetingProcessingWorker(
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=FakeTranscriptionProvider()),
        MeetingSummarizer(settings, provider=analysis_provider),  # type: ignore[arg-type]
        stage="analyse",
    )
    signals: list[str] = []
    thread = _run_worker_in_thread(worker, signals)

    assert analysis_provider.cancel_requested.wait(2.0)
    worker.cancel()
    analysis_provider.cancel_observed.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    assert signals == ["cancelled"]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "transcribed"
    assert details["transcript"]["text"] == "Existing transcript"
    assert details["summary"] is None


def test_worker_cancel_before_start_is_immediately_cancelled(
    qapp, db, settings, recorded_meeting
):
    worker = MeetingProcessingWorker(
        db,
        settings,
        recorded_meeting["id"],
        TranscriptionService(settings, provider=FakeTranscriptionProvider()),
        MeetingSummarizer(settings, provider=FakeAnalysisProvider([valid_analysis()])),
        stage="auto",
    )
    worker.cancel()
    signals: list[str] = []
    thread = _run_worker_in_thread(worker, signals)
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    assert signals == ["cancelled"]
    transcription = worker.transcription_service._provider
    analysis = worker.summarizer._provider
    assert transcription.calls == []  # type: ignore[attr-defined]
    assert analysis.calls == []  # type: ignore[attr-defined]
    details = ops.get_meeting_details(db, recorded_meeting["id"])
    assert details["status"] == "recorded"
