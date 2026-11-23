"""API contract tests against real PostgreSQL via FastAPI's TestClient.

The app under test is wired exactly like production (engine + schema + job
runner) except that providers are faked, so every HTTP contract — including
attachment validation, Range streaming, export ZIPs and hard deletion with
Large Object cleanup — is exercised end to end without a network.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import text

from ai.transcription import TranscriptionResult


@pytest.fixture(scope="module")
def api(pg_engine):
    """FastAPI TestClient wired to the scratch database, providers faked."""

    import os
    from unittest.mock import patch

    import api.app as app_module
    from fastapi.testclient import TestClient

    # Point the app's own engine wiring at the scratch database.
    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = str(pg_engine.url)
    try:
        with (
            patch.object(
                app_module, "_build_transcription_service", _build_fake_transcription
            ),
            patch.object(app_module, "_build_summarizer", _build_fake_summarizer),
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


class _NullRunner:
    """Stands in for JobRunner so tests drive the pipeline synchronously."""

    def __init__(self, *args, **kwargs):
        self.engine = args[0] if args else kwargs.get("engine")
        self.pipeline = args[1] if len(args) > 1 else kwargs.get("pipeline")

    def start(self):
        pass

    def stop(self):
        pass


def _build_fake_transcription():
    from ai.transcription import TranscriptionService
    from config import load_settings

    class _Provider:
        provider_name = "fake"
        model_name = "fake-model"
        transcript_mode = None

        def transcribe(self, audio_path, *, language=None, progress_cb=None,
                       cancel_cb=None):
            return TranscriptionResult(
                text="Fake primary transcript content.",
                language="en",
                segments=[{"start": 0.0, "end": 1.0,
                           "text": "Fake primary transcript content."}],
            )

    return TranscriptionService(load_settings(), provider=_Provider())


def _build_fake_summarizer():
    from ai.summarization import MeetingSummarizer
    from config import load_settings
    from support import valid_analysis

    class _AnalysisProvider:
        model_name = "fake-analysis"

        def analyze(self, *, system_prompt, user_prompt):
            return valid_analysis()

    return MeetingSummarizer(load_settings(), provider=_AnalysisProvider())


@pytest.fixture(scope="module")
def pg_engine_module(pg_engine):
    return pg_engine


@pytest.fixture(autouse=True)
def _clean(pg_engine):
    from pg_harness import truncate_all

    truncate_all(pg_engine)


@pytest.fixture
def user_headers_and_id(api):
    response = api.post("/users", json={"display_name": "Test Host"})
    user = response.json()
    return {"X-Profile-Id": user["id"]}, user["id"]


def _ingest_meeting(api, user_id, *, title="API meeting"):
    """Create a meeting through the API by uploading a small WAV."""

    import wave
    import struct

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x00\x00" * (48000 // 10) * 2)
    response = api.post(
        "/meetings",
        data={"title": title, "user_id": user_id, "duration_ms": "100",
              "sample_rate": "48000", "channels": "2"},
        files={"file": ("audio.wav", buffer.getvalue(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestUsers:
    def test_create_and_list(self, api):
        response = api.post("/users", json={"display_name": "Alice"})
        assert response.status_code == 201
        assert response.json()["display_name"] == "Alice"
        duplicate = api.post("/users", json={"display_name": "Alice"})
        assert duplicate.status_code == 409
        listing = api.get("/users").json()
        assert [u["display_name"] for u in listing] == ["Alice"]


class TestMeetingCreation:
    def test_ingest_creates_meeting_and_files(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        assert meeting["status"] == "recorded"
        files = api.get(f"/meetings/{meeting['id']}/files").json()
        kinds = sorted(f["kind"] for f in files)
        assert kinds == ["derived_audio_16k", "original_audio_48k"]

    def test_rejects_empty_upload(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        response = api.post(
            "/meetings",
            data={"title": "Empty", "user_id": user_id},
            files={"file": ("audio.wav", b"RIFFxx", "audio/wav")},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_recording"

    def test_remote_profile_cannot_create(self, api):
        user = api.post("/users", json={"display_name": "Remote"}).json()
        response = api.post(
            "/meetings",
            headers={"X-Profile-Id": user["id"]},
            data={"title": "Nope"},
            files={"file": ("a.wav", b"RIFFxx", "audio/wav")},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "host_only"


class TestStreaming:
    def test_full_download_matches_stored_bytes(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        files = api.get(f"/meetings/{meeting['id']}/files").json()
        original = next(f for f in files if f["kind"] == "original_audio_48k")
        response = api.get(f"/files/{original['id']}")
        assert response.status_code == 200
        assert response.headers["X-File-SHA256"] == original["sha256"]
        import hashlib

        assert hashlib.sha256(response.content).hexdigest() == original["sha256"]

    def test_range_request(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        files = api.get(f"/meetings/{meeting['id']}/files").json()
        original = next(f for f in files if f["kind"] == "original_audio_48k")
        response = api.get(
            f"/files/{original['id']}", headers={"Range": "bytes=10-19"}
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"].startswith("bytes 10-19/")
        assert len(response.content) == 10

    def test_download_content_disposition(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        files = api.get(f"/meetings/{meeting['id']}/files").json()
        original = next(f for f in files if f["kind"] == "original_audio_48k")
        response = api.get(f"/files/{original['id']}/download")
        assert "attachment" in response.headers["Content-Disposition"]


class TestAttachments:
    def _attach(self, api, meeting_id, *, filename, content_type, payload=b"hello"):
        return api.post(
            f"/meetings/{meeting_id}/files",
            files={"file": (filename, payload, content_type)},
        )

    def test_valid_attachment(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = self._attach(
            api, meeting["id"], filename="notes.pdf",
            content_type="application/pdf", payload=b"%PDF-1.4 fake",
        )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "attachment"

    def test_rejects_wrong_extension(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = self._attach(
            api, meeting["id"], filename="virus.exe",
            content_type="application/octet-stream",
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "attachment_extension"

    def test_rejects_wrong_mime(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = self._attach(
            api, meeting["id"], filename="a.pdf", content_type="application/zip"
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "attachment_mime"

    def test_rejects_oversize(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = self._attach(
            api, meeting["id"], filename="big.pdf",
            content_type="application/pdf", payload=b"x" * (50 * 1024 * 1024 + 1),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "attachment_too_large"

    def test_delete_attachment_removes_lo(self, api, user_headers_and_id, pg_engine):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        created = self._attach(
            api, meeting["id"], filename="doc.docx",
            content_type="application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        ).json()
        with pg_engine.connect() as conn:
            before = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM pg_largeobject_metadata")
                ).scalar_one()
            )
        response = api.delete(f"/files/{created['id']}")
        assert response.status_code == 204
        with pg_engine.connect() as conn:
            after = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM pg_largeobject_metadata")
                ).scalar_one()
            )
        assert after == before - 1


def _transcribe(api, meeting_id):
    """Return the queued transcription job created by ingest."""

    jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
    queued = [j for j in jobs if j["job_type"] == "transcription" and j["status"] == "queued"]
    assert queued, f"No queued transcription job: {jobs}"
    return queued[0]


class TestTranscriptAndAnalysis:
    def test_transcription_flow_creates_primary_transcript(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        # Re-transcribing via the endpoint also works after the first pass;
        # here the ingest-queued job is used directly.
        job = _transcribe(api, meeting["id"])
        _run_job(api, job["id"])
        jobs = api.get(f"/meetings/{meeting['id']}/jobs").json()
        assert jobs[0]["status"] == "succeeded"
        transcripts = api.get(f"/meetings/{meeting['id']}/transcripts").json()
        assert len(transcripts) == 1
        assert transcripts[0]["is_primary"] is True
        assert transcripts[0]["text"] == "Fake primary transcript content."

    def test_cancel_active_job(self, api, user_headers_and_id):
        """Cancelling a queued job works and leaves no transcript behind.

        The cancel UPDATE inlined the message ``Cancelled at the user's
        request.`` into a single-quoted SQL literal, so every cancel failed
        with a PostgreSQL syntax error surfaced as HTTP 503.
        """

        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        job = _transcribe(api, meeting["id"])

        response = api.post(f"/meetings/{meeting['id']}/jobs/{job['id']}/cancel")

        assert response.status_code == 200, response.text
        cancelled = response.json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["error"] == "Cancelled at the user's request."
        assert api.get(f"/meetings/{meeting['id']}/transcripts").json() == []

        again = api.post(f"/meetings/{meeting['id']}/jobs/{job['id']}/cancel")
        assert again.status_code == 404
        assert again.json()["error"]["code"] == "job_not_cancellable"

    def test_analysis_flow(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        _run_job(api, _transcribe(api, meeting["id"])["id"])
        response = api.post(f"/meetings/{meeting['id']}/analyse")
        assert response.status_code == 202, response.text
        _run_job(api, response.json()["id"])
        summary = api.get(f"/meetings/{meeting['id']}/summary").json()
        assert summary["overview"] == "The team agreed to ship the release."
        items = api.get(f"/meetings/{meeting['id']}/action-items").json()
        assert items[0]["task"] == "Ship the release"
        patched = api.patch(
            f"/action-items/{items[0]['id']}", json={"status": "done"}
        )
        assert patched.json()["status"] == "done"
        meeting_details = api.get(f"/meetings/{meeting['id']}").json()
        assert meeting_details["status"] == "completed"

    def test_reanalysis_replaces_the_previous_summary(
        self, api, user_headers_and_id, pg_engine
    ):
        """Re-analysing must replace the stored summary, not violate its key.

        ``summaries`` allows one row per meeting, and transcription already
        chains one analysis, so a second analysis run used to fail with an
        IntegrityError and leave the job unsuccessful.
        """

        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        _run_job(api, _transcribe(api, meeting["id"])["id"])
        first_items = api.get(f"/meetings/{meeting['id']}/action-items").json()
        assert len(first_items) == 1

        response = api.post(f"/meetings/{meeting['id']}/analyse")
        assert response.status_code == 202, response.text
        _run_job(api, response.json()["id"])

        jobs = api.get(f"/meetings/{meeting['id']}/jobs").json()
        assert jobs[0]["status"] == "succeeded", jobs[0]
        summary = api.get(f"/meetings/{meeting['id']}/summary").json()
        assert summary["overview"] == "The team agreed to ship the release."
        items = api.get(f"/meetings/{meeting['id']}/action-items").json()
        assert len(items) == 1
        # The surviving item belongs to the *new* summary row, so the old
        # summary/items really were replaced rather than orphaned.
        with pg_engine.connect() as conn:
            summary_ids = conn.execute(
                text("SELECT summary_id FROM action_items WHERE meeting_id = :m"),
                {"m": meeting["id"]},
            ).scalars().all()
        assert summary_ids == [uuid.UUID(summary["id"])]
        assert api.get(f"/meetings/{meeting['id']}").json()["status"] == "completed"


class TestMeetingListing:
    """Regression cover for ``GET /meetings`` (dashboard list + search box).

    The filter/query SQL once used ``:name::type`` casts, which SQLAlchemy does
    not parse as bind parameters, so every call ended in a PostgreSQL syntax
    error surfaced as HTTP 503.  These tests fail on that bug and pass only when
    the casts are valid SQL.
    """

    def test_list_returns_meetings(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        first = _ingest_meeting(api, user_id, title="First meeting")
        second = _ingest_meeting(api, user_id, title="Second meeting")

        response = api.get("/meetings")

        assert response.status_code == 200, response.text
        rows = response.json()
        assert {row["title"] for row in rows} == {"First meeting", "Second meeting"}
        assert rows[0]["id"] == second["id"]
        assert all(row["status"] == "recorded" for row in rows)
        assert first["id"] in {row["id"] for row in rows}

    def test_list_filters_by_status_and_user(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        mine = _ingest_meeting(api, user_id, title="Mine")
        other = api.post("/users", json={"display_name": "Other"}).json()
        theirs = _ingest_meeting(api, other["id"], title="Theirs")

        by_user = api.get("/meetings", params={"user_id": user_id})
        assert by_user.status_code == 200, by_user.text
        assert [row["id"] for row in by_user.json()] == [mine["id"]]

        by_status = api.get("/meetings", params={"status": "recorded"})
        assert by_status.status_code == 200, by_status.text
        assert {row["id"] for row in by_status.json()} == {mine["id"], theirs["id"]}

        empty = api.get("/meetings", params={"status": "completed"})
        assert empty.status_code == 200, empty.text
        assert empty.json() == []

        invalid = api.get("/meetings", params={"status": "not-a-status"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_status"

    def test_list_query_matches_title_and_transcript(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id, title="Quarterly planning")
        _ingest_meeting(api, user_id, title="Unrelated retro")
        _run_job(api, _transcribe(api, meeting["id"])["id"])

        by_title = api.get("/meetings", params={"query": "Quarterly"})
        assert by_title.status_code == 200, by_title.text
        assert [row["id"] for row in by_title.json()] == [meeting["id"]]

        by_transcript = api.get("/meetings", params={"query": "Fake primary"})
        assert by_transcript.status_code == 200, by_transcript.text
        assert [row["id"] for row in by_transcript.json()] == [meeting["id"]]

        no_hits = api.get("/meetings", params={"query": "nothing-matches-this"})
        assert no_hits.status_code == 200, no_hits.text
        assert no_hits.json() == []

    def test_list_paginates(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        for index in range(3):
            _ingest_meeting(api, user_id, title=f"Meeting {index}")

        page = api.get("/meetings", params={"limit": 2, "offset": 0})
        assert page.status_code == 200, page.text
        assert len(page.json()) == 2

        rest = api.get("/meetings", params={"limit": 2, "offset": 2})
        assert rest.status_code == 200, rest.text
        assert len(rest.json()) == 1


@pytest.fixture(scope="module")
def pg_engine_module(pg_engine):
    return pg_engine


def _run_job(api, job_id):
    """Drive the pipeline synchronously (same code the runner thread uses)."""

    api.app.state.pipeline.run_job(job_id)


class TestDeletion:
    def test_delete_meeting_is_permanent_and_removes_los(
        self, api, user_headers_and_id, pg_engine
    ):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.post(
            f"/meetings/{meeting['id']}/files",
            files={"file": ("a.pdf", b"%PDF", "application/pdf")},
        )
        with pg_engine.connect() as conn:
            before = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM pg_largeobject_metadata")
                ).scalar_one()
            )
        response = api.delete(f"/meetings/{meeting['id']}")
        assert response.status_code == 204
        assert api.get(f"/meetings/{meeting['id']}").status_code == 404
        with pg_engine.connect() as conn:
            after = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM pg_largeobject_metadata")
                ).scalar_one()
            )
        # 3 LOs existed (2 audio + 1 attachment) and all are gone.
        assert after == before - 3

    def test_remote_profile_cannot_delete(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        remote = api.post("/users", json={"display_name": "R"}).json()
        response = api.delete(
            f"/meetings/{meeting['id']}", headers={"X-Profile-Id": remote["id"]}
        )
        assert response.status_code == 403


class TestExport:
    def test_export_zip_contains_everything(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.post(
            f"/meetings/{meeting['id']}/files",
            files={"file": ("notes.txt", b"attached text", "text/plain")},
        )
        _run_job(api, _transcribe(api, meeting["id"])["id"])
        response = api.get(f"/meetings/{meeting['id']}/export")
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        assert "meeting.json" in names
        assert "transcripts/primary.txt" in names
        assert "attachments/notes.txt" in names
        assert any(n.startswith("audio/") for n in names)
        metadata = json.loads(archive.read("meeting.json"))
        assert metadata["meeting"]["title"] == "API meeting"


class TestSearchAndSystem:
    def test_search_finds_transcript(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        _run_job(api, _transcribe(api, meeting["id"])["id"])
        hits = api.get("/search", params={"q": "Fake primary"}).json()
        assert hits
        assert hits[0]["kind"] == "transcript"
        assert hits[0]["meeting_id"] == meeting["id"]

    def test_system_status(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        _ingest_meeting(api, user_id)
        status = api.get("/system/status").json()
        assert status["meeting_count"] == 1
        assert status["db_size_bytes"] > 0
        assert status["schema_version"] == "0002_speaker_profiles"
        assert status["can_record"] is True

    def test_health(self, api):
        response = api.get("/health").json()
        assert response["status"] == "ok"
        assert response["schema"] == "0002_speaker_profiles"
