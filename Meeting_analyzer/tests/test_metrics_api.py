"""Post-transcription metrics tests against real PostgreSQL."""

from __future__ import annotations

import pytest

# Re-exported fixtures from test_api (default app, providers faked).
from test_api import api, user_headers_and_id  # noqa: F401
from test_api import _ingest_meeting


@pytest.fixture(autouse=True)
def _clean(pg_engine):
    from pg_harness import truncate_all

    truncate_all(pg_engine)


def _transcription_job(api, meeting_id: str) -> str:
    jobs = api.get(f"/meetings/{meeting_id}/jobs").json()
    return next(
        j["id"]
        for j in jobs
        if j["job_type"] == "transcription" and j["status"] == "queued"
    )


class TestMetricsPersistence:
    def test_transcription_persists_metrics(
        self, api, user_headers_and_id
    ):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.app.state.pipeline.run_job(_transcription_job(api, meeting["id"]))

        response = api.get(f"/meetings/{meeting['id']}/metrics")
        assert response.status_code == 200
        row = response.json()
        assert row is not None
        metrics = row["metrics"]
        # Measured metrics from the fake provider's single (unlabelled)
        # 1-second segment over a 0.1 s meeting duration. Opus containerization
        # can introduce small duration rounding vs raw WAV.
        assert metrics["audio_duration_s"] == pytest.approx(0.1, abs=0.02)
        assert metrics["transcription_duration_s"] >= 0.0
        assert metrics["rtf"] is None or metrics["rtf"] > 0
        assert metrics["num_speakers"] is None
        assert metrics["unknown_speech_pct"] == 100.0
        assert metrics["labelled_speech_pct"] == 0.0
        assert metrics["review_segments"] == 1  # missing speaker label
        assert metrics["transcription_status"] == "succeeded"
        assert metrics["error"] is None

    def test_metrics_associated_with_primary_transcript(
        self, api, user_headers_and_id
    ):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.app.state.pipeline.run_job(_transcription_job(api, meeting["id"]))

        transcripts = api.get(f"/meetings/{meeting['id']}/transcripts").json()
        primary = next(t for t in transcripts if t["is_primary"])
        row = api.get(f"/meetings/{meeting['id']}/metrics").json()
        assert str(row["transcript_id"]) == str(primary["id"])

    def test_retranscription_replaces_metrics(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.app.state.pipeline.run_job(_transcription_job(api, meeting["id"]))
        first = api.get(f"/meetings/{meeting['id']}/metrics").json()

        # Re-transcribe: a fresh transcription run replaces the metrics row.
        new_job = api.post(f"/meetings/{meeting['id']}/transcribe")
        assert new_job.status_code == 202
        api.app.state.pipeline.run_job(new_job.json()["id"])
        second = api.get(f"/meetings/{meeting['id']}/metrics").json()

        assert second is not None
        assert second["transcript_id"] != first["transcript_id"]
        transcripts = api.get(f"/meetings/{meeting['id']}/transcripts").json()
        primary = next(t for t in transcripts if t["is_primary"])
        assert str(second["transcript_id"]) == str(primary["id"])

    def test_no_metrics_before_transcription(self, api, user_headers_and_id):
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        response = api.get(f"/meetings/{meeting['id']}/metrics")
        assert response.status_code == 200
        assert response.json() is None

    def test_metrics_unknown_meeting_is_404(self, api):
        response = api.get("/meetings/00000000-0000-0000-0000-000000000000/metrics")
        assert response.status_code == 404

    def test_metrics_failure_does_not_fail_transcription(
        self, api, user_headers_and_id, monkeypatch
    ):
        """A broken metrics computation only skips metrics."""

        def broken(*args, **kwargs):
            raise RuntimeError("metrics exploded")

        monkeypatch.setattr(
            "services.pipeline.compute_transcript_metrics", broken
        )
        _headers, user_id = user_headers_and_id
        meeting = _ingest_meeting(api, user_id)
        api.app.state.pipeline.run_job(_transcription_job(api, meeting["id"]))

        # Transcription succeeded; no metrics row exists.
        response = api.get(f"/meetings/{meeting['id']}/metrics")
        assert response.status_code == 200
        assert response.json() is None
        transcripts = api.get(f"/meetings/{meeting['id']}/transcripts").json()
        assert any(t["is_primary"] for t in transcripts)
