"""Post-processing for transcription segments.

Providers occasionally return segments that are out of chronological order
(observed with Sarvam on overlapping-speech and echo-heavy recordings) or
duplicated (an echo reflection transcribed a second time, sometimes under a
second hallucinated speaker label).  The functions here are pure and
dependency-free so they can be unit-tested and reused by tooling.
"""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher

# Two consecutive segments whose normalised texts match at or above this
# ratio are considered an echo duplication of the same utterance.
DEFAULT_SIMILARITY_THRESHOLD = 0.85

_PUNCTUATION_CATEGORY_PREFIXES = ("P", "S")


def normalize_for_comparison(text: str) -> str:
    """Normalise text for similarity comparison.

    NFKC-normalises, casefolds, drops punctuation/symbol characters (keeping
    letters and digits from any script, including Devanagari), and collapses
    whitespace.
    """

    normalised = unicodedata.normalize("NFKC", text).casefold()
    kept: list[str] = []
    for char in normalised:
        if char.isspace():
            kept.append(" ")
            continue
        category = unicodedata.category(char)
        if category.startswith(_PUNCTUATION_CATEGORY_PREFIXES):
            continue
        kept.append(char)
    return " ".join("".join(kept).split())


def sort_segments(
    segments: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return segments in chronological order (stable on ties)."""

    return sorted(
        segments,
        key=lambda segment: (
            float(segment.get("start", 0.0) or 0.0),
            float(segment.get("end", 0.0) or 0.0),
        ),
    )


def dedupe_consecutive_segments(
    segments: list[dict[str, object]],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[list[dict[str, object]], int]:
    """Drop consecutive segments that repeat the same utterance.

    A segment is dropped when its normalised text is near-identical to the
    text of the previous *kept* segment, regardless of speaker labels (echo
    duplications often arrive with a hallucinated second speaker).  Returns
    the deduplicated list and the number of removed segments.
    """

    kept: list[dict[str, object]] = []
    removed = 0
    previous_text: str | None = None
    for segment in segments:
        text = normalize_for_comparison(str(segment.get("text", "") or ""))
        if previous_text is not None and text and SequenceMatcher(
            None, previous_text, text
        ).ratio() >= similarity_threshold:
            removed += 1
            continue
        kept.append(segment)
        previous_text = text
    return kept, removed


def postprocess_segments(
    segments: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    """Sort chronologically, then remove echo duplications.

    Returns the processed segments and the number of removed duplicates.
    """

    ordered = sort_segments(segments)
    return dedupe_consecutive_segments(ordered)
