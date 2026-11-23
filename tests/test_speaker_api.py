"""Voice profiling tests against real PostgreSQL (API + pipeline + review).

Covers the identity policy end to end: automatic matching only suggests,
user confirmation attaches embeddings to profiles, rejections survive
re-profiling, and low-confidence candidates never become assignments.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from services.speaker_profiles import LabelEmbedding

# Re-exported pytest fixtures from test_api (default app, profiling disabled).
from test_api import api, user_headers_and_id  # noqa: F401

# ---------------------------------------------------------------- fakes


class _FakeEncoder:
    """Deterministic per-label embeddings; no audio or model needed."""

    provider_name = "fake"
    model_name = "fake-256"

    VECTORS = {
        "spk-a": [1.0, 0.0, 0.0],
        "spk-b": [0.0, 1.0, 0.0],
        "spk-c": [0.72, 0.72, 0.0],  # cosine 0.71 vs spk-a: candidate only
        "spk-dup": [1.0, 0.0, 0.0],  # identical to spk-a: same voice
    }

    def compute_label_embeddings(
        self, audio_path, segments, *, min_seconds, max_segments_per_speaker,
        progress_cb=None,
    ):
        labels = sorted(
            {
                str(s["speaker"]).strip()
                for s in segments
                if isinstance(s, dict) and s.get("speaker")
            }
        )
        return [
            LabelEmbedding(
                local_label=label,
                embedding=list(self.VECTORS.get(label, [0.5, 0.5, 0.5])),
                source_seconds=10.0,
            )
            for label in labels
        ]


def _build_enabled_profiler(engine):
    from config import Settings, load_settings
    from services.speaker_profiles import SpeakerProfileService
    from pathlib import Path

    base = load_settings()
    settings = Settings(
        **{
            **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
            "voice_profiling_enabled": True,
        }
    )
    profiler = SpeakerProfileService(engine, settings)
    profiler.set_encoder(_FakeEncoder())
    return profiler


@pytest.fixture(scope="module")
def speaker_api(pg_engine):
    """TestClient like tests.test_api.api, but with profiling enabled."""

    import api.app as app_module
    from fastapi.testclient import TestClient

    from test_api import _NullRunner

    old_url = os.environ.get("DATABASE_URL")
    old_flag = os.environ.get("TEAMMEETINGAI_VOICE_PROFILING")
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    os.environ["TEAMMEETINGAI_VOICE_PROFILING"] = "1"
    try:
        with (
            patch.object(app_module, "_build_transcription_service", _fake_transcription),
            patch.object(app_module, "_build_summarizer", _fake_summarizer),
            patch.object(app_module, "_build_speaker_profiler", _build_enabled_profiler),
            patch.object(app_module, "JobRunner", _NullRunner),
        ):
            app = app_module.build_app()
            with TestClient(app, raise_server_exceptions=True) as client:
                yield client
    finally:
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        if old_flag is None:
            os.environ.pop("TEAMMEETINGAI_VOICE_PROFILING", None)
        else:
            os.environ["TEAMMEETINGAI_VOICE_PROFILING"] = old_flag


def _fake_transcription():
    from ai.transcription import TranscriptionResult, TranscriptionService
    from config import load_settings

    class _Provider:
        provider_name = "fake"
        model_name = "fake-model"
        transcript_mode = None

        def transcribe(self, audio_path, *, language=None, progress_cb=None,
                       cancel_cb=None):
            return TranscriptionResult(text="Fake.", language="en", segments=[])

    return TranscriptionService(load_settings(), provider=_Provider())


def _fake_summarizer():
    from ai.summarization import MeetingSummarizer
    from config import load_settings
    from support import valid_analysis

    class _AnalysisProvider:
        model_name = "fake-analysis"

        def analyze(self, *, system_prompt, user_prompt):
            return valid_analysis()

    return MeetingSummarizer(load_settings(), provider=_AnalysisProvider())


# ---------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _clean(pg_engine):
    from pg_harness import truncate_all

    truncate_all(pg_engine)


@pytest.fixture
def host_and_meeting(speaker_api):
    from test_api import _ingest_meeting

    user = speaker_api.post("/users", json={"display_name": "Host"}).json()
    meeting = _ingest_meeting(speaker_api, user["id"], title="Speakers meeting")
    return speaker_api, str(meeting["id"])


def _insert_diarized_transcript(pg_engine, meeting_id: str) -> str:
    segments = [
        {"start": 0.0, "end": 5.0, "text": "hello", "speaker": "spk-a"},
        {"start": 5.0, "end": 10.0, "text": "world", "speaker": "spk-a"},
        {"start": 10.0, "end": 15.0, "text": "hi there", "speaker": "spk-b"},
        {"start": 15.0, "end": 20.0, "text": "mixed", "speaker": "spk-c"},
    ]
    with pg_engine.begin() as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO transcripts (id, meeting_id, provider, model, "
                    "is_primary, text, segments) VALUES (gen_random_uuid(), :m, "
                    "'fake', 'fake-model', true, 'diarized text', "
                    "CAST(:segments AS jsonb)) RETURNING id"
                ),
                {"m": meeting_id, "segments": json.dumps(segments)},
            ).scalar_one()
        )


def _run_profiling_job(speaker_api, meeting_id: str) -> dict:
    response = speaker_api.post(f"/meetings/{meeting_id}/speakers/profile")
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    speaker_api.app.state.pipeline.run_job(job_id)
    jobs = speaker_api.get(f"/meetings/{meeting_id}/jobs").json()
    return next(job for job in jobs if job["id"] == job_id)


def _speaker_by_label(speaker_api, meeting_id: str, label: str) -> dict:
    speakers = speaker_api.get(f"/meetings/{meeting_id}/speakers").json()
    return next(s for s in speakers if s["local_label"] == label)


# ---------------------------------------------------------------- tests


class TestProfileManagement:
    def test_create_list_rename_delete(self, speaker_api):
        created = speaker_api.post("/profiles", json={"display_name": "Alice"})
        assert created.status_code == 201
        profile = created.json()
        assert profile["display_name"] == "Alice"
        assert profile["embedding_count"] == 0

        duplicate = speaker_api.post("/profiles", json={"display_name": "Alice"})
        assert duplicate.status_code == 400

        renamed = speaker_api.patch(
            f"/profiles/{profile['id']}", json={"display_name": "Alice B"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Alice B"

        listing = speaker_api.get("/profiles").json()
        assert [p["display_name"] for p in listing] == ["Alice B"]

        deleted = speaker_api.delete(f"/profiles/{profile['id']}")
        assert deleted.status_code == 204
        missing = speaker_api.delete(f"/profiles/{profile['id']}")
        assert missing.status_code == 404

    def test_mutations_are_host_only(self, speaker_api):
        user = speaker_api.post("/users", json={"display_name": "Remote"}).json()
        headers = {"X-Profile-Id": user["id"]}
        assert (
            speaker_api.post(
                "/profiles", json={"display_name": "X"}, headers=headers
            ).status_code
            == 403
        )

    def test_empty_name_rejected(self, speaker_api):
        response = speaker_api.post("/profiles", json={"display_name": "   "})
        assert response.status_code == 400


class TestProfilingJob:
    def test_unknown_speaker_has_embedding_but_no_assignment(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        _api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        job = _run_profiling_job(speaker_api, meeting_id)
        assert job["status"] == "succeeded"

        speaker = _speaker_by_label(speaker_api, meeting_id, "spk-a")
        assert speaker["has_embedding"] is True
        assert speaker["status"] is None  # no profiles exist: nothing suggested
        assert speaker["profile_id"] is None

    def test_job_without_speakers_succeeds_with_message(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        _api, meeting_id = host_and_meeting
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transcripts (id, meeting_id, provider, model, "
                    "is_primary, text, segments) VALUES (gen_random_uuid(), :m, "
                    "'fake', 'fake-model', true, 'plain', CAST('[]' AS jsonb))"
                ),
                {"m": meeting_id},
            )
        job = _run_profiling_job(speaker_api, meeting_id)
        assert job["status"] == "succeeded"
        assert "No diarized speakers" in job["message"]

    def test_profiling_requires_transcript(self, speaker_api, host_and_meeting):
        _api, meeting_id = host_and_meeting
        response = speaker_api.post(f"/meetings/{meeting_id}/speakers/profile")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_transcript"

    def test_disabled_profiling_rejects_enqueue(self, api, user_headers_and_id, monkeypatch):
        """With profiling disabled, enqueue is refused (400 profiling_disabled)."""

        from test_api import _ingest_meeting

        # The module fixture enables profiling process-wide; simulate the
        # disabled configuration for this app only.
        profiler = api.app.state.speaker_profiler
        monkeypatch.setattr(
            type(profiler), "enabled", property(lambda self: False)
        )
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = api.post(f"/meetings/{meeting['id']}/speakers/profile")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "profiling_disabled"


class TestAutomaticChaining:
    def test_transcription_chains_profiling_automatically(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        """Running the transcription job chains profiling automatically."""

        api, meeting_id = host_and_meeting
        # Ingest already queued a transcription job — the exact job the UI's
        # Re-transcribe button re-runs.
        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        transcription_job_id = next(
            j["id"]
            for j in jobs
            if j["job_type"] == "transcription" and j["status"] == "queued"
        )

        # The transcription fake has no diarized segments, so profiling
        # succeeds with an explanatory message — the point is that the
        # profiling job is enqueued and runs without any manual action.
        api.app.state.pipeline.run_job(transcription_job_id)

        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        profiling_jobs = [j for j in jobs if j["job_type"] == "profiling"]
        assert profiling_jobs, "transcription must enqueue a profiling job"
        assert profiling_jobs[0]["status"] == "succeeded"
        assert "No diarized speakers" in profiling_jobs[0]["message"]

    def test_profiling_failure_does_not_fail_transcription(
        self, speaker_api, pg_engine, host_and_meeting, monkeypatch
    ):
        """A broken encoder fails only the profiling job, with a clear error."""

        from ai.transcription import TranscriptionResult
        from services.speaker_profiles import SpeakerProfilingError

        api, meeting_id = host_and_meeting
        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        transcription_job_id = next(
            j["id"]
            for j in jobs
            if j["job_type"] == "transcription" and j["status"] == "queued"
        )

        # Make the fake transcription produce diarized segments so the chain
        # reaches the (broken) encoder.
        pipeline = api.app.state.pipeline
        service = pipeline._transcription_service

        def diarized_transcribe(audio_path, **kwargs):
            return TranscriptionResult(
                text="diarized text",
                language="en",
                segments=[
                    {"start": 0.0, "end": 5.0, "text": "hello",
                     "speaker": "spk-a"},
                ],
            )

        monkeypatch.setattr(service, "transcribe", diarized_transcribe)

        class _BrokenEncoder:
            provider_name = "fake"
            model_name = "fake-256"

            def compute_label_embeddings(self, *args, **kwargs):
                raise SpeakerProfilingError(
                    "Voice profiling is unavailable: the 'resemblyzer' "
                    "package is installed but cannot import 'pkg_resources'. "
                    "Fix with: .venv\\Scripts\\python.exe -m pip install "
                    "\"setuptools<81\""
                )

        # Instance-attribute patch: monkeypatch restores the module-scoped
        # fake encoder after the test.
        monkeypatch.setattr(
            pipeline._speaker_profiler, "_encoder", _BrokenEncoder()
        )
        api.app.state.pipeline.run_job(transcription_job_id)

        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        transcription_job = next(
            j for j in jobs if j["id"] == transcription_job_id
        )
        assert transcription_job["status"] == "succeeded"
        profiling_jobs = [j for j in jobs if j["job_type"] == "profiling"]
        assert profiling_jobs
        assert profiling_jobs[0]["status"] == "failed"
        assert "setuptools<81" in profiling_jobs[0]["error"]


class TestMatchingAndReview:
    def test_confirm_creates_profile_and_suggests_next_meeting(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        _run_profiling_job(api, meeting_id)

        # No profiles yet: nothing suggested.
        speaker = _speaker_by_label(api, meeting_id, "spk-a")
        assert speaker["status"] is None

        # Confirm spk-a into a brand-new profile.
        confirmed = api.post(
            f"/meetings/{meeting_id}/speakers/spk-a/assign",
            json={"new_profile_name": "Alice", "status": "confirmed"},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"
        assert confirmed.json()["profile_name"] == "Alice"

        # A second meeting with the same voice must produce a suggestion.
        user = api.post("/users", json={"display_name": "Host2"}).json()
        from test_api import _ingest_meeting

        meeting2 = _ingest_meeting(api, user["id"], title="Second meeting")["id"]
        _insert_diarized_transcript(pg_engine, meeting2)
        _run_profiling_job(api, meeting2)
        suggested = _speaker_by_label(api, meeting2, "spk-a")
        assert suggested["status"] == "suggested"
        assert suggested["profile_name"] == "Alice"
        assert suggested["confidence"] >= 0.8

    def test_rejection_survives_reprofiling(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        _run_profiling_job(api, meeting_id)
        api.post(
            f"/meetings/{meeting_id}/speakers/spk-b/assign",
            json={"status": "rejected"},
        )
        # Re-profile: the user's rejection must not be overwritten.
        _run_profiling_job(api, meeting_id)
        speaker = _speaker_by_label(api, meeting_id, "spk-b")
        assert speaker["status"] == "rejected"

    def test_low_confidence_is_candidate_not_assignment(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        _run_profiling_job(api, meeting_id)
        # Confirm spk-a (vector [1,0,0]).
        api.post(
            f"/meetings/{meeting_id}/speakers/spk-a/assign",
            json={"new_profile_name": "Alice", "status": "confirmed"},
        )
        # spk-c (cosine ~0.71 to Alice) is a candidate, never auto-assigned.
        speaker = _speaker_by_label(api, meeting_id, "spk-c")
        assert speaker["status"] is None
        assert speaker["candidates"], "candidate should be offered for review"
        assert speaker["candidates"][0]["display_name"] == "Alice"
        assert 0.70 <= speaker["candidates"][0]["confidence"] < 0.80

    def test_confirm_requires_profile_or_new_name(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        _run_profiling_job(api, meeting_id)
        response = api.post(
            f"/meetings/{meeting_id}/speakers/spk-a/assign",
            json={"status": "confirmed"},
        )
        assert response.status_code == 400

    def test_same_voice_labels_detect_split_speakers(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        """Two labels with the same voice print are flagged as one person."""

        api, meeting_id = host_and_meeting
        segments = [
            {"start": 0.0, "end": 5.0, "text": "hello", "speaker": "spk-a"},
            {"start": 5.0, "end": 10.0, "text": "echo of the same voice",
             "speaker": "spk-dup"},
            {"start": 10.0, "end": 15.0, "text": "different voice",
             "speaker": "spk-b"},
        ]
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transcripts (id, meeting_id, provider, model, "
                    "is_primary, text, segments) VALUES (gen_random_uuid(), :m, "
                    "'fake', 'fake-model', true, 'split diarization', "
                    "CAST(:segments AS jsonb))"
                ),
                {"m": meeting_id, "segments": json.dumps(segments)},
            )
        _run_profiling_job(api, meeting_id)

        spk_a = _speaker_by_label(api, meeting_id, "spk-a")
        spk_dup = _speaker_by_label(api, meeting_id, "spk-dup")
        assert spk_a["same_voice_labels"] == ["spk-dup"]
        assert spk_dup["same_voice_labels"] == ["spk-a"]
        # A genuinely different voice is never flagged.
        spk_b = _speaker_by_label(api, meeting_id, "spk-b")
        assert spk_b["same_voice_labels"] == []

    def test_assign_unknown_label_is_404(self, speaker_api, pg_engine, host_and_meeting):
        """A label that is not in the transcript is a client error (404)."""

        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        response = api.post(
            f"/meetings/{meeting_id}/speakers/does-not-exist/assign",
            json={"status": "rejected"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "speaker_not_found"

    def test_list_speakers_without_transcript_is_empty(
        self, speaker_api, host_and_meeting
    ):
        _api, meeting_id = host_and_meeting
        assert speaker_api.get(f"/meetings/{meeting_id}/speakers").json() == []
