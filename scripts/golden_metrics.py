"""Pure evaluation metrics for the golden-audio pack.

No I/O and no third-party dependencies: every function here is importable
from the unit-test tier. ``scripts/evaluate_golden.py`` orchestrates the
providers and calls these functions to score each datapoint.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Date words that carry commitment semantics in the golden references.
_DATE_WORDS = {
    "friday", "thursday", "tomorrow", "today", "monday", "tuesday",
    "wednesday", "saturday", "sunday",
    "shukrawar", "guruwar", "kal", "aaj", "somwar", "mangalwar",
    "budhwar", "shanivar", "ravivar", "dopahar",
}


# -- text normalisation -----------------------------------------------------


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace.

    Unicode-aware: letters, combining marks (Devanagari matras) and digits
    survive, while the danda, commas, quotes and question marks are removed.
    Python's ``\\w`` does not match combining marks, so categories are used
    instead of a character class.
    """

    if not text:
        return ""
    cleaned = unicodedata.normalize("NFKC", text).casefold()
    cleaned = "".join(
        ch for ch in cleaned if not unicodedata.category(ch).startswith("P")
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _words(text: str) -> list[str]:
    return normalize_text(text).split()


def _chars(text: str) -> list[str]:
    return list(re.sub(r"\s", "", normalize_text(text)))


def levenshtein(a: list[str] | str, b: list[str] | str) -> int:
    """Levenshtein distance between two sequences (words or characters)."""

    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, start=1):
        current = [i]
        for j, item_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,        # deletion
                    current[j - 1] + 1,     # insertion
                    previous[j - 1] + (item_a != item_b),  # substitution
                )
            )
        previous = current
    return previous[-1]


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate; 0.0 for an empty reference."""

    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 0.0
    return min(1.0, levenshtein(ref, hyp) / len(ref))


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate (whitespace ignored); 0.0 for an empty reference."""

    ref, hyp = _chars(reference), _chars(hypothesis)
    if not ref:
        return 0.0
    return min(1.0, levenshtein(ref, hyp) / len(ref))


# -- scripts, language, key terms -------------------------------------------


def has_devanagari(text: str) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in text or "")


def script_matches_language(text: str, language: str) -> bool | None:
    """Whether the script of ``text`` is plausible for ``language``.

    Hindi: either script is *possible* (Sarvam translit returns Latin,
    Whisper returns Devanagari), so this reports which one was produced
    rather than pass/fail — callers decide what is expected.
    English: Devanagari is never plausible.
    """

    if not (text or "").strip():
        return None
    devanagari = has_devanagari(text)
    if language == "english":
        return not devanagari
    return True


def key_terms(reference: str) -> set[str]:
    """Terms whose presence matters: content words plus explicit date words.

    Content words are normalised words of length >= 5; date words are always
    kept regardless of length because a missed deadline is the costliest
    transcription error in a meeting.
    """

    words = set(_words(reference))
    content = {w for w in words if len(w) >= 5}
    dates = words & _DATE_WORDS
    return content | dates


def key_term_recall(reference: str, hypothesis: str) -> float:
    """Fraction of key terms from the reference found in the hypothesis."""

    terms = key_terms(reference)
    if not terms:
        return 1.0
    hyp_words = set(_words(hypothesis))
    found = sum(1 for term in terms if term in hyp_words)
    return found / len(terms)


# -- timestamps ---------------------------------------------------------------


def timestamp_metrics(
    segments: list[dict[str, object]], audio_duration: float | None
) -> dict[str, object]:
    """Monotonicity and coverage of provider segments."""

    starts: list[float] = []
    monotonic = True
    previous_end = 0.0
    for segment in segments:
        start = float(segment.get("start", 0.0) or 0.0)
        end = float(segment.get("end", start) or start)
        if end < start or start < previous_end - 0.001:
            monotonic = False
        previous_end = end
        starts.append(start)

    coverage = None
    if audio_duration and audio_duration > 0 and segments:
        covered = 0.0
        cursor: float | None = None
        span_end = 0.0
        for segment in segments:
            start = float(segment.get("start", 0.0) or 0.0)
            end = max(start, float(segment.get("end", start) or start))
            if cursor is None:
                cursor, span_end = start, end
                continue
            if start > span_end:
                covered += span_end - cursor
                cursor, span_end = start, end
            else:
                span_end = max(span_end, end)
        if cursor is not None:
            covered += span_end - cursor
        coverage = min(1.0, covered / audio_duration)

    return {
        "segment_count": len(segments),
        "timestamps_monotonic": monotonic,
        "duration_coverage": round(coverage, 3) if coverage is not None else None,
    }


# -- diarization --------------------------------------------------------------


def duplicate_segment_ratio(segments: list[dict[str, object]]) -> float:
    """Fraction of segments that near-duplicate the previous segment.

    Echo-heavy recordings make the provider transcribe the same utterance
    twice (sometimes under a second speaker label); this measures how much
    of the transcript is duplicated.  0.0 for fewer than two segments.
    """

    if len(segments) < 2:
        return 0.0
    duplicates = 0
    for previous, current in zip(segments, segments[1:]):
        previous_text = normalize_text(str(previous.get("text", "") or ""))
        current_text = normalize_text(str(current.get("text", "") or ""))
        if not previous_text or not current_text:
            continue
        if SequenceMatcher(None, previous_text, current_text).ratio() >= 0.85:
            duplicates += 1
    return round(duplicates / len(segments), 3)


def out_of_order_count(segments: list[dict[str, object]]) -> int:
    """Number of segments whose start precedes the previous segment's start."""

    violations = 0
    previous_start: float | None = None
    for segment in segments:
        start = float(segment.get("start", 0.0) or 0.0)
        if previous_start is not None and start < previous_start - 0.001:
            violations += 1
        previous_start = start
    return violations


# -- diarization (speaker labels) ---------------------------------------------


def speaker_metrics(
    segments: list[dict[str, object]],
    expected_utterances: list[str],
    expected_speaker_count: int | None = None,
) -> dict[str, object]:
    """Speaker-count error and attribution accuracy for diarized output.

    ``expected_utterances`` are the manifest's per-speaker utterance texts.
    ``expected_speaker_count`` overrides the count derived from the utterances
    (used by the real meeting, where utterances are unknown but the headcount
    is).  Segments without speaker labels (Whisper fallback) return ``None``
    accuracies — diarization is simply not available for that datapoint.
    """

    expected_count = (
        expected_speaker_count
        if expected_speaker_count is not None
        else len(expected_utterances)
    )
    labeled = [s for s in segments if s.get("speaker")]
    if not labeled:
        return {
            "expected_speakers": expected_count,
            "found_speakers": None,
            "speaker_count_error": None,
            "attribution_accuracy": None,
            "matched_utterances": 0,
        }

    unique = {str(s["speaker"]) for s in labeled}
    matcher = SequenceMatcher(autojunk=False)
    matched = 0
    # A provider label is only trustworthy when it never claims two different
    # expected utterance slots with high confidence.
    slot_for_label: dict[str, int] = {}
    ambiguous_labels: set[str] = set()
    for index, utterance in enumerate(expected_utterances):
        expected_words = normalize_text(utterance)
        best_ratio, best_speaker = 0.0, None
        for segment in labeled:
            matcher.set_seq1(expected_words)
            matcher.set_seq2(normalize_text(str(segment.get("text", ""))))
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best_ratio, best_speaker = ratio, segment.get("speaker")
        if best_ratio >= 0.4 and best_speaker is not None:
            matched += 1
            label = str(best_speaker)
            if label in slot_for_label and slot_for_label[label] != index:
                ambiguous_labels.add(label)
            slot_for_label.setdefault(label, index)

    attribution = None
    if matched and expected_utterances:
        attribution = round((matched - len(ambiguous_labels)) / len(expected_utterances), 3)

    return {
        "expected_speakers": expected_count,
        "found_speakers": len(unique),
        "speaker_count_error": abs(len(unique) - expected_count),
        "attribution_accuracy": attribution,
        "matched_utterances": matched,
    }


# -- composite score ----------------------------------------------------------


def composite_score(
    *,
    wer_value: float,
    key_term_recall_value: float,
    language_ok: bool | None,
    diarization_accuracy: float | None,
    structure_value: float,
) -> float:
    """Weighted 0-1 score per the golden-evaluation plan.

    Weights: accuracy 0.45, key terms 0.15, diarization 0.15, language 0.10,
    structure 0.15.  Without diarization its weight goes to accuracy.
    """

    if diarization_accuracy is None:
        weights = {
            "accuracy": 0.60,
            "key_terms": 0.15,
            "language": 0.10,
            "structure": 0.15,
        }
    else:
        weights = {
            "accuracy": 0.45,
            "key_terms": 0.15,
            "language": 0.10,
            "structure": 0.15,
        }

    accuracy_part = (1.0 - min(1.0, max(0.0, wer_value))) * weights["accuracy"]
    key_term_part = key_term_recall_value * weights["key_terms"]
    language_part = (
        (1.0 if language_ok else 0.0) * weights["language"]
        if language_ok is not None
        else 0.0
    )
    structure_part = min(1.0, max(0.0, structure_value)) * weights["structure"]

    total = accuracy_part + key_term_part + language_part + structure_part
    if diarization_accuracy is not None:
        total += min(1.0, max(0.0, diarization_accuracy)) * 0.15
    return round(min(1.0, total), 3)
