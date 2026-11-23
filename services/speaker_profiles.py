"""Voice speaker profiles: embeddings, matching, and user-reviewed identity.

Design rules:

* Voice similarity **never** assigns an identity.  Automatic matching only
  creates ``suggested`` assignments (or manual candidates below the suggest
  threshold); a profile name is displayed in a transcript only after a user
  confirmed the assignment.
* Profile centroids grow exclusively from embeddings attached by confirmed
  assignments, so corrections immediately improve future matching.
* The encoder (Resemblyzer) is a lazy, optional dependency: without the
  package the rest of the application works exactly as before.

Embeddings are small fixed-length float vectors, stored as JSONB.  Meeting
audio itself always stays in Large Objects (see ``db/models.py``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from config import Settings

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]


class SpeakerProfilingError(RuntimeError):
    """Raised when voice profiling cannot be completed."""


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity between two equal-length vectors (0.0 if degenerate)."""

    va = np.asarray(list(a), dtype=np.float64)
    vb = np.asarray(list(b), dtype=np.float64)
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if norm == 0.0:
        return 0.0
    return float(np.dot(va, vb) / norm)


def mean_embedding(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of equally sized vectors; raises on empty input."""

    if not vectors:
        raise SpeakerProfilingError("Cannot average an empty embedding set.")
    return np.mean(np.asarray(vectors, dtype=np.float64), axis=0).tolist()


@dataclass(frozen=True)
class LabelEmbedding:
    """Embedding for one meeting-local diarization label."""

    local_label: str
    embedding: list[float]
    source_seconds: float


@dataclass(frozen=True)
class MatchCandidate:
    """One profile candidate for a label, best first."""

    profile_id: str
    profile_name: str
    confidence: float


class VoiceEncoderAdapter:
    """Thin wrapper around the Resemblyzer GE2E encoder.

    Kept behind an interface so tests (and future encoders) can substitute a
    fake without touching the database or matching logic.
    """

    provider_name = "resemblyzer"
    model_name = "ge2e-256"

    def __init__(self) -> None:
        try:
            from resemblyzer import VoiceEncoder, preprocess_wav
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise SpeakerProfilingError(
                "Voice profiling requires the optional 'resemblyzer' package. "
                "Install it with: pip install resemblyzer"
            ) from exc
        self._preprocess_wav = preprocess_wav
        self._encoder = VoiceEncoder(verbose=False)

    def compute_label_embeddings(
        self,
        audio_path: Path,
        segments: list[dict[str, Any]],
        *,
        min_seconds: float,
        max_segments_per_speaker: int,
        progress_cb: ProgressCallback | None = None,
    ) -> list[LabelEmbedding]:
        """Embed each diarized label from its transcript segments."""

        wav = self._preprocess_wav(str(audio_path))
        return embeddings_from_wav(
            wav,
            segments,
            embed_utterance=lambda chunk: self._encoder.embed_utterance(
                chunk
            ).tolist(),
            min_seconds=min_seconds,
            max_segments_per_speaker=max_segments_per_speaker,
            progress_cb=progress_cb,
        )


def is_encoder_available() -> bool:
    """Whether the optional resemblyzer package is importable."""

    import importlib.util

    return importlib.util.find_spec("resemblyzer") is not None


def get_encoder() -> VoiceEncoderAdapter:
    """Build the default encoder (raises SpeakerProfilingError if missing)."""

    return VoiceEncoderAdapter()


def embeddings_from_wav(
    wav: np.ndarray,
    segments: list[dict[str, Any]],
    *,
    embed_utterance: Callable[[np.ndarray], list[float]],
    min_seconds: float,
    max_segments_per_speaker: int,
    progress_cb: ProgressCallback | None = None,
) -> list[LabelEmbedding]:
    """Group transcript segments by speaker label and embed each group.

    ``embed_utterance`` maps one 16 kHz float slice to an embedding vector,
    keeping this function testable without the real encoder.  Labels with
    less than ``min_seconds`` of speech are skipped (too little speech for a
    stable voice print).
    """

    sample_rate = 16_000
    if len(wav) == 0:
        raise SpeakerProfilingError("The audio track is empty.")

    slices: dict[str, list[np.ndarray]] = {}
    totals: dict[str, float] = {}
    for segment in segments:
        label = segment.get("speaker") if isinstance(segment, dict) else None
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()
        bucket = slices.setdefault(label, [])
        if len(bucket) >= max_segments_per_speaker:
            continue
        try:
            start = max(0.0, float(segment.get("start", 0.0) or 0.0))
            end = float(segment.get("end", start) or start)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        chunk = wav[int(start * sample_rate) : int(end * sample_rate)]
        if len(chunk) < int(0.5 * sample_rate):
            continue
        bucket.append(chunk)
        totals[label] = totals.get(label, 0.0) + (end - start)

    results: list[LabelEmbedding] = []
    labels = sorted(slices)
    for index, label in enumerate(labels):
        if totals[label] < min_seconds:
            continue
        vectors = [embed_utterance(chunk) for chunk in slices[label]]
        results.append(
            LabelEmbedding(
                local_label=label,
                embedding=mean_embedding(vectors),
                source_seconds=round(totals[label], 3),
            )
        )
        if progress_cb is not None:
            progress_cb(
                (index + 1) / len(labels),
                f"Computed voice print for {label}...",
            )
    return results


class SpeakerProfileService:
    """Database-facing voice profiling: compute, match, review."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._encoder: VoiceEncoderAdapter | None = None

    def set_encoder(self, encoder: VoiceEncoderAdapter) -> None:
        """Inject an encoder (tests inject a fake; production builds lazily)."""

        self._encoder = encoder

    def _get_encoder(self) -> VoiceEncoderAdapter:
        if self._encoder is None:
            self._encoder = get_encoder()
        return self._encoder

    @property
    def enabled(self) -> bool:
        return bool(self._settings.voice_profiling_enabled)

    # -- compute + match (pipeline) -----------------------------------------

    def compute_and_store(
        self,
        *,
        meeting_id: str,
        transcript_id: str,
        audio_path: Path,
        segments: list[dict[str, Any]],
        progress_cb: ProgressCallback | None = None,
    ) -> list[LabelEmbedding]:
        """Compute per-label embeddings, store them, and record suggestions.

        Suggestions are written only where no user decision exists yet:
        confirmed/rejected assignments are never overwritten.  Returns the
        embeddings that were stored.
        """

        label_embeddings = self._get_encoder().compute_label_embeddings(
            audio_path,
            segments,
            min_seconds=self._settings.voice_min_segment_seconds,
            max_segments_per_speaker=self._settings.voice_max_segments_per_speaker,
            progress_cb=progress_cb,
        )
        if not label_embeddings:
            return []

        centroids = self._profile_centroids()
        with self._engine.begin() as conn:
            # Re-profiling the same transcript replaces its embeddings, but
            # user decisions in speaker_assignments always survive.
            conn.execute(
                text("DELETE FROM speaker_embeddings WHERE transcript_id = :t"),
                {"t": transcript_id},
            )
            for item in label_embeddings:
                conn.execute(
                    text(
                        "INSERT INTO speaker_embeddings (id, meeting_id, "
                        "transcript_id, local_label, embedding, source_seconds) "
                        "VALUES (gen_random_uuid(), :m, :t, :label, "
                        "CAST(:embedding AS jsonb), :seconds)"
                    ),
                    {
                        "m": meeting_id,
                        "t": transcript_id,
                        "label": item.local_label,
                        "embedding": _json(item.embedding),
                        "seconds": item.source_seconds,
                    },
                )
                candidates = self._match(
                    item.embedding, centroids, self._settings.voice_review_threshold
                )
                best = candidates[0] if candidates else None
                if best is None or best.confidence < self._settings.voice_match_threshold:
                    # No automatic suggestion; the candidate (if any) stays
                    # available for manual assignment in the review UI.
                    continue
                conn.execute(
                    text(
                        "INSERT INTO speaker_assignments (id, meeting_id, "
                        "transcript_id, local_label, profile_id, confidence, status) "
                        "VALUES (gen_random_uuid(), :m, :t, :label, :profile, "
                        ":confidence, 'suggested') "
                        "ON CONFLICT (transcript_id, local_label) DO UPDATE SET "
                        "profile_id = EXCLUDED.profile_id, "
                        "confidence = EXCLUDED.confidence, status = 'suggested', "
                        "updated_at = now() "
                        "WHERE speaker_assignments.status = 'suggested'"
                    ),
                    {
                        "m": meeting_id,
                        "t": transcript_id,
                        "label": item.local_label,
                        "profile": best.profile_id,
                        "confidence": round(best.confidence, 4),
                    },
                )
        return label_embeddings

    def _profile_centroids(self) -> dict[str, list[float]]:
        """Mean embedding per profile from user-confirmed embeddings only."""

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT profile_id, embedding FROM speaker_embeddings "
                    "WHERE profile_id IS NOT NULL"
                )
            ).all()
        grouped: dict[str, list[list[float]]] = {}
        for profile_id, embedding in rows:
            if profile_id is None or embedding is None:
                continue
            grouped.setdefault(str(profile_id), []).append(list(embedding))
        return {
            profile_id: mean_embedding(vectors)
            for profile_id, vectors in grouped.items()
        }

    def _match(
        self,
        embedding: list[float],
        centroids: dict[str, list[float]],
        min_confidence: float,
    ) -> list[MatchCandidate]:
        """Rank profiles by cosine similarity above ``min_confidence``."""

        if not centroids:
            return []
        names = self._profile_names(list(centroids))
        candidates: list[MatchCandidate] = []
        for profile_id, centroid in centroids.items():
            score = cosine_similarity(embedding, centroid)
            if score >= min_confidence:
                candidates.append(
                    MatchCandidate(
                        profile_id=profile_id,
                        profile_name=names.get(profile_id, ""),
                        confidence=score,
                    )
                )
        candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
        return candidates

    def _profile_names(self, profile_ids: list[str]) -> dict[str, str]:
        if not profile_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, display_name FROM speaker_profiles "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": profile_ids},
            ).all()
        return {str(row[0]): str(row[1]) for row in rows}

    # -- review (API) --------------------------------------------------------

    def list_meeting_speakers(self, meeting_id: str) -> list[dict[str, Any]]:
        """Every diarized label in the meeting's primary transcript.

        Includes the stored embedding state, the current assignment (if any)
        and manual candidate profiles above the review threshold.
        """

        with self._engine.begin() as conn:
            transcript = conn.execute(
                text(
                    "SELECT id, segments FROM transcripts WHERE meeting_id = :m "
                    "AND is_primary = true"
                ),
                {"m": meeting_id},
            ).mappings().one_or_none()
            if transcript is None:
                return []

            labels: dict[str, dict[str, Any]] = {}
            for segment in transcript["segments"] or []:
                label = segment.get("speaker") if isinstance(segment, dict) else None
                if not isinstance(label, str) or not label.strip():
                    continue
                label = label.strip()
                entry = labels.setdefault(label, {"segment_count": 0, "speech_seconds": 0.0})
                entry["segment_count"] += 1
                try:
                    start = float(segment.get("start", 0.0) or 0.0)
                    end = float(segment.get("end", start) or start)
                except (TypeError, ValueError):
                    continue
                if end > start:
                    entry["speech_seconds"] += end - start

            embeddings = {
                str(row["local_label"]): row
                for row in conn.execute(
                    text(
                        "SELECT id, local_label, embedding, source_seconds, profile_id "
                        "FROM speaker_embeddings WHERE transcript_id = :t"
                    ),
                    {"t": transcript["id"]},
                ).mappings()
            }
            assignments = {
                str(row["local_label"]): row
                for row in conn.execute(
                    text(
                        "SELECT id, local_label, profile_id, confidence, status "
                        "FROM speaker_assignments WHERE transcript_id = :t"
                    ),
                    {"t": transcript["id"]},
                ).mappings()
            }

        profiles = self.list_profiles()
        profile_names = {str(p["id"]): str(p["display_name"]) for p in profiles}
        centroids = self._profile_centroids()

        speakers: list[dict[str, Any]] = []
        for label in sorted(labels):
            stats = labels[label]
            embedding = embeddings.get(label)
            assignment = assignments.get(label)
            embedding_vector = (
                list(embedding["embedding"]) if embedding is not None else None
            )
            candidates: list[dict[str, Any]] = []
            if embedding_vector is not None:
                for candidate in self._match(
                    embedding_vector, centroids, self._settings.voice_review_threshold
                ):
                    candidates.append(
                        {
                            "profile_id": candidate.profile_id,
                            "display_name": candidate.profile_name,
                            "confidence": round(candidate.confidence, 4),
                        }
                    )
            profile_id = (
                str(assignment["profile_id"])
                if assignment is not None and assignment["profile_id"] is not None
                else None
            )
            speakers.append(
                {
                    "local_label": label,
                    "segment_count": stats["segment_count"],
                    "speech_seconds": round(stats["speech_seconds"], 2),
                    "has_embedding": embedding_vector is not None,
                    "embedded_profile_id": (
                        str(embedding["profile_id"])
                        if embedding is not None and embedding["profile_id"] is not None
                        else None
                    ),
                    "profile_id": profile_id,
                    "profile_name": (
                        profile_names.get(profile_id, "") if profile_id else None
                    ),
                    "confidence": (
                        round(float(assignment["confidence"]), 4)
                        if assignment is not None and assignment["confidence"] is not None
                        else None
                    ),
                    "status": (
                        str(assignment["status"])
                        if assignment is not None
                        else None
                    ),
                    "candidates": candidates,
                }
            )
        return speakers

    def assign_speaker(
        self,
        *,
        meeting_id: str,
        local_label: str,
        profile_id: str | None,
        new_profile_name: str | None,
        status: str,
    ) -> dict[str, Any]:
        """Create or update a user decision for one meeting-local label.

        ``status`` is ``confirmed`` (attach to a profile, existing or newly
        created), ``rejected`` (this label is nobody we track), or
        ``suggested`` (undo back to the model's suggestion).  Confirming
        attaches the meeting's embedding to the profile so future matching
        learns from the correction.
        """

        if status not in ("suggested", "confirmed", "rejected"):
            raise SpeakerProfilingError(f"Invalid assignment status {status!r}.")
        if status == "confirmed" and not profile_id and not new_profile_name:
            raise SpeakerProfilingError(
                "Confirming a speaker requires a profile or a new profile name."
            )

        # Keep any model confidence already recorded on the assignment; the
        # user's explicit decision supersedes it either way.
        confidence: float | None = None

        with self._engine.begin() as conn:
            transcript = conn.execute(
                text(
                    "SELECT id FROM transcripts WHERE meeting_id = :m "
                    "AND is_primary = true"
                ),
                {"m": meeting_id},
            ).mappings().one_or_none()
            if transcript is None:
                raise SpeakerProfilingError(
                    "This meeting has no primary transcript yet."
                )
            transcript_id = str(transcript["id"])

            if status == "confirmed":
                if new_profile_name:
                    profile_id = self.create_profile(new_profile_name)["id"]
                assert profile_id is not None
                conn.execute(
                    text(
                        "UPDATE speaker_embeddings SET profile_id = :p "
                        "WHERE transcript_id = :t AND local_label = :label"
                    ),
                    {"p": profile_id, "t": transcript_id, "label": local_label},
                )

            row = conn.execute(
                text(
                    "INSERT INTO speaker_assignments (id, meeting_id, transcript_id, "
                    "local_label, profile_id, confidence, status) "
                    "VALUES (gen_random_uuid(), :m, :t, :label, :profile, "
                    "CAST(:confidence AS double precision), :status) "
                    "ON CONFLICT (transcript_id, local_label) DO UPDATE SET "
                    "profile_id = EXCLUDED.profile_id, status = EXCLUDED.status, "
                    "confidence = COALESCE("
                    "CAST(:confidence AS double precision), "
                    "speaker_assignments.confidence), "
                    "updated_at = now() "
                    "RETURNING id, profile_id, status, confidence"
                ),
                {
                    "m": meeting_id,
                    "t": transcript_id,
                    "label": local_label,
                    "profile": profile_id,
                    "confidence": confidence,
                    "status": status,
                },
            ).mappings().one()
            return dict(row)

    # -- profile management ---------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT p.id, p.display_name, p.created_at, "
                    "COUNT(e.id) AS embedding_count "
                    "FROM speaker_profiles p "
                    "LEFT JOIN speaker_embeddings e ON e.profile_id = p.id "
                    "GROUP BY p.id, p.display_name, p.created_at "
                    "ORDER BY p.display_name"
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    def create_profile(self, display_name: str) -> dict[str, Any]:
        name = (display_name or "").strip()
        if not name:
            raise SpeakerProfilingError("A profile name is required.")
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM speaker_profiles WHERE display_name = :n"),
                {"n": name},
            ).scalar_one_or_none()
            if existing is not None:
                raise SpeakerProfilingError(
                    f"A speaker profile named '{name}' already exists."
                )
            row = conn.execute(
                text(
                    "INSERT INTO speaker_profiles (id, display_name) "
                    "VALUES (gen_random_uuid(), :n) "
                    "RETURNING id, display_name, created_at"
                ),
                {"n": name},
            ).mappings().one()
        return dict(row)

    def rename_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        name = (display_name or "").strip()
        if not name:
            raise SpeakerProfilingError("A profile name is required.")
        with self._engine.begin() as conn:
            clash = conn.execute(
                text(
                    "SELECT id FROM speaker_profiles "
                    "WHERE display_name = :n AND id <> :p"
                ),
                {"n": name, "p": profile_id},
            ).scalar_one_or_none()
            if clash is not None:
                raise SpeakerProfilingError(
                    f"A speaker profile named '{name}' already exists."
                )
            row = conn.execute(
                text(
                    "UPDATE speaker_profiles SET display_name = :n, updated_at = now() "
                    "WHERE id = :p "
                    "RETURNING id, display_name, created_at, updated_at"
                ),
                {"n": name, "p": profile_id},
            ).mappings().one_or_none()
        if row is None:
            raise SpeakerProfilingError("Speaker profile not found.")
        return dict(row)

    def profile_embedding_count(self, profile_id: str) -> int:
        """How many confirmed embeddings back a profile (V1 review signal)."""

        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM speaker_embeddings "
                        "WHERE profile_id = :p"
                    ),
                    {"p": profile_id},
                ).scalar_one()
            )

    def delete_profile(self, profile_id: str) -> None:
        """Delete a profile; assignments and embeddings survive unlinked."""

        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM speaker_profiles WHERE id = :p"),
                {"p": profile_id},
            )
        if result.rowcount == 0:
            raise SpeakerProfilingError("Speaker profile not found.")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
