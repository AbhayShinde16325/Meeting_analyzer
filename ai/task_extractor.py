"""Validation and normalisation of structured meeting analysis.

LLM output is untrusted.  It is parsed and validated here before any value is
shown or stored.  Missing owners, deadlines, and evidence are kept as
``None`` rather than guessed.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any, Mapping

_LIST_FIELDS = (
    "key_points",
    "decisions",
    "open_questions",
    "risks_or_blockers",
    "follow_ups",
)
_UNKNOWN_VALUES = {
    "",
    "null",
    "none",
    "unknown",
    "not specified",
    "n/a",
    "na",
    "-",
}

# Tokens that make a deadline string date-like: weekdays (English, common
# Hindi transliterations, and Devanagari), months, relative terms, and
# dayparts.  ASR noise inside a transcript frequently invents deadlines
# (for example "2-2 March" from mis-heard speech), so anything without one
# of these tokens is treated as implausible and rejected.
_WEEKDAY_TOKENS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "somwar", "somvaar", "mangalwar", "budhwar", "guruwar", "guruvaar",
    "shukrawar", "shukravaar", "shaniwar", "shanivaar", "sanichar", "sanivar",
    "ravivar", "ravivaar",
    "रविवार", "सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार",
)
_MONTH_TOKENS = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
)
_RELATIVE_TOKENS = (
    "today", "tomorrow", "yesterday", "kal", "aaj", "parso",
    "week", "weeks", "weekend", "month", "months", "quarter", "sprint",
)
_DAYPART_TOKENS = (
    "morning", "afternoon", "evening", "tonight", "night", "noon",
    "subah", "dopahar", "shaam", "raat",
    "सुबह", "दोपहर", "शाम", "रात",
)
_DATE_TOKENS = frozenset(
    _WEEKDAY_TOKENS + _MONTH_TOKENS + _RELATIVE_TOKENS + _DAYPART_TOKENS
)
# Duplicated tokens ("2-2 March", or the word-duplication form "do-do
# March" produced when Hindi speech is mis-transcribed) are classic
# ASR-corruption artefacts.  The word pattern uses \w plus the Devanagari
# range because Python's \w does not match combining matras.
_DUPLICATED_NUMBER_RE = re.compile(r"\b(\d{1,2})\s*[-–/]\s*\1\b")
_DUPLICATED_WORD_RE = re.compile(r"\b(\w{1,6})\s*[-–]\s*\1\b")
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,4}\s*[-/.]\s*\d{1,2}(?:\s*[-/.]\s*\d{1,4})?\b")
_NUMBER_RE = re.compile(r"\d{1,4}")
# Devanagari combining marks (matras) are category Mn and are excluded from
# \w by the re module, so the range is added explicitly.
_WORD_RE = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


class AnalysisValidationError(ValueError):
    """Raised when a model response does not match the expected schema."""


def deadline_is_plausible(deadline: str) -> bool:
    """Whether a deadline string looks like an actual date expression.

    This is deliberately a sanity check, not a parser: it catches ASR- or
    model-corrupted values (duplicated or out-of-range numbers, phrases with
    no date meaning at all) while accepting every reasonable human form,
    including Hindi words in either script.
    """

    text = unicodedata.normalize("NFKC", str(deadline)).casefold()
    if _DUPLICATED_NUMBER_RE.search(text) or _DUPLICATED_WORD_RE.search(text):
        return False
    for match in _NUMBER_RE.finditer(text):
        value = int(match.group())
        if value > 31 and not 1000 <= value <= 2999:
            return False
    if _NUMERIC_DATE_RE.search(text):
        return True
    words = set(_WORD_RE.findall(text))
    return bool(words & _DATE_TOKENS)


def parse_model_json(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract a JSON object from a model response.

    Small local models sometimes wrap JSON in Markdown fences or add a short
    sentence before it.  We accept those wrappers but still require a JSON
    object, never evaluating arbitrary text.
    """

    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise AnalysisValidationError("The model returned an empty response.")

    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise AnalysisValidationError(
                "The model response did not contain a JSON object."
            )
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AnalysisValidationError(
                "The model response was not valid JSON. Please retry."
            ) from exc

    if not isinstance(parsed, dict):
        raise AnalysisValidationError("The model response must be a JSON object.")
    return parsed


def validate_analysis(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise a meeting-analysis object.

    Returns a fresh dictionary containing exactly the fields the database and
    UI expect.  Raises :class:`AnalysisValidationError` for malformed output.
    """

    if not isinstance(data, Mapping):
        raise AnalysisValidationError("Meeting analysis must be a JSON object.")

    overview = data.get("overview")
    if not isinstance(overview, str):
        raise AnalysisValidationError("The 'overview' field must be text.")
    overview = overview.strip()
    if not overview:
        raise AnalysisValidationError("The model returned an empty overview.")

    normalised: dict[str, Any] = {"overview": overview}
    for field_name in _LIST_FIELDS:
        normalised[field_name] = _validate_string_list(data.get(field_name), field_name)

    action_items_raw = data.get("action_items", [])
    if action_items_raw is None:
        action_items_raw = []
    if not isinstance(action_items_raw, list):
        raise AnalysisValidationError("The 'action_items' field must be a list.")

    action_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(action_items_raw, start=1):
        if not isinstance(raw_item, Mapping):
            raise AnalysisValidationError(
                f"Action item {index} must be an object with a 'task' field."
            )
        task = raw_item.get("task")
        if not isinstance(task, str) or not task.strip():
            raise AnalysisValidationError(
                f"Action item {index} is missing a task description."
            )
        deadline = _optional_text(
            raw_item.get("deadline"), f"action item {index} deadline"
        )
        action_items.append(
            {
                "task": task.strip(),
                "owner": _optional_text(raw_item.get("owner"), f"action item {index} owner"),
                "deadline": deadline,
                "evidence": _optional_text(
                    raw_item.get("evidence"), f"action item {index} evidence"
                ),
            }
        )
    normalised["action_items"] = action_items
    return normalised


def find_grounding_issues(
    analysis: Mapping[str, Any], transcript_text: str
) -> list[str]:
    """Describe action items with invented or implausible details.

    The system prompt forbids invented owners and deadlines, but ASR-corrupted
    transcripts can still lead the model to hallucinate them (an owner name
    that occurs nowhere in the transcript, or a corrupted date such as
    "2-2 March").  Sanity issues are reported instead of raising so the
    caller can retry and finally downgrade the offending fields to null.
    """

    transcript = unicodedata.normalize("NFKC", transcript_text).casefold()
    issues: list[str] = []
    for index, item in enumerate(analysis.get("action_items", []), start=1):
        if not isinstance(item, Mapping):
            continue
        owner = item.get("owner")
        if owner and not _text_present(str(owner), transcript):
            issues.append(
                f"action item {index} owner '{owner}' does not appear in "
                "the transcript; use null if the owner is not stated"
            )
        deadline = item.get("deadline")
        if deadline and not deadline_is_plausible(str(deadline)):
            issues.append(
                f"action item {index} deadline '{deadline}' is not a "
                "plausible date; extract only dates explicitly agreed in "
                "the transcript; use null otherwise"
            )
    return issues


def downgrade_ungrounded(
    analysis: Mapping[str, Any], transcript_text: str
) -> dict[str, Any]:
    """Return a copy with ungrounded owners and implausible deadlines nulled.

    Used after retries are exhausted: keeping the action item without its
    invented owner or corrupted deadline is better than losing the analysis.
    """

    transcript = unicodedata.normalize("NFKC", transcript_text).casefold()
    downgraded = copy.deepcopy(dict(analysis))
    for item in downgraded.get("action_items", []):
        owner = item.get("owner")
        if owner and not _text_present(str(owner), transcript):
            item["owner"] = None
        deadline = item.get("deadline")
        if deadline is not None and not deadline_is_plausible(deadline):
            item["deadline"] = None
    return downgraded


def _text_present(text: str, transcript: str) -> bool:
    normalised = unicodedata.normalize("NFKC", text).casefold().strip()
    if normalised and normalised in transcript:
        return True
    return any(
        len(word) >= 3 and word in transcript
        for word in _WORD_RE.findall(normalised)
    )


def _validate_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AnalysisValidationError(f"The '{field_name}' field must be a list.")
    items: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, Mapping) and isinstance(entry.get("text"), str):
            text = entry["text"].strip()
        else:
            raise AnalysisValidationError(
                f"Entries in '{field_name}' must be text."
            )
        if text:
            items.append(text)
    return items


def _optional_text(value: Any, description: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        raise AnalysisValidationError(f"The {description} must be text or null.")
    text = str(value).strip()
    if text.lower() in _UNKNOWN_VALUES:
        return None
    return text
