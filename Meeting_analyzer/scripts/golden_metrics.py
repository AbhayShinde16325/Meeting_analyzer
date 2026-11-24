"""Re-export shim for the pure evaluation metrics.

The implementation moved to ``services/golden_metrics.py`` so the server can
reuse the same functions for post-transcription metrics.  Import from either
location; this shim keeps ``scripts/evaluate_golden.py`` and existing tests
working.
"""

from services.golden_metrics import (  # noqa: F401
    cer,
    composite_score,
    duplicate_segment_ratio,
    has_devanagari,
    key_term_recall,
    key_terms,
    levenshtein,
    normalize_text,
    out_of_order_count,
    script_matches_language,
    speaker_metrics,
    timestamp_metrics,
    wer,
)
