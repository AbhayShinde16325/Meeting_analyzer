"""Unit tests for the eight post-transcription metrics."""

from __future__ import annotations

import pytest

from services.transcript_metrics import compute_transcript_metrics


def _compute(segments, *, audio=10.0, transcription=5.0, **overrides):
    kwargs: dict = {
        "segments": segments,
        "audio_duration_s": audio,
        "transcription_duration_s": transcription,
        "transcription_status": "succeeded",
        "error": None,
    }
    kwargs.update(overrides)
    return compute_transcript_metrics(**kwargs)


class TestDurationsAndRtf:
    def test_durations_and_rtf_measured(self):
        metrics = _compute(
            [{"start": 0.0, "end": 2.0, "text": "a", "speaker": "A"}],
            audio=10.0,
            transcription=5.0,
        )
        assert metrics["audio_duration_s"] == 10.0
        assert metrics["transcription_duration_s"] == 5.0
        assert metrics["rtf"] == 0.5

    def test_rtf_is_none_without_audio_duration(self):
        metrics = _compute([], audio=None, transcription=5.0)
        assert metrics["rtf"] is None
        assert metrics["audio_duration_s"] is None

    def test_rtf_is_none_for_zero_audio_duration(self):
        metrics = _compute([], audio=0.0, transcription=5.0)
        assert metrics["rtf"] is None

    def test_rtf_is_none_without_transcription_duration(self):
        metrics = _compute([], audio=10.0, transcription=None)
        assert metrics["rtf"] is None
        assert metrics["transcription_duration_s"] is None


class TestSpeakerMetrics:
    def test_num_speakers_counts_distinct_labels(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "Alice"},
            {"start": 2.0, "end": 4.0, "text": "b", "speaker": "Bob"},
            {"start": 4.0, "end": 6.0, "text": "c", "speaker": "Alice"},
        ]
        assert _compute(segments)["num_speakers"] == 2

    def test_num_speakers_none_without_labels(self):
        segments = [{"start": 0.0, "end": 2.0, "text": "a"}]
        assert _compute(segments)["num_speakers"] is None

    def test_percentages_derived_from_duration_not_counts(self):
        # One long labelled segment (8s) and one short unlabelled (2s):
        # counts would say 50/50, durations say 80/20.
        segments = [
            {"start": 0.0, "end": 8.0, "text": "a", "speaker": "Alice"},
            {"start": 8.0, "end": 10.0, "text": "b"},
        ]
        metrics = _compute(segments)
        assert metrics["labelled_speech_pct"] == 80.0
        assert metrics["unknown_speech_pct"] == 20.0

    def test_percentages_none_without_any_speech(self):
        metrics = _compute([])
        assert metrics["labelled_speech_pct"] is None
        assert metrics["unknown_speech_pct"] is None
        assert metrics["num_speakers"] is None

    def test_all_unknown_speech_is_100_percent_unknown(self):
        segments = [{"start": 0.0, "end": 4.0, "text": "a"}]
        metrics = _compute(segments)
        assert metrics["labelled_speech_pct"] == 0.0
        assert metrics["unknown_speech_pct"] == 100.0


class TestReviewSegments:
    def test_unlabelled_segments_need_review(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "A"},
            {"start": 2.0, "end": 4.0, "text": "b"},
        ]
        assert _compute(segments)["review_segments"] == 1

    def test_invalid_timestamps_need_review(self):
        segments = [
            {"start": 5.0, "end": 2.0, "text": "backwards", "speaker": "A"},
        ]
        metrics = _compute(segments)
        assert metrics["review_segments"] == 1
        # The backwards segment contributes no speech duration, so the
        # percentages are undefined rather than zero.
        assert metrics["labelled_speech_pct"] is None
        assert metrics["unknown_speech_pct"] is None

    def test_clean_segments_need_no_review(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "A"},
            {"start": 2.0, "end": 4.0, "text": "b", "speaker": "B"},
        ]
        assert _compute(segments)["review_segments"] == 0


class TestStatusAndErrors:
    def test_status_and_error_recorded(self):
        metrics = _compute([], transcription_status="succeeded", error=None)
        assert metrics["transcription_status"] == "succeeded"
        assert metrics["error"] is None

    def test_error_field_carries_failure_text(self):
        metrics = _compute(
            [],
            transcription_status="failed",
            error="Sarvam reported a failed job: bad audio",
        )
        assert metrics["transcription_status"] == "failed"
        assert "bad audio" in metrics["error"]

    def test_fallback_info_recorded(self):
        metrics = _compute(
            [], fallback_from="sarvam", fallback_reason="network down"
        )
        assert metrics["fallback_from"] == "sarvam"
        assert metrics["fallback_reason"] == "network down"

    def test_non_dict_segments_are_ignored(self):
        metrics = _compute(["not a dict", None])
        assert metrics["num_speakers"] is None
        assert metrics["labelled_speech_pct"] is None
        assert metrics["unknown_speech_pct"] is None
        assert metrics["review_segments"] == 0
