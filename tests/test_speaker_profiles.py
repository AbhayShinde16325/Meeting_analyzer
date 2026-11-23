"""Unit tests for voice-profile matching and embedding aggregation."""

from __future__ import annotations

import numpy as np
import pytest

from services.speaker_profiles import (
    LabelEmbedding,
    SpeakerProfilingError,
    cosine_similarity,
    embeddings_from_wav,
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


def test_label_embedding_is_frozen():
    embedding = LabelEmbedding(local_label="S", embedding=[1.0], source_seconds=1.0)
    with pytest.raises(Exception):
        embedding.local_label = "T"  # type: ignore[misc]
