"""Tests for transcription segment post-processing (sort + dedup)."""

from __future__ import annotations

from ai.segment_postprocess import (
    dedupe_consecutive_segments,
    normalize_for_comparison,
    postprocess_segments,
    sort_segments,
)
from ai.transcription import TranscriptionResult, TranscriptionService


class TestNormalizeForComparison:
    def test_case_punctuation_and_whitespace_are_ignored(self):
        assert (
            normalize_for_comparison("Hello,  World!")
            == normalize_for_comparison("hello world")
        )

    def test_devanagari_text_is_preserved(self):
        assert normalize_for_comparison("गुरुवार दोपहर") == "गुरुवार दोपहर"


class TestSortSegments:
    def test_sorts_out_of_order_segments(self):
        segments = [
            {"start": 4.0, "end": 5.0, "text": "b"},
            {"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 2.5, "end": 3.0, "text": "c"},
        ]
        assert [s["text"] for s in sort_segments(segments)] == ["a", "c", "b"]

    def test_already_sorted_input_is_stable(self):
        segments = [
            {"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 2.0, "end": 3.0, "text": "b"},
        ]
        assert sort_segments(segments) == segments

    def test_zero_timestamps_sort_first(self):
        segments = [
            {"start": 3.0, "end": 4.0, "text": "b"},
            {"start": 0.0, "end": 0.0, "text": "a"},
        ]
        assert [s["text"] for s in sort_segments(segments)] == ["a", "b"]


class TestDedupeConsecutiveSegments:
    def test_exact_duplicate_is_removed(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "hello world"},
            {"start": 1.0, "end": 2.0, "text": "hello world"},
        ]
        kept, removed = dedupe_consecutive_segments(segments)
        assert removed == 1
        assert [s["text"] for s in kept] == ["hello world"]

    def test_echo_duplicate_with_different_speaker_is_removed(self):
        # EN06-shaped case: the echo reflection arrives under a
        # hallucinated second speaker label.
        segments = [
            {"start": 0.0, "end": 2.0, "text": "Please review the report.", "speaker": "Speaker 0"},
            {"start": 2.5, "end": 4.5, "text": "please review the report", "speaker": "Speaker 1"},
        ]
        kept, removed = dedupe_consecutive_segments(segments)
        assert removed == 1
        assert kept[0]["speaker"] == "Speaker 0"

    def test_distinct_texts_are_kept(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "hello world"},
            {"start": 1.0, "end": 2.0, "text": "goodbye world"},
        ]
        kept, removed = dedupe_consecutive_segments(segments)
        assert removed == 0
        assert len(kept) == 2

    def test_chain_of_three_duplicates_keeps_only_first(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "same text"},
            {"start": 1.0, "end": 2.0, "text": "Same text."},
            {"start": 2.0, "end": 3.0, "text": "SAME TEXT!"},
        ]
        kept, removed = dedupe_consecutive_segments(segments)
        assert removed == 2
        assert [s["text"] for s in kept] == ["same text"]

    def test_repeated_phrase_after_intervening_speech_is_kept(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "hello world"},
            {"start": 1.0, "end": 2.0, "text": "how are you"},
            {"start": 2.0, "end": 3.0, "text": "hello world"},
        ]
        kept, removed = dedupe_consecutive_segments(segments)
        assert removed == 0
        assert len(kept) == 3


class TestPostprocessSegments:
    def test_sorts_then_dedupes(self):
        segments = [
            {"start": 4.0, "end": 6.0, "text": "hello world"},
            {"start": 1.0, "end": 2.0, "text": "hello world"},
            {"start": 5.5, "end": 7.0, "text": "hello world."},
        ]
        kept, removed = postprocess_segments(segments)
        assert removed == 2
        assert [s["start"] for s in kept] == [1.0]


class TestServiceIntegration:
    def test_service_sorts_and_rebuilds_text_on_dedup(self, settings, wav_factory):
        path = wav_factory(settings.recordings_dir / "echo.wav")

        class EchoProvider:
            def transcribe(self, audio_path, *, language=None, progress_cb=None):
                return TranscriptionResult(
                    text="hello world\nhello world",
                    language="en",
                    segments=[
                        {"start": 2.0, "end": 3.0, "text": "hello world"},
                        {"start": 0.5, "end": 1.5, "text": "hello world"},
                    ],
                )

        result = TranscriptionService(settings, provider=EchoProvider()).transcribe(path)
        assert len(result.segments) == 1
        assert result.segments[0]["start"] == 0.5
        assert result.text == "hello world"

    def test_service_keeps_text_untouched_when_nothing_deduped(
        self, settings, wav_factory
    ):
        path = wav_factory(settings.recordings_dir / "clean.wav")

        class CleanProvider:
            def transcribe(self, audio_path, *, language=None, progress_cb=None):
                return TranscriptionResult(
                    text="First line.\nSecond line.",
                    language="en",
                    segments=[
                        {"start": 0.0, "end": 1.0, "text": "First line."},
                        {"start": 1.0, "end": 2.0, "text": "Second line."},
                    ],
                )

        result = TranscriptionService(settings, provider=CleanProvider()).transcribe(path)
        assert result.text == "First line.\nSecond line."
        assert len(result.segments) == 2
