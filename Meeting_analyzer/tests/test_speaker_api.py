"""Voice profiling tests against real PostgreSQL (API + pipeline + review).

Covers automatic clear-match confirmation, explicit enrollment references,
manual decision preservation, retry idempotency, and Unknown handling for
low-confidence or ambiguous matches.
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
        "spk-c": [0.72, 0.72, 0.0],  # cosine ~0.707 vs spk-a/b: ambiguous when both refs exist
        "spk-low": [0.69, 0.72, 0.0],  # cosine ~0.692 vs spk-a: below 0.70 threshold
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

    def embed_reference_audio(self, audio_path):
        name = Path(audio_path).name.lower()
        for key, vec in self.VECTORS.items():
            if key in name:
                return list(vec), 10.0
        # Allow tests to encode vector via filename like ref-1-0-0.wav? not needed now.
        return [1.0, 0.0, 0.0], 10.0


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
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, "
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

    def test_mutations_allowed_for_profiles(self, speaker_api):
        user = speaker_api.post("/users", json={"display_name": "Remote"}).json()
        headers = {"X-Profile-Id": user["id"]}
        assert speaker_api.post("/profiles", json={"display_name": "X"}, headers=headers).status_code == 201

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
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, "
                    "is_primary, text, segments) VALUES (gen_random_uuid(), :m, "
                    "'fake', 'fake-model', true, 'plain', CAST('[]' AS jsonb))"
                ),
                {"m": meeting_id},
            )
        job = _run_profiling_job(speaker_api, meeting_id)
        assert job["status"] == "succeeded"
        assert "No diarized speakers" in job["message"]

    def test_repeated_profile_request_reuses_active_job(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        first = api.post(f"/meetings/{meeting_id}/speakers/profile")
        second = api.post(f"/meetings/{meeting_id}/speakers/profile")
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        assert sum(j["job_type"] == "profiling" for j in jobs) == 1

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

        # The transcription fake has no diarized segments. Profiling is queued
        # as a separate background job and is not run in the transcription call.
        api.app.state.pipeline.run_job(transcription_job_id)

        jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
        profiling_jobs = [j for j in jobs if j["job_type"] == "profiling"]
        assert len(profiling_jobs) == 1, "transcription must enqueue one profiling job"
        assert profiling_jobs[0]["status"] == "queued"
        api.app.state.pipeline.run_job(profiling_jobs[0]["id"])
        finished = next(j for j in api.get(f"/meetings/{meeting_id}/jobs").json() if j["id"] == profiling_jobs[0]["id"])
        assert finished["status"] == "succeeded"
        assert "No diarized speakers" in finished["message"]

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
        assert profiling_jobs[0]["status"] == "queued"
        api.app.state.pipeline.run_job(profiling_jobs[0]["id"])
        profiling_jobs = [j for j in api.get(f"/meetings/{meeting_id}/jobs").json() if j["job_type"] == "profiling"]
        assert profiling_jobs[0]["status"] == "failed"
        assert "setuptools<81" in profiling_jobs[0]["error"]
        assert api.get(f"/meetings/{meeting_id}").json()["status"] == "transcribed"


class TestMatchingAndReview:
    def _enroll_ref(self, api, profile_id: str, label: str = "spk-a"):
        files = {"file": (f"{label}.wav", b"RIFF" + b"\0" * 120, "audio/wav")}
        resp = api.post(f"/profiles/{profile_id}/enroll", files=files)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_enrollment_embedding_is_reused_as_profile_reference(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        profile = api.post("/profiles", json={"display_name": "Enrolled"}).json()
        before_meetings = pg_engine.connect().execute(text("SELECT COUNT(*) FROM meeting_analyzer.meetings")).scalar_one()
        before_transcripts = pg_engine.connect().execute(text("SELECT COUNT(*) FROM meeting_analyzer.transcripts")).scalar_one()
        self._enroll_ref(api, profile["id"], "spk-a")
        with pg_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM meeting_analyzer.meetings")).scalar_one() == before_meetings
            assert conn.execute(text("SELECT COUNT(*) FROM meeting_analyzer.transcripts")).scalar_one() == before_transcripts
            ref_count = conn.execute(text(
                "SELECT COUNT(*) FROM meeting_analyzer.speaker_reference_embeddings WHERE profile_id=:p"
            ), {"p": profile["id"]}).scalar_one()
        assert ref_count == 1
        centroids = api.app.state.speaker_profiler._profile_centroids()
        assert centroids[profile["id"]] == [1.0, 0.0, 0.0]

    def test_reprofiling_existing_manual_decision_repairs_embedding_link(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        transcript_id = _insert_diarized_transcript(pg_engine, meeting_id)
        profile = api.post("/profiles", json={"display_name": "Manual"}).json()
        with pg_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO meeting_analyzer.speaker_assignments "
                "(id,meeting_id,transcript_id,local_label,profile_id,confidence,status,origin) "
                "VALUES (gen_random_uuid(),:m,:t,'spk-a',:p,1.0,'confirmed','manual')"
            ), {"m": meeting_id, "t": transcript_id, "p": profile["id"]})
        _run_profiling_job(api, meeting_id)
        with pg_engine.connect() as conn:
            linked = conn.execute(text(
                "SELECT profile_id FROM meeting_analyzer.speaker_embeddings "
                "WHERE transcript_id=:t AND local_label='spk-a'"
            ), {"t": transcript_id}).scalar_one()
            origin = conn.execute(text(
                "SELECT origin FROM meeting_analyzer.speaker_assignments "
                "WHERE transcript_id=:t AND local_label='spk-a'"
            ), {"t": transcript_id}).scalar_one()
        assert str(linked) == profile["id"]
        assert origin == "manual"

    def test_clear_profile_match_is_returned_as_confirmed_name(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        transcript_id = _insert_diarized_transcript(pg_engine, meeting_id)
        with pg_engine.connect() as conn:
            original_segments = conn.execute(text(
                "SELECT segments FROM meeting_analyzer.transcripts WHERE id=:t"
            ), {"t": transcript_id}).scalar_one()
        profile = api.post("/profiles", json={"display_name": "Alice"}).json()
        self._enroll_ref(api, profile["id"], "spk-a")

        _run_profiling_job(api, meeting_id)

        speaker = _speaker_by_label(api, meeting_id, "spk-a")
        with pg_engine.connect() as conn:
            assert conn.execute(text(
                "SELECT segments FROM meeting_analyzer.transcripts WHERE id=:t"
            ), {"t": transcript_id}).scalar_one() == original_segments
            origin = conn.execute(text(
                "SELECT origin FROM meeting_analyzer.speaker_assignments WHERE transcript_id=:t AND local_label='spk-a'"
            ), {"t": transcript_id}).scalar_one()
        assert speaker["status"] == "confirmed"
        assert speaker["origin"] == "auto"
        assert origin == "auto"
        assert speaker["profile_id"] == profile["id"]
        assert speaker["profile_name"] == "Alice"
        assert speaker["best_score"] >= 0.70
        assert speaker["threshold"] == 0.70
        assert speaker["decision"] == "matched"

    def test_confirmed_profile_is_reused_automatically_in_next_meeting(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        _run_profiling_job(api, meeting_id)

        speaker = _speaker_by_label(api, meeting_id, "spk-a")
        assert speaker["status"] is None

        # Confirm spk-a into a new profile but DO NOT upload reference audio.
        confirmed = api.post(
            f"/meetings/{meeting_id}/speakers/spk-a/assign",
            json={"new_profile_name": "Alice", "status": "confirmed"},
        )
        assert confirmed.status_code == 200, confirmed.text
        profile_id = confirmed.json()["profile_id"]

        user = api.post("/users", json={"display_name": "Host2"}).json()
        from test_api import _ingest_meeting

        meeting2 = _ingest_meeting(api, user["id"], title="Second meeting")["id"]
        _insert_diarized_transcript(pg_engine, meeting2)
        _run_profiling_job(api, meeting2)
        matched = _speaker_by_label(api, meeting2, "spk-a")
        # Reference-only: confirmed meeting assignments are not references.
        assert matched["profile_id"] != profile_id
        assert matched["decision"] in ("no_reference", "below_threshold", "ambiguous")
        assert matched["status"] in (None, "suggested")

        # After explicit reference upload, next profiling can match.
        self._enroll_ref(api, profile_id, "spk-a")
        _run_profiling_job(api, meeting2)
        matched2 = _speaker_by_label(api, meeting2, "spk-a")
        assert matched2["status"] == "confirmed"
        assert matched2["profile_name"] == "Alice"
        assert matched2["origin"] == "auto"

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
        _run_profiling_job(api, meeting_id)
        speaker = _speaker_by_label(api, meeting_id, "spk-b")
        assert speaker["status"] == "rejected"
        assert speaker["origin"] == "manual"

    def test_low_confidence_remains_unknown(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        segments = [
            {"start": 0.0, "end": 5.0, "text": "hi", "speaker": "spk-a"},
            {"start": 5.0, "end": 10.0, "text": "hey", "speaker": "spk-low"},
        ]
        with pg_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO meeting_analyzer.transcripts (id,meeting_id,provider,model,is_primary,text,segments) "
                "VALUES (gen_random_uuid(),:m,'fake','fake-model',true,'t',CAST(:seg AS jsonb))"
            ), {"m": meeting_id, "seg": json.dumps(segments)})
        profile = api.post("/profiles", json={"display_name": "AliceLow"}).json()
        self._enroll_ref(api, profile["id"], "spk-a")
        _run_profiling_job(api, meeting_id)
        speaker = _speaker_by_label(api, meeting_id, "spk-low")
        assert speaker["status"] is None
        assert speaker["profile_id"] is None
        assert speaker["decision"] == "below_threshold"
        assert speaker["best_profile_id"] == profile["id"]
        assert speaker["best_score"] is not None and speaker["best_score"] < 0.70

    def test_ambiguous_match_remains_unknown(
        self, speaker_api, pg_engine, host_and_meeting
    ):
        api, meeting_id = host_and_meeting
        _insert_diarized_transcript(pg_engine, meeting_id)
        p1 = api.post("/profiles", json={"display_name": "Alice"}).json()
        p2 = api.post("/profiles", json={"display_name": "Bob"}).json()
        self._enroll_ref(api, p1["id"], "spk-a")
        self._enroll_ref(api, p2["id"], "spk-b")
        _run_profiling_job(api, meeting_id)
        speaker = _speaker_by_label(api, meeting_id, "spk-c")
        assert speaker["status"] is None
        assert speaker["profile_id"] is None
        assert speaker["decision"] == "ambiguous"

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
                    "INSERT INTO meeting_analyzer.transcripts (id, meeting_id, provider, model, "
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

    def test_reference_only_matching_and_diagnostics(self, speaker_api, pg_engine, host_and_meeting):
        api, meeting_id = host_and_meeting
        transcript_id = _insert_diarized_transcript(pg_engine, meeting_id)
        profile = api.post("/profiles", json={"display_name": "DiagRefUser"}).json()
        with pg_engine.begin() as conn:
            # Isolate: remove references from other tests in this module-scoped fixture.
            conn.execute(text("DELETE FROM meeting_analyzer.speaker_reference_embeddings"))
        _run_profiling_job(api, meeting_id)
        spk = _speaker_by_label(api, meeting_id, "spk-a")
        assert spk["decision"] == "no_reference"
        assert spk["reference_available"] is False
        assert spk["reference_count"] == 0
        assert spk["profiles_evaluated"] == 0

        self._enroll_ref(api, profile["id"], "spk-a")
        _run_profiling_job(api, meeting_id)
        spk = _speaker_by_label(api, meeting_id, "spk-a")
        assert spk["decision"] == "matched"
        assert spk["status"] == "confirmed"
        assert spk["origin"] == "auto"
        assert spk["reference_available"] is True
        assert spk["reference_count"] >= 1
        assert spk["best_profile_name"] == "DiagRefUser"
        assert spk["best_score"] >= 0.70
        assert spk["threshold"] == 0.70
        assert spk["profiles_evaluated"] >= 1
        assert spk["evaluated_at"] is not None

        with pg_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT best_score, decision, profiles_evaluated, match_threshold, evaluated_at FROM meeting_analyzer.speaker_embeddings "
                "WHERE transcript_id=:t AND local_label='spk-a'"
            ), {"t": transcript_id}).mappings().one()
        assert row["decision"] == "matched"
        assert float(row["best_score"]) >= 0.70
        assert float(row["match_threshold"]) == 0.70
        assert int(row["profiles_evaluated"]) >= 1
        assert row["evaluated_at"] is not None

    def test_manual_assignment_preserved_and_auto_recalculated(self, speaker_api, pg_engine, host_and_meeting):
        api, meeting_id = host_and_meeting
        transcript_id = _insert_diarized_transcript(pg_engine, meeting_id)
        p_a = api.post("/profiles", json={"display_name": "ProvA"}).json()
        p_b = api.post("/profiles", json={"display_name": "ProvB"}).json()
        self._enroll_ref(api, p_a["id"], "spk-a")
        self._enroll_ref(api, p_b["id"], "spk-b")
        _run_profiling_job(api, meeting_id)
        assert _speaker_by_label(api, meeting_id, "spk-a")["status"] == "confirmed"
        # Manual override
        api.post(f"/meetings/{meeting_id}/speakers/spk-a/assign", json={"profile_id": p_b["id"], "status": "confirmed"})
        # Reprofile: manual confirmed must survive
        _run_profiling_job(api, meeting_id)
        spk = _speaker_by_label(api, meeting_id, "spk-a")
        assert spk["status"] == "confirmed"
        assert spk["origin"] == "manual"
        assert spk["profile_id"] == p_b["id"]
        # spk-b should be auto recalculated/matched normally
        spk_b = _speaker_by_label(api, meeting_id, "spk-b")
        assert spk_b["origin"] == "auto"
        assert spk_b["status"] == "confirmed"

    def test_older_meeting_reprofiling_uses_current_threshold(self, speaker_api, pg_engine, host_and_meeting):
        api, meeting_id = host_and_meeting
        transcript_id = _insert_diarized_transcript(pg_engine, meeting_id)
        profile = api.post("/profiles", json={"display_name": "Threshold70"}).json()
        self._enroll_ref(api, profile["id"], "spk-a")
        _run_profiling_job(api, meeting_id)

        with pg_engine.begin() as conn:
            conn.execute(text(
                "UPDATE meeting_analyzer.speaker_embeddings SET match_threshold=0.80, evaluated_at=now() - interval '1 day' "
                "WHERE transcript_id=:t AND local_label='spk-a'"
            ), {"t": transcript_id})
        before = _speaker_by_label(api, meeting_id, "spk-a")
        assert before["threshold"] == 0.80

        _run_profiling_job(api, meeting_id)
        after = _speaker_by_label(api, meeting_id, "spk-a")
        assert after["threshold"] == 0.70
        assert after["decision"] == "matched"
        assert after["status"] == "confirmed"
        assert after["evaluated_at"] is not None
        assert after["evaluated_at"] != before["evaluated_at"]
