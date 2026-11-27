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
                "INSERT INTO meeting_analyzer.users (id, display_name) VALUES (gen_random_uuid(), :n) "
                "RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def _meeting_and_job(conn, meeting_id: str) -> dict:
    meeting = conn.execute(
        text("SELECT * FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
    ).mappings().one()
    files = conn.execute(
        text("SELECT kind, size_bytes, sha256 FROM meeting_analyzer.meeting_files WHERE meeting_id = :m "
             "ORDER BY kind"),
        {"m": meeting_id},
    ).mappings().all()
    jobs = conn.execute(
        text("SELECT job_type, status FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m"),
        {"m": meeting_id},
    ).mappings().all()
    return {"meeting": dict(meeting), "files": [dict(f) for f in files],
            "jobs": [dict(j) for j in jobs]}


class TestIngest:
    def test_ingest_stores_only_canonical_opus(self, pg, wav_factory):
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
        assert kinds == {"audio_opus"}

    def test_opus_audio_metadata_and_lo(self, pg, wav_factory):
        user_id = _user_id(pg)
        original = wav_factory(seconds=1.0)
        meeting_id = ingest_recording(
            pg,
            user_id=user_id,
            title="Check opus",
            original_path=original,
            duration_ms=1000,
            sample_rate=48000,
            channels=2,
        )
        pg.commit()
        row = pg.execute(
            text(
                """
                SELECT sample_rate, channels, size_bytes, sha256, lo_oid, mime_type, codec, bitrate_kbps, container, duration_ms
                FROM meeting_analyzer.meeting_files
                WHERE meeting_id = :m AND kind = 'audio_opus'
                """
            ),
            {"m": meeting_id},
        ).mappings().one()
        assert row["sample_rate"] == 16000
        assert row["channels"] == 1
        assert row["mime_type"] == "audio/ogg"
        assert row["codec"] == "opus"
        assert row["bitrate_kbps"] == 32
        assert row["container"] == "ogg"
        assert row["duration_ms"] and row["duration_ms"] > 0
        assert lo.verify_lo(pg, int(row["lo_oid"]), row["sha256"]) is True
        # OggS header check from LO bytes
        data = lo.read_lo_range(pg, int(row["lo_oid"]), 0, 4)
        assert data == b"OggS"
        # Should be much smaller than source WAV (1s 48k stereo 16-bit ~192KB).
        assert row["size_bytes"] < original.stat().st_size

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
            text("SELECT job_type, status FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m"),
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
            "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id = :m AND "
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
                "SELECT provider, model, is_primary, text, language FROM meeting_analyzer.transcripts "
                "WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        ).mappings().all()
        assert len(row) == 1
        assert row[0]["is_primary"] is True
        assert row[0]["provider"] == "stub"
        assert row[0]["text"].startswith("Alice:")
        status = pg.execute(
            text("SELECT status FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
        ).scalar_one()
        assert status == "transcribed"
        job_status = pg.execute(
            text("SELECT status, error FROM meeting_analyzer.processing_jobs WHERE id = :id"),
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
                "SELECT provider, is_primary, fallback_from FROM meeting_analyzer.transcripts WHERE "
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
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status) "
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
                "SELECT COUNT(*) FROM meeting_analyzer.transcripts WHERE meeting_id = :m AND is_primary"
            ),
            {"m": meeting_id},
        ).scalar_one()
        total = pg.execute(
            text("SELECT COUNT(*) FROM meeting_analyzer.transcripts WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one()
        assert primaries == 1
        assert total == 2

    def test_provider_failure_marks_job_failed(self, pg, tmp_path, ingested_meeting):
        meeting_id, job_id = ingested_meeting
        pg.execute(text("UPDATE meeting_analyzer.processing_jobs SET max_attempts=1, attempt_count=1 WHERE id=:id"), {"id": job_id})
        pg.commit()
        service = TranscriptionService(
            make_settings(tmp_path),
            provider=StubProvider(error=TranscriptionError("all providers down")),
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status, error = pg.execute(
            text("SELECT status, error FROM meeting_analyzer.processing_jobs WHERE id = :id"),
            {"id": job_id},
        ).one()
        assert status == "failed"
        assert error is not None
        assert pg.execute(
            text("SELECT COUNT(*) FROM meeting_analyzer.transcripts WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).scalar_one() == 0

    def test_transient_failure_retries_then_fails(self, pg, tmp_path, ingested_meeting):
        meeting_id, job_id = ingested_meeting
        pg.execute(text("UPDATE meeting_analyzer.processing_jobs SET max_attempts=3, attempt_count=1 WHERE id=:id"), {"id": job_id})
        pg.commit()
        service = TranscriptionService(
            make_settings(tmp_path),
            provider=StubProvider(error=TranscriptionError("temporary provider timeout")),
        )
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        row = pg.execute(text("SELECT status, attempt_count, max_attempts, next_run_at, error_code FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id": job_id}).mappings().one()
        assert row["status"] == "queued"
        assert row["attempt_count"] == 1
        assert row["max_attempts"] == 3
        assert row["next_run_at"] is not None
        assert row["error_code"] is not None

        pg.execute(text("UPDATE meeting_analyzer.processing_jobs SET attempt_count=3, next_run_at=NULL WHERE id=:id"), {"id": job_id})
        pg.commit()
        pipeline.run_job(job_id)
        pg.commit()
        row = pg.execute(text("SELECT status, error_code, error FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id": job_id}).mappings().one()
        assert row["status"] == "failed"
        assert row["error_code"] is not None
        assert row["error"] is not None

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
            text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id = :id"), {"id": job_id}
        ).scalar_one()
        assert status == "cancelled"
        assert pg.execute(
            text("SELECT COUNT(*) FROM meeting_analyzer.transcripts WHERE meeting_id = :m"),
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
                            "UPDATE meeting_analyzer.processing_jobs SET status = 'cancelled' WHERE id = :id"
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
            text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id = :id"), {"id": job_id}
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
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status) "
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
            text("SELECT overview, payload, model FROM meeting_analyzer.summaries WHERE meeting_id = :m"),
            {"m": meeting_id},
        ).mappings().one()
        assert summary["overview"] == "The team agreed to ship the release."
        item = pg.execute(
            text(
                "SELECT task, owner, deadline, evidence, status FROM meeting_analyzer.action_items "
                "WHERE meeting_id = :m"
            ),
            {"m": meeting_id},
        ).mappings().one()
        assert item["task"] == "Ship the release"
        assert item["deadline"] == "Friday"
        assert item["status"] == "open"
        status = pg.execute(
            text("SELECT status FROM meeting_analyzer.meetings WHERE id = :m"), {"m": meeting_id}
        ).scalar_one()
        assert status == "completed"
        job_status = pg.execute(
            text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id = :id"),
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
                    "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status) "
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
            text("SELECT status, error FROM meeting_analyzer.processing_jobs WHERE id = :id"),
            {"id": analysis_job_id},
        ).one()
        assert status == "failed"
        assert "primary transcript" in error


class TestOpusCompatibility:
    def test_legacy_wav_files_still_supported_by_pipeline(self, pg, tmp_path, wav_factory):
        # Simulate a legacy meeting with derived_audio_16k WAV already stored.
        user_id = _user_id(pg)
        import wave, struct, math
        derived = tmp_path / 'legacy-16k.wav'
        sr = 16000; n = int(sr * 0.25)
        with wave.open(str(derived), 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
            for i in range(n):
                wf.writeframes(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / sr))))
        meeting_id = str(pg.execute(text(
            "INSERT INTO meeting_analyzer.meetings (id, user_id, title, status, duration_ms) "
            "VALUES (gen_random_uuid(), :uid, 'Legacy', 'recorded', 250) RETURNING id"
        ), {"uid": user_id}).scalar_one())
        from db import lo
        oid, sha, size = lo.create_lo_from_file(pg, derived)
        pg.execute(text(
            "INSERT INTO meeting_analyzer.meeting_files (id, meeting_id, kind, lo_oid, sha256, size_bytes, mime_type, duration_ms, sample_rate, channels, original_filename) "
            "VALUES (gen_random_uuid(), :m, 'derived_audio_16k', :oid, :sha, :size, 'audio/wav', 250, 16000, 1, 'legacy.wav')"
        ), {"m": meeting_id, "oid": oid, "sha": sha, "size": size})
        job_id = str(pg.execute(text(
            "INSERT INTO meeting_analyzer.processing_jobs (id, meeting_id, job_type, status) "
            "VALUES (gen_random_uuid(), :m, 'transcription', 'queued') RETURNING id"
        ), {"m": meeting_id}).scalar_one())
        pg.commit()
        service = TranscriptionService(make_settings(tmp_path), provider=StubProvider(result=TRANSCRIPT_RESULT))
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status = pg.execute(text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id": job_id}).scalar_one()
        assert status == 'succeeded'

    def test_ingest_rejects_corrupt_audio(self, pg, tmp_path):
        user_id = _user_id(pg)
        bad = tmp_path / 'bad.wav'
        bad.write_bytes(b'not audio at all')
        with pytest.raises(IngestError):
            ingest_recording(pg, user_id=user_id, title='Bad', original_path=bad, duration_ms=0, sample_rate=16000, channels=1)

    def test_no_permanent_wav_kinds_on_new_ingest(self, pg, wav_factory):
        user_id = _user_id(pg)
        original = wav_factory(seconds=0.5)
        meeting_id = ingest_recording(
            pg, user_id=user_id, title="Opus only", original_path=original,
            duration_ms=500, sample_rate=48000, channels=2,
        )
        pg.commit()
        kinds = [r[0] for r in pg.execute(
            text("SELECT kind FROM meeting_analyzer.meeting_files WHERE meeting_id=:m ORDER BY kind"),
            {"m": meeting_id},
        ).all()]
        assert kinds == ["audio_opus"]
        # No legacy WAV artifacts persisted for new ingest
        assert "original_audio_48k" not in kinds
        assert "derived_audio_16k" not in kinds

    def test_supported_format_converts_to_canonical_opus(self, pg, tmp_path):
        import shutil, subprocess
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not available")
        # Generate a small source mp3 (supported upload format) from synthetic audio
        src_wav = tmp_path / "src.wav"
        write_pcm_wav(src_wav, seconds=0.5)
        src_mp3 = tmp_path / "src.mp3"
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(src_wav), "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "64k", str(src_mp3)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not src_mp3.exists():
            pytest.skip(f"Could not generate mp3 test fixture: {proc.stderr[:200]}")
        user_id = _user_id(pg)
        meeting_id = ingest_recording(
            pg, user_id=user_id, title="MP3 upload", original_path=src_mp3,
            duration_ms=500, sample_rate=44100, channels=2, mime_type="audio/mpeg",
            original_filename="src.mp3",
        )
        pg.commit()
        row = pg.execute(text(
            "SELECT kind, mime_type, codec, bitrate_kbps, container, sample_rate, channels, duration_ms, lo_oid, sha256, original_filename "
            "FROM meeting_analyzer.meeting_files WHERE meeting_id=:m"
        ), {"m": meeting_id}).mappings().one()
        assert row["kind"] == "audio_opus"
        assert row["mime_type"] == "audio/ogg"
        assert row["codec"] == "opus"
        assert row["bitrate_kbps"] == 32
        assert row["container"] == "ogg"
        assert row["sample_rate"] == 16000
        assert row["channels"] == 1
        assert row["duration_ms"] and row["duration_ms"] > 0
        assert row["original_filename"].endswith(".ogg")
        assert lo.verify_lo(pg, int(row["lo_oid"]), row["sha256"]) is True
        header = lo.read_lo_range(pg, int(row["lo_oid"]), 0, 4)
        assert header == b"OggS"

    def test_retry_reuses_stored_opus_artifact(self, pg, tmp_path, wav_factory):
        user_id = _user_id(pg)
        original = wav_factory(seconds=0.25)
        meeting_id = ingest_recording(
            pg, user_id=user_id, title="Retry opus", original_path=original,
            duration_ms=250, sample_rate=48000, channels=2,
        )
        pg.commit()
        before = pg.execute(text(
            "SELECT lo_oid, sha256, size_bytes FROM meeting_analyzer.meeting_files WHERE meeting_id=:m AND kind='audio_opus'"
        ), {"m": meeting_id}).mappings().one()

        # Fail once, then succeed on retry - should reuse same stored opus LO.
        calls = {"n": 0}
        class FlakyProvider:
            name = "flaky"
            def transcribe(self, audio_path, *, language="en", cancel_cb=None, progress_cb=None, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TranscriptionError("transient network error")
                return TRANSCRIPT_RESULT

        job_id = str(pg.execute(text(
            "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m AND job_type='transcription'"
        ), {"m": meeting_id}).scalar_one())
        service = TranscriptionService(make_settings(tmp_path), provider=FlakyProvider())
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status = pg.execute(text("SELECT status, attempt_count, error_code FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id": job_id}).mappings().one()
        assert status["status"] == "queued"  # retry queued
        assert status["error_code"] == "transcription_failed_retryable"
        # attempt_count is incremented by JobRunner claim; direct pipeline.run_job may leave it unchanged.

        pipeline.run_job(job_id)
        pg.commit()
        after = pg.execute(text(
            "SELECT lo_oid, sha256, size_bytes FROM meeting_analyzer.meeting_files WHERE meeting_id=:m AND kind='audio_opus'"
        ), {"m": meeting_id}).mappings().one()
        assert dict(after) == dict(before)
        kinds = [r[0] for r in pg.execute(text(
            "SELECT kind FROM meeting_analyzer.meeting_files WHERE meeting_id=:m ORDER BY kind"
        ), {"m": meeting_id}).all()]
        assert kinds == ["audio_opus"]

    def test_transcription_decodes_opus_to_temp_wav_and_cleans_up(self, pg, tmp_path, wav_factory, monkeypatch):
        import tempfile, os
        real_temporary_directory = tempfile.TemporaryDirectory
        captured = []
        class CapturingTempDir:
            def __init__(self, *args, **kwargs):
                self._real = real_temporary_directory(*args, **kwargs)
                captured.append(self._real.name)
            def __enter__(self):
                return self._real.__enter__()
            def __exit__(self, exc_type, exc, tb):
                return self._real.__exit__(exc_type, exc, tb)

        monkeypatch.setattr(tempfile, "TemporaryDirectory", CapturingTempDir)
        user_id = _user_id(pg)
        original = wav_factory(seconds=0.25)
        meeting_id = ingest_recording(
            pg, user_id=user_id, title="Temp cleanup", original_path=original,
            duration_ms=250, sample_rate=48000, channels=2,
        )
        pg.commit()
        job_id = str(pg.execute(text(
            "SELECT id FROM meeting_analyzer.processing_jobs WHERE meeting_id=:m AND job_type='transcription'"
        ), {"m": meeting_id}).scalar_one())
        service = TranscriptionService(make_settings(tmp_path), provider=StubProvider(result=TRANSCRIPT_RESULT))
        pipeline = TranscriptionPipeline(pg.engine, service, summarizer=None)
        pipeline.run_job(job_id)
        pg.commit()
        status = pg.execute(text("SELECT status FROM meeting_analyzer.processing_jobs WHERE id=:id"), {"id": job_id}).scalar_one()
        assert status == "succeeded"
        # Captured temp dirs (ingest + pipeline) should be cleaned up
        assert captured, "expected TemporaryDirectory to be used"
        for d in captured:
            assert not os.path.exists(d), f"Temp dir not cleaned up: {d}"
