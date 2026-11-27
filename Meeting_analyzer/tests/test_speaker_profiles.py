"""Unit tests for voice-profile matching and embedding aggregation."""

from __future__ import annotations

import importlib

import numpy as np
import pytest

from services import speaker_profiles as sp
from services.speaker_profiles import (
    LabelEmbedding,
    SpeakerProfilingError,
    cosine_similarity,
    embeddings_from_wav,
    encoder_import_error,
    mean_embedding,
)

SAMPLE_RATE = 16_000


def _unit(n, index):
    vector = [0.0] * n
    vector[index] = 1.0
    return vector


class TestCosineSimilarity:
    def test_identical_vectors_are_one(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_are_minus_one(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_is_zero_not_error(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


class TestMeanEmbedding:
    def test_averages_elementwise(self):
        assert mean_embedding([[1.0, 3.0], [3.0, 5.0]]) == [2.0, 4.0]

    def test_empty_input_raises(self):
        with pytest.raises(SpeakerProfilingError):
            mean_embedding([])


class TestEmbeddingsFromWav:
    def _wav(self, seconds: float) -> np.ndarray:
        return np.ones(int(seconds * SAMPLE_RATE), dtype=np.float32)

    def test_groups_by_label_and_averages(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "Speaker 0"},
            {"start": 2.0, "end": 4.0, "text": "b", "speaker": "Speaker 0"},
            {"start": 4.0, "end": 6.0, "text": "c", "speaker": "Speaker 1"},
        ]
        calls: list[int] = []

        def embed(chunk):
            calls.append(len(chunk))
            return _unit(4, len(calls) - 1)

        results = embeddings_from_wav(
            self._wav(6.0),
            segments,
            embed_utterance=embed,
            min_seconds=3.0,
            max_segments_per_speaker=20,
        )
        by_label = {r.local_label: r for r in results}
        # Speaker 0 has 4s of speech: embedding is the mean of two unit
        # vectors scaled by slice length; Speaker 1 has 2s: skipped.
        assert set(by_label) == {"Speaker 0"}
        assert by_label["Speaker 0"].source_seconds == pytest.approx(4.0)
        assert by_label["Speaker 0"].embedding == [0.5, 0.5, 0.0, 0.0]

    def test_labels_below_min_seconds_are_skipped(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "Speaker 0"},
        ]
        results = embeddings_from_wav(
            self._wav(2.0),
            segments,
            embed_utterance=lambda chunk: _unit(2, 0),
            min_seconds=3.0,
            max_segments_per_speaker=20,
        )
        assert results == []

    def test_segments_per_speaker_are_capped(self):
        segments = [
            {"start": float(i), "end": float(i + 1), "text": "a", "speaker": "S"}
            for i in range(6)
        ]
        count = {"n": 0}

        def embed(chunk):
            count["n"] += 1
            return _unit(2, 0)

        results = embeddings_from_wav(
            self._wav(6.0),
            segments,
            embed_utterance=embed,
            min_seconds=1.0,
            max_segments_per_speaker=3,
        )
        assert len(results) == 1
        assert count["n"] == 3

    def test_unlabelled_segments_are_ignored(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a"},
            {"start": 2.0, "end": 4.0, "text": "b", "speaker": "   "},
        ]
        results = embeddings_from_wav(
            self._wav(4.0),
            segments,
            embed_utterance=lambda chunk: _unit(2, 0),
            min_seconds=1.0,
            max_segments_per_speaker=20,
        )
        assert results == []

    def test_empty_wav_raises(self):
        with pytest.raises(SpeakerProfilingError):
            embeddings_from_wav(
                np.zeros(0, dtype=np.float32),
                [{"start": 0.0, "end": 1.0, "text": "a", "speaker": "S"}],
                embed_utterance=lambda chunk: _unit(2, 0),
                min_seconds=1.0,
                max_segments_per_speaker=20,
            )

    def test_progress_reports_per_label(self):
        segments = [
            {"start": 0.0, "end": 2.0, "text": "a", "speaker": "A"},
            {"start": 2.0, "end": 4.0, "text": "b", "speaker": "B"},
        ]
        events: list[tuple[float, str]] = []
        embeddings_from_wav(
            self._wav(4.0),
            segments,
            embed_utterance=lambda chunk: _unit(2, 0),
            min_seconds=1.0,
            max_segments_per_speaker=20,
            progress_cb=lambda p, m: events.append((p, m)),
        )
        assert [round(p, 2) for p, _ in events] == [0.5, 1.0]

    def test_zero_length_segments_do_not_keyerror(self):
        segments = [
            {"start": 1.0, "end": 1.0, "text": "", "speaker": "Speaker 1"},
            {"start": 2.0, "end": 2.0, "text": "", "speaker": "Speaker 2"},
        ]
        results = embeddings_from_wav(
            self._wav(3.0),
            segments,
            embed_utterance=lambda chunk: _unit(2, 0),
            min_seconds=1.0,
            max_segments_per_speaker=20,
        )
        assert results == []

    def test_short_only_segments_are_skipped(self):
        segments = [
            {"start": 0.0, "end": 0.2, "text": "hi", "speaker": "S1"},
            {"start": 0.2, "end": 0.4, "text": "yo", "speaker": "S1"},
        ]
        results = embeddings_from_wav(
            self._wav(1.0),
            segments,
            embed_utterance=lambda chunk: _unit(2, 0),
            min_seconds=1.0,
            max_segments_per_speaker=20,
        )
        assert results == []

    def test_mixed_valid_and_invalid_labels_preserve_valid(self):
        segments = [
            {"start": 0.0, "end": 0.0, "text": "", "speaker": "BadZero"},
            {"start": 1.0, "end": 1.2, "text": "short", "speaker": "BadShort"},
            {"start": 2.0, "end": 4.0, "text": "ok", "speaker": "Good"},
        ]
        results = embeddings_from_wav(
            self._wav(5.0),
            segments,
            embed_utterance=lambda chunk: _unit(3, 0),
            min_seconds=1.0,
            max_segments_per_speaker=20,
        )
        assert [r.local_label for r in results] == ["Good"]
        assert results[0].source_seconds == pytest.approx(2.0)


def test_label_embedding_is_frozen():
    embedding = LabelEmbedding(local_label="S", embedding=[1.0], source_seconds=1.0)
    with pytest.raises(Exception):
        embedding.local_label = "T"  # type: ignore[misc]


class TestDependencyDetection:
    """The availability check must catch broken imports, not just find_spec."""

    def test_missing_package_message_names_install_command(self, monkeypatch):
        def fail(name):
            if name == "resemblyzer":
                raise ImportError("No module named 'resemblyzer'")
            return importlib.import_module(name)

        monkeypatch.setattr(sp.importlib, "import_module", fail)
        message = encoder_import_error()
        assert message is not None
        assert "pip install resemblyzer" in message

    def test_broken_dependency_message_names_the_fix(self, monkeypatch):
        def fail(name):
            if name == "resemblyzer":
                raise ImportError("No module named 'pkg_resources'")
            return importlib.import_module(name)

        monkeypatch.setattr(sp.importlib, "import_module", fail)
        message = encoder_import_error()
        assert message is not None
        assert "pkg_resources" in message
        assert "setuptools<81" in message

    def test_broken_non_dependency_error_is_reported(self, monkeypatch):
        def fail(name):
            if name == "resemblyzer":
                raise RuntimeError("cuda exploded")
            return importlib.import_module(name)

        monkeypatch.setattr(sp.importlib, "import_module", fail)
        message = encoder_import_error()
        assert message is not None
        assert "cuda exploded" in message

    def test_successful_import_reports_no_problem(self, monkeypatch):
        monkeypatch.setattr(
            sp.importlib,
            "import_module",
            lambda name: importlib.import_module(name),
        )
        # In the test environment resemblyzer may be absent; both outcomes
        # are valid, the contract is only that no exception escapes.
        result = encoder_import_error()
        assert result is None or "resemblyzer" in result

    def test_is_encoder_available_follows_import_error(self, monkeypatch):
        monkeypatch.setattr(sp, "encoder_import_error", lambda: None)
        assert sp.is_encoder_available() is True
        monkeypatch.setattr(sp, "encoder_import_error", lambda: "broken")
        assert sp.is_encoder_available() is False

    def test_encoder_adapter_raises_actionable_error(self, monkeypatch):
        monkeypatch.setattr(
            sp, "encoder_import_error", lambda: "fix me: setuptools<81"
        )
        with pytest.raises(SpeakerProfilingError, match="setuptools<81"):
            sp.get_encoder()


class TestMatchingDecision:
    def test_threshold_boundaries(self):
        decide = sp.SpeakerProfileService._decision
        assert decide(0.69, None, 1, 0.70, 0.05) == "below_threshold"
        assert decide(0.70, None, 1, 0.70, 0.05) == "matched"
        assert decide(0.71, None, 1, 0.70, 0.05) == "matched"

    def test_ambiguity_margin(self):
        decide = sp.SpeakerProfileService._decision
        # difference 0.04 < 0.05 => ambiguous
        assert decide(0.74, 0.70, 2, 0.70, 0.05) == "ambiguous"
        # difference exactly 0.05 => not ambiguous
        assert decide(0.75, 0.70, 2, 0.70, 0.05) == "matched"
        # top-two gap within margin is ambiguous even if second is below threshold
        assert decide(0.70, 0.69, 2, 0.70, 0.05) == "ambiguous"

    def test_no_reference(self):
        decide = sp.SpeakerProfileService._decision
        assert decide(0.95, None, 0, 0.70, 0.05) == "no_reference"

    def test_decision_detail_includes_reason_and_gap(self):
        decision, reason = sp.SpeakerProfileService._decision_detail(0.74, 0.70, 2, 0.70, 0.05)
        assert decision == "ambiguous"
        assert "ambiguity margin" in reason.lower()
        assert "0.0400" in reason

    def test_regression_absent_profile_must_not_match_when_ambiguous(self):
        # Absent person's profile is top score, but another profile is within margin.
        decision, reason = sp.SpeakerProfileService._decision_detail(0.73, 0.69, 2, 0.70, 0.05)
        assert decision == "ambiguous"
        assert "Unknown" in reason or "unknown" in reason.lower()

    def test_regression_absent_profile_must_not_match_when_below_threshold(self):
        decision, reason = sp.SpeakerProfileService._decision_detail(0.69, 0.10, 2, 0.70, 0.05)
        assert decision == "below_threshold"
        assert "below threshold" in reason.lower()
