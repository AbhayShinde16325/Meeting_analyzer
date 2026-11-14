"""Validation and normalisation of structured meeting analysis.

LLM output is untrusted.  It is parsed and validated here before any value is
shown or stored.  Missing owners, deadlines, and evidence are kept as
``None`` rather than guessed.
"""

from __future__ import annotations

import json
import re
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


class AnalysisValidationError(ValueError):
    """Raised when a model response does not match the expected schema."""


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
        action_items.append(
            {
                "task": task.strip(),
                "owner": _optional_text(raw_item.get("owner"), f"action item {index} owner"),
                "deadline": _optional_text(
                    raw_item.get("deadline"), f"action item {index} deadline"
                ),
                "evidence": _optional_text(
                    raw_item.get("evidence"), f"action item {index} evidence"
                ),
            }
        )
    normalised["action_items"] = action_items
    return normalised


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
