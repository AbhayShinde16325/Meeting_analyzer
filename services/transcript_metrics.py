"""Post-transcription metrics for real meetings.

Computed automatically after a transcription succeeds, from the stored
segments and the job's own timing.  Eight metrics, all measured:

1. ``audio_duration_s`` — the meeting's stored audio duration.
2. ``transcription_duration_s`` — wall-clock time of the transcription job.
3. ``rtf`` — transcription duration / audio duration (None when either is
   missing or the audio duration is zero).
4. ``num_speakers`` — distinct diarization labels in the segments.
5. ``labelled_speech_pct`` — share of transcribed speech duration carrying a
   speaker label.
6. ``unknown_speech_pct`` — share of transcribed speech duration without a
   speaker label.  The two percentages are derived from segment *durations*,
   not segment counts, and sum to 100 when any speech exists.
7. ``review_segments`` — segments needing a human look: missing speaker
   label or invalid timestamps (end before start).  No confidence is
   fabricated.
8. ``transcription_status`` / ``error`` / fallback info — from the job row,
   so failures and fallbacks are visible next to the numbers.
"""

from __future__ import annotations

from typing import Any

# Speaker labels produced by the pipeline normalization; a segment without
# one is treated as an unknown speaker.
_REVIEW_NO_SPEAKER = "no speaker label"
_REVIEW_BAD_TIMESTAMPS = "invalid timestamps"


def _duration_seconds(segment: dict[str, Any]) -> float:
    try:
        start = float(segment.get("start", 0.0) or 0.0)
        end = float(segment.get("end", start) or start)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, end - start)


def compute_transcript_metrics(
    *,
    segments: list[dict[str, Any]],
    audio_duration_s: float | None,
    transcription_duration_s: float | None,
    transcription_status: str,
    error: str | None,
    fallback_from: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    """The eight post-transcription metrics for one transcription run.

    Every value is measured from stored data; nothing is estimated.
    """

    labelled_seconds = 0.0
    unknown_seconds = 0.0
    review_reasons: list[str] = []
    speakers: set[str] = set()

    for segment in segments:
        if not isinstance(segment, dict):
            continue
        label = segment.get("speaker")
        duration = _duration_seconds(segment)
        if isinstance(label, str) and label.strip():
            speakers.add(label.strip())
            labelled_seconds += duration
        else:
            unknown_seconds += duration
            review_reasons.append(_REVIEW_NO_SPEAKER)

        try:
            start = float(segment.get("start", 0.0) or 0.0)
            end = float(segment.get("end", start) or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if end < start:
            review_reasons.append(_REVIEW_BAD_TIMESTAMPS)

    speech_seconds = labelled_seconds + unknown_seconds
    labelled_pct: float | None = None
    unknown_pct: float | None = None
    if speech_seconds > 0:
        labelled_pct = round(labelled_seconds / speech_seconds * 100, 1)
        unknown_pct = round(unknown_seconds / speech_seconds * 100, 1)

    rtf: float | None = None
    if (
        transcription_duration_s is not None
        and audio_duration_s is not None
        and audio_duration_s > 0
    ):
        rtf = round(transcription_duration_s / audio_duration_s, 3)

    return {
        "audio_duration_s": audio_duration_s,
        "transcription_duration_s": transcription_duration_s,
        "rtf": rtf,
        "num_speakers": len(speakers) if speakers else None,
        "labelled_speech_pct": labelled_pct,
        "unknown_speech_pct": unknown_pct,
        "review_segments": len(review_reasons),
        "transcription_status": transcription_status,
        "error": error,
        "fallback_from": fallback_from,
        "fallback_reason": fallback_reason,
    }
