"""Groq cloud analysis provider.

This adapter implements the :class:`ai.summarization.AnalysisProvider` port
using the Groq cloud API.  Unlike the local providers, it sends the meeting
transcript (never the audio) to an external service, so it must only be used
when the user has explicitly opted in:

    TEAMMEETINGAI_ANALYSIS_BACKEND=groq
    TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1
    TEAMMEETINGAI_GROQ_MODEL=<model from the Groq console>
    TEAMMEETINGAI_GROQ_API_KEY=<secret, environment variable only>

The API key is read from the environment at construction time and is never
logged, stored in the database, or included in ``Settings``.  The prompts are
built and the response validated by :class:`ai.summarization.MeetingSummarizer`;
this adapter only performs the HTTP call.
"""

from __future__ import annotations

import logging
import os

from ai.summarization import SummarizationError
from config import Settings

logger = logging.getLogger(__name__)

_API_KEY_ENV = "TEAMMEETINGAI_GROQ_API_KEY"


class GroqAnalysisProvider:
    """Analysis provider that calls the Groq chat-completions API."""

    is_cloud = True

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.settings = settings
        self._api_key = (
            api_key
            if api_key is not None
            else os.environ.get(_API_KEY_ENV, "")
        )
        self._model = model if model is not None else settings.groq_model
        self._timeout = timeout if timeout is not None else settings.groq_timeout

    @property
    def model_name(self) -> str:
        return self._model

    def _check_configuration(self) -> None:
        if not self.settings.allow_cloud_analysis:
            raise SummarizationError(
                "Cloud analysis is not enabled. Set "
                "TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1 to opt in, knowing that "
                "the meeting transcript will be sent to Groq."
            )
        if not self._api_key:
            raise SummarizationError(
                f"No Groq API key is configured. Set the {_API_KEY_ENV} "
                "environment variable."
            )
        if not self._model:
            raise SummarizationError(
                "No Groq model is configured. Set the "
                "TEAMMEETINGAI_GROQ_MODEL environment variable to a model "
                "available in your Groq account."
            )

    def analyze(self, *, system_prompt: str, user_prompt: str) -> str:
        self._check_configuration()

        try:
            import groq
        except ImportError as exc:
            raise SummarizationError(
                "The groq package is not installed. Install it with: "
                "pip install groq"
            ) from exc

        client = groq.Groq(
            api_key=self._api_key,
            timeout=self._timeout,
            max_retries=0,
        )
        logger.info(
            "Sending meeting transcript to Groq for analysis (model=%s).",
            self._model,
        )
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.settings.llm_temperature,
                response_format={"type": "json_object"},
            )
        except groq.AuthenticationError as exc:
            raise SummarizationError(
                "Groq rejected the API key. Check the value of the "
                f"{_API_KEY_ENV} environment variable."
            ) from exc
        except groq.RateLimitError as exc:
            raise SummarizationError(
                "The Groq rate limit was reached. Wait a moment and retry."
            ) from exc
        except groq.NotFoundError as exc:
            raise SummarizationError(
                f"The Groq model '{self._model}' is not available in your "
                "account. Check TEAMMEETINGAI_GROQ_MODEL."
            ) from exc
        except (groq.APITimeoutError, groq.APIConnectionError) as exc:
            raise SummarizationError(
                "Could not reach Groq. Check the network connection and try "
                "again."
            ) from exc
        except groq.APIStatusError as exc:
            status = getattr(exc, "status_code", None) or "unknown"
            raise SummarizationError(
                f"Groq returned an unexpected error (HTTP {status}). "
                "The transcript was not analysed."
            ) from exc

        content = ""
        choices = getattr(response, "choices", None) or []
        if choices:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", "") or ""
        if not content.strip():
            raise SummarizationError(
                "Groq returned an empty response. Please retry."
            )
        return content
