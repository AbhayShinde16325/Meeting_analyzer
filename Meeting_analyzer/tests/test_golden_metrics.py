"""Unit tests for the golden-evaluation metrics (no network, no audio)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import golden_metrics as gm  # noqa: E402


class TestDuplicateSegmentRatio:
    def test_fewer_than_two_segments_is_zero(self):
        assert gm.duplicate_segment_ratio([]) == 0.0
        assert gm.duplicate_segment_ratio([{"start": 0, "end": 1, "text": "a"}]) == 0.0

    def test_exact_duplicate_counts(self):
        segments = [
            {"start": 0, "end": 1, "text": "hello world"},
            {"start": 1, "end": 2, "text": "hello world."},
        ]
        assert gm.duplicate_segment_ratio(segments) == 0.5

    def test_distinct_segments_are_zero(self):
        segments = [
            {"start": 0, "end": 1, "text": "hello world"},
            {"start": 1, "end": 2, "text": "goodbye world"},
            {"start": 2, "end": 3, "text": "see you later"},
        ]
        assert gm.duplicate_segment_ratio(segments) == 0.0

    def test_chain_of_duplicates(self):
        segments = [
            {"start": 0, "end": 1, "text": "same text"},
            {"start": 1, "end": 2, "text": "same text"},
            {"start": 2, "end": 3, "text": "same text!"},
        ]
        assert gm.duplicate_segment_ratio(segments) == round(2 / 3, 3)


class TestOutOfOrderCount:
    def test_empty_and_single_are_zero(self):
        assert gm.out_of_order_count([]) == 0
        assert gm.out_of_order_count([{"start": 5, "end": 6, "text": "a"}]) == 0

    def test_counts_violations(self):
        segments = [
            {"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 3.0, "end": 4.0, "text": "b"},
            {"start": 0.5, "end": 1.0, "text": "c"},
            {"start": 0.25, "end": 0.5, "text": "d"},
        ]
        assert gm.out_of_order_count(segments) == 2

    def test_ties_are_not_violations(self):
        segments = [
            {"start": 1.0, "end": 2.0, "text": "a"},
            {"start": 1.0, "end": 2.5, "text": "b"},
        ]
        assert gm.out_of_order_count(segments) == 0


class TestNormalizeText:
    def test_lowercases_and_strips_punctuation(self):
        assert gm.normalize_text("Hello, World!") == "hello world"

    def test_preserves_devanagari(self):
        assert gm.normalize_text("सभी को नमस्कार।") == "सभी को नमस्कार"

    def test_removes_danda(self):
        assert "।" not in gm.normalize_text("आज हम करेंगे।")

    def test_collapses_whitespace(self):
        assert gm.normalize_text("  a\t\nb  ") == "a b"

    def test_empty(self):
        assert gm.normalize_text("") == ""


class TestWer:
    def test_perfect_match(self):
        assert gm.wer("the build failed again", "The build failed again!") == 0.0

    def test_one_substitution(self):
        assert gm.wer("a b c", "a x c") == 1 / 3

    def test_insertion_and_deletion(self):
        assert gm.wer("a b", "a x b c") == 2 / 2

    def test_empty_hypothesis_scores_one(self):
        assert gm.wer("a b c", "") == 1.0

    def test_empty_reference_scores_zero(self):
        assert gm.wer("", "anything") == 0.0

    def test_capped_at_one(self):
        assert gm.wer("a", "x y z w") == 1.0


class TestCer:
    def test_perfect_match(self):
        assert gm.cer("namaskar", "na ma s ka r".replace(" ", "")) == 0.0

    def test_whitespace_ignored(self):
        assert gm.cer("sabhi ko", "sabhiko") == 0.0

    def test_single_char_error(self):
        assert gm.cer("abc", "abd") == 1 / 3


class TestScriptsAndKeyTerms:
    def test_has_devanagari(self):
        assert gm.has_devanagari("नमस्कार")
        assert not gm.has_devanagari("namaskar")

    def test_script_matches_english_rejects_devanagari(self):
        assert gm.script_matches_language("hello", "english") is True
        assert gm.script_matches_language("नमस्कार", "english") is False

    def test_key_terms_keep_short_date_words(self):
        terms = gm.key_terms("We deploy on Friday, ok?")
        assert "friday" in terms
        assert "ok" not in terms

    def test_key_term_recall(self):
        reference = "The PostgreSQL connection pool is exhausted. We deploy Friday."
        assert gm.key_term_recall(reference, reference) == 1.0
        assert gm.key_term_recall(reference, "PostgreSQL pool exhausted friday") < 1.0
        assert gm.key_term_recall("", "anything") == 1.0


class TestTimestampMetrics:
    def test_monotonic_and_coverage(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a"},
            {"start": 2.0, "end": 4.0, "text": "b"},
        ]
        metrics = gm.timestamp_metrics(segments, 5.0)
        assert metrics["segment_count"] == 2
        assert metrics["timestamps_monotonic"] is True
        assert metrics["duration_coverage"] == 0.8

    def test_overlaps_count_once(self):
        segments = [
            {"start": 0.0, "end": 3.0, "text": "a"},
            {"start": 2.0, "end": 4.0, "text": "b"},
        ]
        metrics = gm.timestamp_metrics(segments, 4.0)
        assert metrics["duration_coverage"] == 1.0

    def test_backwards_timestamps_flagged(self):
        segments = [
            {"start": 2.0, "end": 1.0, "text": "a"},
        ]
        assert gm.timestamp_metrics(segments, 5.0)["timestamps_monotonic"] is False

    def test_no_duration_no_crash(self):
        metrics = gm.timestamp_metrics([], None)
        assert metrics["duration_coverage"] is None


class TestSpeakerMetrics:
    def test_no_speaker_labels_returns_none(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "hello"}]
        metrics = gm.speaker_metrics(segments, ["hello"])
        assert metrics["attribution_accuracy"] is None
        assert metrics["speaker_count_error"] is None

    def test_exact_two_speakers(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "the api integration is complete",
             "speaker": "Speaker SPEAKER_00"},
            {"start": 2.0, "end": 4.0, "text": "i will review the database migration",
             "speaker": "Speaker SPEAKER_01"},
        ]
        utterances = [
            "The API integration is complete.",
            "I will review the database migration.",
        ]
        metrics = gm.speaker_metrics(segments, utterances)
        assert metrics["expected_speakers"] == 2
        assert metrics["found_speakers"] == 2
        assert metrics["speaker_count_error"] == 0
        assert metrics["attribution_accuracy"] == 1.0
        assert metrics["matched_utterances"] == 2

    def test_speaker_count_error(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "hello there everyone", "speaker": "A"},
        ]
        utterances = ["hello there everyone", "second speaker talks"]
        metrics = gm.speaker_metrics(segments, utterances)
        assert metrics["speaker_count_error"] == 1

    def test_real_meeting_count_without_utterances(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "one", "speaker": "A"},
            {"start": 2.0, "end": 4.0, "text": "two", "speaker": "B"},
            {"start": 4.0, "end": 6.0, "text": "three", "speaker": "C"},
        ]
        metrics = gm.speaker_metrics(segments, [], expected_speaker_count=3)
        assert metrics["expected_speakers"] == 3
        assert metrics["speaker_count_error"] == 0
        assert metrics["attribution_accuracy"] is None


class TestCompositeScore:
    def test_perfect_with_diarization(self):
        score = gm.composite_score(
            wer_value=0.0,
            key_term_recall_value=1.0,
            language_ok=True,
            diarization_accuracy=1.0,
            structure_value=1.0,
        )
        assert score == 1.0

    def test_perfect_without_diarization(self):
        score = gm.composite_score(
            wer_value=0.0,
            key_term_recall_value=1.0,
            language_ok=True,
            diarization_accuracy=None,
            structure_value=1.0,
        )
        assert score == 1.0

    def test_worst_case(self):
        score = gm.composite_score(
            wer_value=1.0,
            key_term_recall_value=0.0,
            language_ok=False,
            diarization_accuracy=None,
            structure_value=0.0,
        )
        assert score == 0.0

    def test_language_none_contributes_nothing(self):
        with_lang = gm.composite_score(
            wer_value=0.5, key_term_recall_value=0.5, language_ok=True,
            diarization_accuracy=None, structure_value=0.5,
        )
        without_lang = gm.composite_score(
            wer_value=0.5, key_term_recall_value=0.5, language_ok=None,
            diarization_accuracy=None, structure_value=0.5,
        )
        assert with_lang == round(without_lang + 0.10, 3)
