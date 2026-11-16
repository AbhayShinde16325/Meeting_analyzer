"""Ingest and pipeline tests against real PostgreSQL.

Covers: 48k->16k derivation and LO storage, transcription job success/failure/
cancellation, fallback recording, single-primary guarantee, and chained
analysis producing summaries + action items.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from config import Settings
from db import lo
from services.ingest import IngestError, ingest_recording
from services.pipeline import TranscriptionPipeline
from support import FakeAnalysisProvider, TranscriptionResult, valid_analysis, write_pcm_wav
from ai.summarization import MeetingSummarizer
from ai.transcription import (
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionService,
)


@pytest.fixture
def wav_factory(tmp_path):
    def factory(name: str = "audio.wav", seconds: float = 0.25) -> Path:
        path = tmp_path / name
        write_pcm_wav(path, seconds=seconds)
        return path

    return factory


def _user_id(conn, name="Pipeline User") -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO users (id, display_name) VALUES (gen_random_uuid(), :n) "
                "RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def _meeting_and_job(conn, meeting_id: str) -> dict:
    meeting = conn.execute(
        text("SELECT * FROM meetings WHERE id = :m"), {"m": meeting_id}
    ).mappings().one()
    files = conn.execute(
        text("SELECT kind, size_bytes, sha256 FROM meeting_files WHERE meeting_id = :m "
             "ORDER BY kind"),
        {"m": meeting_id},
    ).mappings().all()
    jobs = conn.execute(
        text("SELECT job_type, status FROM processing_jobs WHERE meeting_id = :m"),
        {"m": meeting_id},
    ).mappings().all()
    return {"meeting": dict(meeting), "files": [dict(f) for f in files],
            "jobs": [dict(j) for j in jobs]}


class TestIngest:
    def test_ingest_stores_original_and_derived_audio(self, pg, wav_factory):
        user_id = _user_id(pg)
        original = wav_factory()
        meeting_id = ingest_recording(
            pg,
            user_id=user_id,
            title="Ingested meeting",
            original_path=original,
            duration_ms=250,
            sample_rate=48000,
            channels=2,
        )
        pg.commit()
        state = _meeting_and_job(pg, meeting_id)
        assert state["meeting"]["status"] == "recorded"
        kinds = {f["kind"] for f in state["files"]}
        assert kinds == {"original_audio_48k", "derived_audio_16k"}

    def test_derived_audio_is_16k_mono_and_matches_lo(self, pg, wav_factory):
        user_id = _user_id(pg)
        original = wav_factory()
        meeting_id = ingest_recording(
            pg,
            user_id=user_id,
            title="Check derived",
            original_path=original,
            duration_ms=250,
            sample_rate=48000,
            channels=2,
        )
        pg.commit()
        row = pg.execute(
            text(
                "SELECT sample_rate, channels, size_bytes, sha256, lo_oid FROM "
                "meeting_files WHERE meeting_id = :m AND kind = 'derived_audio_16k'"
            ),
            {"m": meeting_id},
        ).mappings().one()
        assert row["sample_rate"] == 16000
        assert row["channels"] == 1
        assert lo.verify_lo(pg, int(row["lo_oid"]), row["sha256"]) is True
        # The 16 kHz derivative must be smaller than the 48 kHz stereo original.
        original_size = pg.execute(
            text(
                "SELECT size_bytes FROM meeting_files WHERE meeting_id = :m AND "
                "kind = 'original_audio_48k'"
            ),
            {"m": meeting_id},
        ).scalar_one()
        assert row["size_bytes"] < original_size

    def test_ingest_enqueues_transcription_job(self, pg, wav_factory):
        user_id = _user_id(pg)
        meeting_id = ingest_recording(
            pg,
            user_id=user_id,
            title="Job enqueue",
            original_path=wav_factory(),
            duration_ms=250,
            sample_rate=48000,
            channels=2,
        )
        pg.commit()
        jobs = pg.execute(
            text("SELECT job_type, status FROM processing_jobs WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).mappings().all()
        assert [(j["job_type"], j["status"]) for j in jobs] == [
            ("transcription", "queued")
        ]

    def test_ingest_rejects_missing_file(self, pg):
        user_id = _user_id(pg)
        with pytest.raises(IngestError):
            ingest_recording(
                pg,
                user_id=user_id,
                title="No file",
                original_path=Path("Z:/does/not/exist.wav"),
                duration_ms=0,
                sample_rate=48000,
                channels=2,
            )

    def test_ingest_rejects_empty_file(self, pg, tmp_path):
        user_id = _user_id(pg)
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"RIFF....")
        with pytest.raises(IngestError, match="empty"):
            ingest_recording(
                pg,
                user_id=user_id,
                title="Empty",
                original_path=empty,
                duration_ms=0,
                sample_rate=48000,
                channels=2,
            )


def make_settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, recordings_dir=tmp_path / "recordings")


class StubProvider:
    def __init__(self, *, name: str = "stub", model: str = "stub-model",
                 result=None, error=None):
        self.provider_name = name
        self.model_name = model
        self.transcript_mode = None
        self.result = result
        self.error = error

    def transcribe(self, audio_path, *, language=None, progress_cb=None, cancel_cb=None):
        if self.error is not None:
            raise self.error
        return self.result


TRANSCRIPT_RESULT = TranscriptionResult(
    text="Alice: Ship the release on Friday.",
    language="en",
    segments=[{"start": 0.0, "end": 2.5, "text": "Alice: Ship the release on Friday."}],
)


@pytest.fixture
def ingested_meeting(pg, wav_factory):
    """Ingest a recording and return (meeting_id, transcription_job_id)."""

    user_id = _user_id(pg)
    meeting_id = ingest_recording(
        pg,
        user_id=user_id,
        title="Pipeline test meeting",
        original_path=wav_factory(),
        duration_ms=250,
        sample_rate=48000,
        channels=2,
    )
    pg.commit()
    job_id = pg.execute(
        text(
            "SELECT id FROM processing_jobs WHERE meeting_id = :m AND "
            "job_type = 'transcription'"
        ),
        {"m": meeting_id},
    ).scalar_one()
    return str(meeting_id), str(job_id)


class TestTranscriptionPipeline:
    def test_success_creates_primary_transcript(self, pg, tmp_path, ingested_meeting):
        meeting_id, job_id = ingested_meeting
        service = TranscriptionService(
            make_settings(tmp_path), provider=StubProvider(result=TRANSCRIPT_RESULT)
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()

        row = pg.execute(
            text(
                "SELECT provider, model, is_primary, text, language FROM transcripts "
                "WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        ).mappings().all()
        assert len(row) == 1
        assert row[0]["is_primary"] is True
        assert row[0]["provider"] == "stub"
        assert row[0]["text"].startswith("Alice:")
        status = pg.execute(
            text("SELECT status FROM meetings WHERE id = :m"), {"m": meeting_id}
        ).scalar_one()
        assert status == "transcribed"
        job_status = pg.execute(
            text("SELECT status, error FROM processing_jobs WHERE id = :id"),
            {"id": job_id},
        ).mappings().one()
        assert job_status["status"] == "succeeded"
        assert job_status["error"] is None

    def test_fallback_is_recorded_on_transcript(self, pg, tmp_path, ingested_meeting):
        from ai.fallback_provider import FallbackTranscriptionProvider

        meeting_id, job_id = ingested_meeting
        primary = StubProvider(name="sarvam", error=TranscriptionError("down"))
        fallback = StubProvider(name="whisper", model="small", result=TRANSCRIPT_RESULT)
        service = TranscriptionService(
            make_settings(tmp_path),
            provider=FallbackTranscriptionProvider(primary, fallback),
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()

        row = pg.execute(
            text(
                "SELECT provider, is_primary, fallback_from FROM transcripts WHERE "
                "meeting_id = :m"
            ),
            {"m": meeting_id},
        ).mappings().one()
        assert row["provider"] == "whisper"
        assert row["fallback_from"] == "sarvam"
        assert row["is_primary"] is True

    def test_retranscribe_keeps_exactly_one_primary(self, pg, tmp_path, ingested_meeting):
        meeting_id, job_id = ingested_meeting
        service = TranscriptionService(
            make_settings(tmp_path), provider=StubProvider(result=TRANSCRIPT_RESULT)
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        # Second pass (as if re-transcribed): ingest a new queued job.
        second_id = str(
            pg.execute(
                text(
                    "INSERT INTO processing_jobs (id, meeting_id, job_type, status) "
                    "VALUES (gen_random_uuid(), :m, 'transcription', 'queued') "
                    "RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()
        )
        pg.commit()
        pipeline.run_job(second_id)
        pg.commit()
        primaries = pg.execute(
            text(
                "SELECT COUNT(*) FROM transcripts WHERE meeting_id = :m AND is_primary"
            ),
            {"m": meeting_id},
        ).scalar_one()
        total = pg.execute(
            text("SELECT COUNT(*) FROM transcripts WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one()
        assert primaries == 1
        assert total == 2

    def test_provider_failure_marks_job_failed(self, pg, tmp_path, ingested_meeting):
        meeting_id, job_id = ingested_meeting
        service = TranscriptionService(
            make_settings(tmp_path),
            provider=StubProvider(error=TranscriptionError("all providers down")),
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status, error = pg.execute(
            text("SELECT status, error FROM processing_jobs WHERE id = :id"),
            {"id": job_id},
        ).one()
        assert status == "failed"
        assert error is not None
        assert pg.execute(
            text("SELECT COUNT(*) FROM transcripts WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one() == 0

    def test_cancellation_marks_job_cancelled_without_transcript(
        self, pg, tmp_path, ingested_meeting
    ):
        meeting_id, job_id = ingested_meeting
        service = TranscriptionService(
            make_settings(tmp_path),
            provider=StubProvider(error=TranscriptionCancelled("cancelled")),
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status = pg.execute(
            text("SELECT status FROM processing_jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
        assert status == "cancelled"
        assert pg.execute(
            text("SELECT COUNT(*) FROM transcripts WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one() == 0

    def test_cancel_flag_checked_mid_transcription(self, pg, tmp_path, ingested_meeting):
        """A provider polling cancel_cb sees the DB 'cancelled' status."""

        meeting_id, job_id = ingested_meeting
        observed: dict = {}

        class CancellingProvider:
            provider_name = "cancelling"
            model_name = "c"
            transcript_mode = None

            def transcribe(self, audio_path, *, language=None, progress_cb=None,
                           cancel_cb=None):
                observed["cancel_cb"] = cancel_cb
                if cancel_cb is not None:
                    # Mark cancelled externally, then let the provider see it.
                    pg.execute(
                        text(
                            "UPDATE processing_jobs SET status = 'cancelled' WHERE id = :id"
                        ),
                        {"id": job_id},
                    )
                    pg.commit()
                    if cancel_cb():
                        raise TranscriptionCancelled("seen via cancel_cb")
                return TRANSCRIPT_RESULT

        service = TranscriptionService(
            make_settings(tmp_path), provider=CancellingProvider()
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        assert observed["cancel_cb"] is not None
        status = pg.execute(
            text("SELECT status FROM processing_jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
        assert status == "cancelled"


class TestAnalysisPipeline:
    def test_analysis_uses_primary_transcript_and_saves_items(
        self, pg, tmp_path, ingested_meeting, monkeypatch
    ):
        meeting_id, job_id = ingested_meeting
        service = TranscriptionService(
            make_settings(tmp_path), provider=StubProvider(result=TRANSCRIPT_RESULT)
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()

        analysis_provider = FakeAnalysisProvider([valid_analysis()])
        summarizer = MeetingSummarizer(
            make_settings(tmp_path), provider=analysis_provider
        )
        analysis_job_id = str(
            pg.execute(
                text(
                    "INSERT INTO processing_jobs (id, meeting_id, job_type, status) "
                    "VALUES (gen_random_uuid(), :m, 'analysis', 'queued') RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()
        )
        pg.commit()
        analysis_pipeline = TranscriptionPipeline(
            pg.engine, service, summarizer=summarizer
        )
        analysis_pipeline.run_job(analysis_job_id)
        pg.commit()

        assert len(analysis_provider.calls) == 1
        # The analysis prompt must contain the primary transcript text.
        assert "Ship the release on Friday" in analysis_provider.calls[0]["user_prompt"]

        summary = pg.execute(
            text("SELECT overview, payload, model FROM summaries WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).mappings().one()
        assert summary["overview"] == "The team agreed to ship the release."
        item = pg.execute(
            text(
                "SELECT task, owner, deadline, evidence, status FROM action_items "
                "WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        ).mappings().one()
        assert item["task"] == "Ship the release"
        assert item["deadline"] == "Friday"
        assert item["status"] == "open"
        status = pg.execute(
            text("SELECT status FROM meetings WHERE id = :m"), {"m": meeting_id}
        ).scalar_one()
        assert status == "completed"
        job_status = pg.execute(
            text("SELECT status FROM processing_jobs WHERE id = :id"),
            {"id": analysis_job_id},
        ).scalar_one()
        assert job_status == "succeeded"

    def test_analysis_without_transcript_fails(
        self, pg, tmp_path, ingested_meeting
    ):
        meeting_id, _job_id = ingested_meeting
        summarizer = MeetingSummarizer(
            make_settings(tmp_path), provider=FakeAnalysisProvider([valid_analysis()])
        )
        analysis_job_id = str(
            pg.execute(
                text(
                    "INSERT INTO processing_jobs (id, meeting_id, job_type, status) "
                    "VALUES (gen_random_uuid(), :m, 'analysis', 'queued') RETURNING id"
                ),
                {"m": meeting_id},
            ).scalar_one()
        )
        pg.commit()
        pipeline = TranscriptionPipeline(pg.engine, transcription_service=None, summarizer=summarizer)
        pipeline.run_job(analysis_job_id)
        pg.commit()
        status, error = pg.execute(
            text("SELECT status, error FROM processing_jobs WHERE id = :id"),
            {"id": analysis_job_id},
        ).one()
        assert status == "failed"
        assert "primary transcript" in error
