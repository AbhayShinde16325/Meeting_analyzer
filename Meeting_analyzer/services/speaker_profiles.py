"""Voice speaker profiles: embeddings, matching, and review.

References are explicitly uploaded audio samples stored in
``speaker_reference_embeddings``. Meeting-derived embeddings are diagnostics
only and never become reference data. Clear, unambiguous matches above the
configured threshold are automatically confirmed with ``origin='auto'``;
manual confirmed/rejected decisions are preserved across re-profiling.
"""

from __future__ import annotations

import importlib
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
    """Thin wrapper around the Resemblyzer GE2E encoder."""

    provider_name = "resemblyzer"
    model_name = "ge2e-256"

    def __init__(self) -> None:
        problem = encoder_import_error()
        if problem:
            raise SpeakerProfilingError(problem)
        from resemblyzer import VoiceEncoder, preprocess_wav

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

    def embed_reference_audio(self, audio_path: Path) -> tuple[list[float], float]:
        """Embed an explicit reference sample and return (embedding, seconds)."""

        wav = self._preprocess_wav(str(audio_path))
        sample_rate = 16_000
        seconds = len(wav) / sample_rate if len(wav) else 0.0
        return self._encoder.embed_utterance(wav).tolist(), seconds


def encoder_import_error() -> str | None:
    """Actionable message when the encoder cannot be imported, else None."""

    try:
        importlib.import_module("resemblyzer")
    except ImportError as exc:
        reason = str(exc)
        if "No module named 'resemblyzer'" in reason or reason.endswith(
            "resemblyzer"
        ):
            return (
                "Voice profiling is unavailable: the optional 'resemblyzer' "
                "package is not installed. Install it with: "
                "pip install resemblyzer"
            )
        if "pkg_resources" in reason:
            return (
                "Voice profiling is unavailable: the 'resemblyzer' package "
                "is installed but cannot import 'pkg_resources'. Fix with: "
                '.venv\\Scripts\\python.exe -m pip install "setuptools<81"'
            )
        return (
            "Voice profiling is unavailable: the 'resemblyzer' package is "
            f"installed but cannot be imported ({reason}). Reinstall with: "
            "pip install --force-reinstall resemblyzer"
        )
    except Exception as exc:  # broken optional dependency of resemblyzer
        return (
            "Voice profiling is unavailable: importing 'resemblyzer' failed "
            f"({type(exc).__name__}: {exc})."
        )
    return None


def is_encoder_available() -> bool:
    """Whether the voice encoder can actually be imported right now."""

    return encoder_import_error() is None


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

    Labels with no valid >=0.5s chunks, or with less than ``min_seconds`` of
    total valid speech, are skipped. This prevents KeyError/unstable embeddings
    from zero-length or short-only diarized labels.
    """

    sample_rate = 16_000
    min_chunk_samples = int(0.5 * sample_rate)
    if len(wav) == 0:
        raise SpeakerProfilingError("The audio track is empty.")

    slices: dict[str, list[np.ndarray]] = {}
    totals: dict[str, float] = {}
    for segment in segments:
        label = segment.get("speaker") if isinstance(segment, dict) else None
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()
        try:
            start = max(0.0, float(segment.get("start", 0.0) or 0.0))
            end = float(segment.get("end", start) or start)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        start_i = int(start * sample_rate)
        end_i = min(int(end * sample_rate), len(wav))
        if end_i <= start_i:
            continue
        chunk = wav[start_i:end_i]
        if len(chunk) < min_chunk_samples:
            continue
        bucket = slices.setdefault(label, [])
        if len(bucket) >= max_segments_per_speaker:
            continue
        bucket.append(chunk)
        totals[label] = totals.get(label, 0.0) + (end - start)

    results: list[LabelEmbedding] = []
    labels = sorted(slices)
    for index, label in enumerate(labels):
        bucket = slices.get(label, [])
        if not bucket:
            continue
        total = float(totals.get(label, 0.0))
        if total < min_seconds:
            continue
        vectors = [embed_utterance(chunk) for chunk in bucket]
        if not vectors:
            continue
        results.append(
            LabelEmbedding(
                local_label=label,
                embedding=mean_embedding(vectors),
                source_seconds=round(total, 3),
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

    # -- reference enrollment ------------------------------------------------

    def enroll_profile_audio(
        self,
        *,
        profile_id: str,
        audio_path: Path,
        original_filename: str | None = None,
    ) -> dict[str, Any]:
        """Enroll an explicit reference sample into ``speaker_reference_embeddings``.

        Never creates synthetic meetings, transcripts, or meeting embeddings.
        """

        if not self.enabled:
            raise SpeakerProfilingError(
                "Voice profiling is disabled. Set TEAMMEETINGAI_VOICE_PROFILING=1 and install resemblyzer."
            )
        with self._engine.begin() as conn:
            profile = (
                conn.execute(
                    text(
                        "SELECT id, display_name FROM meeting_analyzer.speaker_profiles WHERE id=:p"
                    ),
                    {"p": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if profile is None:
                raise SpeakerProfilingError("Speaker profile not found.")
        encoder = self._get_encoder()
        embedding, seconds = encoder.embed_reference_audio(audio_path)
        if seconds < self._settings.voice_min_segment_seconds:
            raise SpeakerProfilingError(
                f"Enrollment sample too short ({seconds:.1f}s). Provide at least "
                f"{self._settings.voice_min_segment_seconds:.0f}s of clean speech."
            )
        if float(np.linalg.norm(np.asarray(embedding, dtype=np.float64))) <= 1e-8:
            raise SpeakerProfilingError(
                "Enrollment sample appears too quiet/noisy. Please record a clearer sample."
            )
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "INSERT INTO meeting_analyzer.speaker_reference_embeddings "
                        "(id, profile_id, embedding, source_seconds, original_filename, provider, model) "
                        "VALUES (gen_random_uuid(), :p, CAST(:e AS jsonb), :s, :fn, :provider, :model) "
                        "RETURNING id, profile_id, source_seconds, original_filename, provider, model, created_at"
                    ),
                    {
                        "p": profile_id,
                        "e": _json(embedding),
                        "s": float(seconds),
                        "fn": original_filename,
                        "provider": encoder.provider_name,
                        "model": encoder.model_name,
                    },
                )
                .mappings()
                .one()
            )
        return {
            "profile_id": str(profile["id"]),
            "display_name": str(profile["display_name"]),
            "seconds": round(float(seconds), 3),
            "embedding_count": self.profile_embedding_count(profile_id),
            "reference": dict(row),
        }

    def list_profile_references(self, profile_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM meeting_analyzer.speaker_profiles WHERE id=:p"),
                {"p": profile_id},
            ).scalar_one_or_none()
            if exists is None:
                raise SpeakerProfilingError("Speaker profile not found.")
            rows = (
                conn.execute(
                    text(
                        "SELECT id, profile_id, source_seconds, original_filename, provider, model, created_at "
                        "FROM meeting_analyzer.speaker_reference_embeddings WHERE profile_id=:p "
                        "ORDER BY created_at DESC, id DESC"
                    ),
                    {"p": profile_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete_profile_reference(self, profile_id: str, reference_id: str) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "DELETE FROM meeting_analyzer.speaker_reference_embeddings "
                    "WHERE id=:r AND profile_id=:p"
                ),
                {"r": reference_id, "p": profile_id},
            )
        if result.rowcount == 0:
            raise SpeakerProfilingError("Reference sample not found.")

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
        """Compute per-label embeddings, persist diagnostics, and apply matches.

        Confirmed/rejected manual assignments are never overwritten. Auto/suggested
        rows are recomputed; obsolete auto/suggested rows are removed.
        """

        label_embeddings = self._get_encoder().compute_label_embeddings(
            audio_path,
            segments,
            min_seconds=self._settings.voice_min_segment_seconds,
            max_segments_per_speaker=self._settings.voice_max_segments_per_speaker,
            progress_cb=progress_cb,
        )
        label_embeddings = [
            item
            for item in label_embeddings
            if item.source_seconds >= self._settings.voice_min_segment_seconds
            and float(np.linalg.norm(np.asarray(item.embedding, dtype=np.float64)))
            > 1e-8
        ]
        with self._engine.connect() as conn:
            diarized_labels = _diarized_labels(conn, transcript_id)

        if not label_embeddings and not diarized_labels:
            return []

        centroids = self._profile_centroids()
        profile_count = len(centroids)
        threshold = float(self._settings.voice_match_threshold)
        ambiguity_margin = float(self._settings.voice_match_ambiguity_margin)
        eval_time = _now_sql(self._engine)

        embedded_by_label = {item.local_label: item for item in label_embeddings}
        with self._engine.begin() as conn:
            for label in sorted(diarized_labels):
                item = embedded_by_label.get(label)
                assignment = (
                    conn.execute(
                        text(
                            "SELECT profile_id, status, origin FROM meeting_analyzer.speaker_assignments "
                            "WHERE transcript_id=:t AND local_label=:label"
                        ),
                        {"t": transcript_id, "label": label},
                    )
                    .mappings()
                    .one_or_none()
                )
                manual_locked = bool(
                    assignment
                    and assignment["status"] in ("confirmed", "rejected")
                    and assignment.get("origin", "manual") == "manual"
                )

                if item is None:
                    if not manual_locked:
                        conn.execute(
                            text(
                                "DELETE FROM meeting_analyzer.speaker_assignments "
                                "WHERE transcript_id=:t AND local_label=:label "
                                "AND (origin='auto' OR status='suggested') "
                                "AND NOT (origin='manual' AND status IN ('confirmed','rejected'))"
                            ),
                            {"t": transcript_id, "label": label},
                        )
                    continue

                ranked = self._rank_profiles(item.embedding, centroids)
                best = ranked[0] if ranked else None
                second = ranked[1] if len(ranked) > 1 else None
                best_score = float(best.confidence) if best else None
                best_profile_id = str(best.profile_id) if best else None
                decision = self._decision(best_score, second.confidence if second else None, profile_count, threshold, ambiguity_margin)
                match_profile_id = best_profile_id if decision == "matched" else None

                # Preserve profile link for manually locked confirmed assignments.
                profile_link = (
                    str(assignment["profile_id"])
                    if manual_locked
                    and assignment["status"] == "confirmed"
                    and assignment["profile_id"] is not None
                    else match_profile_id
                )
                conn.execute(
                    text(
                        "INSERT INTO meeting_analyzer.speaker_embeddings "
                        "(id, meeting_id, transcript_id, local_label, embedding, source_seconds, profile_id, "
                        " best_profile_id, best_score, decision, profiles_evaluated, match_threshold, evaluated_at) "
                        "VALUES (gen_random_uuid(), :m, :t, :label, CAST(:embedding AS jsonb), :seconds, :profile, "
                        " :best_profile, :best_score, :decision, :profiles_evaluated, :match_threshold, :evaluated_at) "
                        "ON CONFLICT (transcript_id, local_label) DO UPDATE SET "
                        "embedding=EXCLUDED.embedding, source_seconds=EXCLUDED.source_seconds, profile_id=EXCLUDED.profile_id, "
                        "best_profile_id=EXCLUDED.best_profile_id, best_score=EXCLUDED.best_score, decision=EXCLUDED.decision, "
                        "profiles_evaluated=EXCLUDED.profiles_evaluated, match_threshold=EXCLUDED.match_threshold, evaluated_at=EXCLUDED.evaluated_at"
                    ),
                    {
                        "m": meeting_id,
                        "t": transcript_id,
                        "label": label,
                        "embedding": _json(item.embedding),
                        "seconds": item.source_seconds,
                        "profile": profile_link,
                        "best_profile": best_profile_id,
                        "best_score": best_score,
                        "decision": decision,
                        "profiles_evaluated": profile_count,
                        "match_threshold": threshold,
                        "evaluated_at": eval_time,
                    },
                )

                if manual_locked:
                    continue

                if decision == "matched" and best is not None:
                    conn.execute(
                        text(
                            "INSERT INTO meeting_analyzer.speaker_assignments "
                            "(id, meeting_id, transcript_id, local_label, profile_id, confidence, status, origin) "
                            "VALUES (gen_random_uuid(), :m, :t, :label, :profile, :confidence, 'confirmed', 'auto') "
                            "ON CONFLICT (transcript_id, local_label) DO UPDATE SET "
                            "profile_id=EXCLUDED.profile_id, confidence=EXCLUDED.confidence, status='confirmed', origin='auto', updated_at=now() "
                            "WHERE NOT (speaker_assignments.origin='manual' AND speaker_assignments.status IN ('confirmed','rejected'))"
                        ),
                        {
                            "m": meeting_id,
                            "t": transcript_id,
                            "label": label,
                            "profile": str(best.profile_id),
                            "confidence": round(float(best.confidence), 4),
                        },
                    )
                else:
                    conn.execute(
                        text(
                            "DELETE FROM meeting_analyzer.speaker_assignments "
                            "WHERE transcript_id=:t AND local_label=:label "
                            "AND (origin='auto' OR status='suggested') "
                            "AND NOT (origin='manual' AND status IN ('confirmed','rejected'))"
                        ),
                        {"t": transcript_id, "label": label},
                    )
        return label_embeddings

    @staticmethod
    def _decision(
        best_score: float | None,
        second_score: float | None,
        profile_count: int,
        threshold: float,
        ambiguity_margin: float,
    ) -> str:
        if profile_count <= 0:
            return "no_reference"
        if best_score is None:
            return "below_threshold"
        if best_score < threshold:
            return "below_threshold"
        # best_score >= threshold
        if (
            second_score is not None
            and second_score >= threshold
            and round(best_score - second_score, 10) < ambiguity_margin
        ):
            return "ambiguous"
        return "matched"

    def _profile_centroids(self) -> dict[str, list[float]]:
        """Mean explicit reference embeddings per profile. References only."""

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT profile_id, embedding FROM meeting_analyzer.speaker_reference_embeddings"
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

    def _rank_profiles(
        self, embedding: list[float], centroids: dict[str, list[float]]
    ) -> list[MatchCandidate]:
        if not centroids:
            return []
        names = self._profile_names(list(centroids))
        candidates = [
            MatchCandidate(
                profile_id=pid,
                profile_name=names.get(pid, ""),
                confidence=cosine_similarity(embedding, centroid),
            )
            for pid, centroid in centroids.items()
        ]
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    def _match(
        self,
        embedding: list[float],
        centroids: dict[str, list[float]],
        min_confidence: float,
    ) -> list[MatchCandidate]:
        """Rank profiles by cosine similarity above ``min_confidence``."""

        return [c for c in self._rank_profiles(embedding, centroids) if c.confidence >= min_confidence]

    def _profile_names(self, profile_ids: list[str]) -> dict[str, str]:
        if not profile_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, display_name FROM meeting_analyzer.speaker_profiles "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": profile_ids},
            ).all()
        return {str(row[0]): str(row[1]) for row in rows}

    # -- review (API) --------------------------------------------------------

    def list_meeting_speakers(self, meeting_id: str) -> list[dict[str, Any]]:
        """Diarized labels with persisted diagnostics and review state."""

        with self._engine.begin() as conn:
            transcript = (
                conn.execute(
                    text(
                        "SELECT id, segments FROM meeting_analyzer.transcripts WHERE meeting_id = :m "
                        "AND is_primary = true"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            if transcript is None:
                return []

            labels: dict[str, dict[str, Any]] = {}
            for segment in transcript["segments"] or []:
                label = segment.get("speaker") if isinstance(segment, dict) else None
                if not isinstance(label, str) or not label.strip():
                    continue
                label = label.strip()
                entry = labels.setdefault(
                    label, {"segment_count": 0, "speech_seconds": 0.0}
                )
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
                        "SELECT id, local_label, embedding, source_seconds, profile_id, "
                        "best_profile_id, best_score, decision, profiles_evaluated, match_threshold, evaluated_at "
                        "FROM meeting_analyzer.speaker_embeddings WHERE transcript_id = :t"
                    ),
                    {"t": transcript["id"]},
                )
                .mappings()
            }
            assignments = {
                str(row["local_label"]): row
                for row in conn.execute(
                    text(
                        "SELECT id, local_label, profile_id, confidence, status, origin "
                        "FROM meeting_analyzer.speaker_assignments WHERE transcript_id = :t"
                    ),
                    {"t": transcript["id"]},
                )
                .mappings()
            }
            latest_job = (
                conn.execute(
                    text(
                        "SELECT status, error, started_at, finished_at "
                        "FROM meeting_analyzer.processing_jobs "
                        "WHERE meeting_id=:m AND job_type='profiling' "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )

        profiles = self.list_profiles()
        profile_names = {str(p["id"]): str(p["display_name"]) for p in profiles}
        profile_ref_counts = {str(p["id"]): int(p.get("embedding_count") or 0) for p in profiles}
        reference_available = sum(profile_ref_counts.values()) > 0
        centroids = self._profile_centroids()
        threshold = float(self._settings.voice_match_threshold)

        same_voice = self._same_voice_map(
            {
                label: list(row["embedding"])
                for label, row in embeddings.items()
                if row["embedding"] is not None
            }
        )

        job_status = str(latest_job["status"]) if latest_job else None
        job_error = str(latest_job["error"]) if latest_job and latest_job["error"] else None

        speakers: list[dict[str, Any]] = []
        for label in sorted(labels):
            stats = labels[label]
            emb = embeddings.get(label)
            assignment = assignments.get(label)
            embedding_vector = list(emb["embedding"]) if emb is not None else None
            candidates: list[dict[str, Any]] = []
            if embedding_vector is not None:
                for cand in self._match(
                    embedding_vector, centroids, self._settings.voice_review_threshold
                ):
                    candidates.append(
                        {
                            "profile_id": cand.profile_id,
                            "display_name": cand.profile_name,
                            "confidence": round(cand.confidence, 4),
                        }
                    )
            profile_id = (
                str(assignment["profile_id"])
                if assignment is not None and assignment["profile_id"] is not None
                else None
            )
            best_profile_id = (
                str(emb["best_profile_id"])
                if emb is not None and emb.get("best_profile_id") is not None
                else (candidates[0]["profile_id"] if candidates else None)
            )
            best_score = (
                float(emb["best_score"])
                if emb is not None and emb.get("best_score") is not None
                else (float(candidates[0]["confidence"]) if candidates else None)
            )
            decision = str(emb["decision"]) if emb is not None and emb.get("decision") else None
            if decision is None:
                if emb is None:
                    decision = "error" if job_status == "failed" else "no_embedding"
                elif not reference_available:
                    decision = "no_reference"

            speakers.append(
                {
                    "local_label": label,
                    "segment_count": stats["segment_count"],
                    "speech_seconds": round(stats["speech_seconds"], 2),
                    "has_embedding": embedding_vector is not None,
                    "embedded_profile_id": (
                        str(emb["profile_id"])
                        if emb is not None and emb["profile_id"] is not None
                        else None
                    ),
                    "profile_id": profile_id,
                    "profile_name": profile_names.get(profile_id) if profile_id else None,
                    "confidence": (
                        round(float(assignment["confidence"]), 4)
                        if assignment is not None and assignment["confidence"] is not None
                        else (round(best_score, 4) if best_score is not None else None)
                    ),
                    "status": str(assignment["status"]) if assignment is not None else None,
                    "origin": str(assignment["origin"]) if assignment is not None and assignment.get("origin") else None,
                    "candidates": candidates,
                    "same_voice_labels": same_voice.get(label, []),
                    "reference_available": reference_available,
                    "reference_count": sum(profile_ref_counts.values()),
                    "best_profile_id": best_profile_id,
                    "best_profile_name": profile_names.get(best_profile_id) if best_profile_id else None,
                    "best_score": round(best_score, 4) if best_score is not None else None,
                    "threshold": (
                        round(float(emb["match_threshold"]), 4)
                        if emb is not None and emb.get("match_threshold") is not None
                        else None
                    ),
                    "decision": decision,
                    "profiles_evaluated": (
                        int(emb["profiles_evaluated"])
                        if emb is not None and emb.get("profiles_evaluated") is not None
                        else (len(centroids) if emb is not None else None)
                    ),
                    "evaluated_at": emb.get("evaluated_at") if emb is not None else None,
                    "job_status": job_status,
                    "job_error": job_error,
                }
            )
        return speakers

    def _same_voice_map(
        self, label_embeddings: dict[str, list[float]]
    ) -> dict[str, list[str]]:
        threshold = self._settings.voice_merge_threshold
        labels = sorted(label_embeddings)
        same_voice: dict[str, list[tuple[float, str]]] = {}
        for index, label in enumerate(labels):
            for other in labels[index + 1 :]:
                score = cosine_similarity(
                    label_embeddings[label], label_embeddings[other]
                )
                if score >= threshold:
                    same_voice.setdefault(label, []).append((score, other))
                    same_voice.setdefault(other, []).append((score, label))
        return {
            label: [other for _, other in sorted(pairs, reverse=True)]
            for label, pairs in same_voice.items()
        }

    def assign_speaker(
        self,
        *,
        meeting_id: str,
        local_label: str,
        profile_id: str | None,
        new_profile_name: str | None,
        status: str,
    ) -> dict[str, Any]:
        """Create or update a manual decision for one meeting-local label."""

        if status not in ("suggested", "confirmed", "rejected"):
            raise SpeakerProfilingError(f"Invalid assignment status {status!r}.")
        if status == "confirmed" and not profile_id and not new_profile_name:
            raise SpeakerProfilingError(
                "Confirming a speaker requires a profile or a new profile name."
            )

        with self._engine.begin() as conn:
            transcript = (
                conn.execute(
                    text(
                        "SELECT id FROM meeting_analyzer.transcripts WHERE meeting_id = :m "
                        "AND is_primary = true"
                    ),
                    {"m": meeting_id},
                )
                .mappings()
                .one_or_none()
            )
            if transcript is None:
                raise SpeakerProfilingError(
                    "This meeting has no primary transcript yet."
                )
            transcript_id = str(transcript["id"])
            label_exists = conn.execute(
                text(
                    "SELECT 1 FROM meeting_analyzer.transcripts t "
                    "CROSS JOIN LATERAL jsonb_array_elements(t.segments) segment "
                    "WHERE t.id=:t AND segment->>'speaker'=:label LIMIT 1"
                ),
                {"t": transcript_id, "label": local_label},
            ).scalar_one_or_none()
            if label_exists is None:
                raise SpeakerProfilingError(
                    "Speaker label not found in the primary transcript."
                )

            if status == "confirmed":
                if new_profile_name:
                    profile_id = self.create_profile(new_profile_name)["id"]
                assert profile_id is not None
                exists = conn.execute(
                    text("SELECT 1 FROM meeting_analyzer.speaker_profiles WHERE id=:p"),
                    {"p": profile_id},
                ).scalar_one_or_none()
                if not exists:
                    raise SpeakerProfilingError("Speaker profile not found.")
                conn.execute(
                    text(
                        "UPDATE meeting_analyzer.speaker_embeddings SET profile_id = :p "
                        "WHERE transcript_id = :t AND local_label = :label"
                    ),
                    {"p": profile_id, "t": transcript_id, "label": local_label},
                )
            else:
                conn.execute(
                    text(
                        "UPDATE meeting_analyzer.speaker_embeddings SET profile_id = NULL "
                        "WHERE transcript_id = :t AND local_label = :label"
                    ),
                    {"t": transcript_id, "label": local_label},
                )

            row = (
                conn.execute(
                    text(
                        "INSERT INTO meeting_analyzer.speaker_assignments (id, meeting_id, transcript_id, "
                        "local_label, profile_id, confidence, status, origin) "
                        "VALUES (gen_random_uuid(), :m, :t, :label, :profile, NULL, :status, 'manual') "
                        "ON CONFLICT (transcript_id, local_label) DO UPDATE SET "
                        "profile_id = EXCLUDED.profile_id, status = EXCLUDED.status, origin = 'manual', "
                        "updated_at = now() "
                        "RETURNING id, profile_id, status, confidence, origin"
                    ),
                    {
                        "m": meeting_id,
                        "t": transcript_id,
                        "label": local_label,
                        "profile": profile_id,
                        "status": status,
                    },
                )
                .mappings()
                .one()
            )
            return dict(row)

    # -- profile management ---------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT p.id, p.display_name, p.created_at, p.updated_at, "
                        "COUNT(r.id) AS embedding_count "
                        "FROM meeting_analyzer.speaker_profiles p "
                        "LEFT JOIN meeting_analyzer.speaker_reference_embeddings r ON r.profile_id = p.id "
                        "GROUP BY p.id, p.display_name, p.created_at, p.updated_at "
                        "ORDER BY p.display_name"
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def create_profile(self, display_name: str) -> dict[str, Any]:
        name = (display_name or "").strip()
        if not name:
            raise SpeakerProfilingError("A profile name is required.")
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM meeting_analyzer.speaker_profiles WHERE display_name = :n"),
                {"n": name},
            ).scalar_one_or_none()
            if existing is not None:
                raise SpeakerProfilingError(
                    f"A speaker profile named '{name}' already exists."
                )
            row = (
                conn.execute(
                    text(
                        "INSERT INTO meeting_analyzer.speaker_profiles (id, display_name) "
                        "VALUES (gen_random_uuid(), :n) "
                        "RETURNING id, display_name, created_at, updated_at"
                    ),
                    {"n": name},
                )
                .mappings()
                .one()
            )
        return dict(row)

    def rename_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        name = (display_name or "").strip()
        if not name:
            raise SpeakerProfilingError("A profile name is required.")
        with self._engine.begin() as conn:
            clash = conn.execute(
                text(
                    "SELECT id FROM meeting_analyzer.speaker_profiles "
                    "WHERE display_name = :n AND id <> :p"
                ),
                {"n": name, "p": profile_id},
            ).scalar_one_or_none()
            if clash is not None:
                raise SpeakerProfilingError(
                    f"A speaker profile named '{name}' already exists."
                )
            row = (
                conn.execute(
                    text(
                        "UPDATE meeting_analyzer.speaker_profiles SET display_name = :n, updated_at = now() "
                        "WHERE id = :p "
                        "RETURNING id, display_name, created_at, updated_at"
                    ),
                    {"n": name, "p": profile_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SpeakerProfilingError("Speaker profile not found.")
        return dict(row)

    def profile_embedding_count(self, profile_id: str) -> int:
        """Number of explicit reference samples for a profile."""

        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM meeting_analyzer.speaker_reference_embeddings "
                        "WHERE profile_id = :p"
                    ),
                    {"p": profile_id},
                ).scalar_one()
            )

    def delete_profile(self, profile_id: str) -> None:
        """Delete a profile; assignments and meeting embeddings survive unlinked."""

        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM meeting_analyzer.speaker_profiles WHERE id = :p"),
                {"p": profile_id},
            )
        if result.rowcount == 0:
            raise SpeakerProfilingError("Speaker profile not found.")

    def mark_profiling_error(self, *, meeting_id: str, transcript_id: str, error: str) -> None:
        """Persist `error` decision for diarized labels when profiling fails."""

        eval_time = _now_sql(self._engine)
        threshold = float(self._settings.voice_match_threshold)
        with self._engine.begin() as conn:
            labels = _diarized_labels(conn, transcript_id)
            profiles_evaluated = conn.execute(
                text(
                    "SELECT COUNT(DISTINCT profile_id) FROM meeting_analyzer.speaker_reference_embeddings"
                )
            ).scalar_one()
            for label in labels:
                conn.execute(
                    text(
                        "UPDATE meeting_analyzer.speaker_embeddings SET "
                        "decision='error', profiles_evaluated=:pe, match_threshold=:mt, evaluated_at=:ea "
                        "WHERE transcript_id=:t AND local_label=:label"
                    ),
                    {
                        "t": transcript_id,
                        "label": label,
                        "pe": int(profiles_evaluated),
                        "mt": threshold,
                        "ea": eval_time,
                    },
                )
        logger.warning("Profiling marked error for meeting %s: %s", meeting_id, error)


def _diarized_labels(conn, transcript_id: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT segment->>'speaker' AS label "
            "FROM meeting_analyzer.transcripts t "
            "CROSS JOIN LATERAL jsonb_array_elements(COALESCE(t.segments, '[]'::jsonb)) segment "
            "WHERE t.id=:t AND segment->>'speaker' IS NOT NULL AND btrim(segment->>'speaker') <> ''"
        ),
        {"t": transcript_id},
    ).all()
    return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]


def _now_sql(engine: Engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT now()")).scalar_one()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
