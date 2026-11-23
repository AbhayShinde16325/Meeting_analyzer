"""Sarvam Saaras transcription provider (mandatory primary).

Uses the official ``sarvamai`` SDK against the batch speech-to-text API, which
supports files up to 2 hours, diarization and chunk-level timestamps:

    SarvamAI(api_subscription_key=...) -> client.speech_to_text_job.create_job(
        model="saaras:v4", mode="translit", with_diarization=True)
        -> job.upload_files([path]) -> job.start()
        -> job.get_status() polling -> job.download_outputs(dir)

The provider requests ``mode="translit"`` so the transcript is Roman-script
(Roman transliteration is the required primary transcript).

**Privacy note (disclosed in the UI consent label):** audio is uploaded to
Sarvam's cloud service for transcription.  The API key is read directly from
the environment (``TEAMMEETINGAI_SARVAM_API_KEY``) and never stored in
Settings, reprs, or logs.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ai.transcription import (
    CancelCallback,
    ProgressCallback,
    TranscriptionCancelled,
    TranscriptionError,
    TranscriptionResult,
)
from config import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "sarvam"
MODEL_NAME = "saaras:v4"

SARVAM_KEY_ENV = "TEAMMEETINGAI_SARVAM_API_KEY"


class SarvamConfigurationError(TranscriptionError):
    """Raised when the Sarvam provider is selected but not configured."""


class SarvamTranscriptionProvider:
    """Transcribe via the Sarvam batch API (2h files, diarization, timestamps)."""

    provider_name = PROVIDER_NAME
    model_name = MODEL_NAME
    # Roman-script transliteration is the required primary transcript mode.
    transcript_mode = "translit"
    is_cloud = True

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
        transcript_mode: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client  # injectable for tests
        self._poll_interval = poll_interval if poll_interval is not None else 5.0
        self._timeout = timeout if timeout is not None else 1800.0
        # Roman transliteration is the production default; callers (e.g. the
        # evaluation harness) may request the native script instead.
        if transcript_mode is not None:
            self.transcript_mode = transcript_mode

    # -- public API ---------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        progress_cb: ProgressCallback | None = None,
        cancel_cb: CancelCallback | None = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` via the Sarvam batch API.

        Language is auto-detected by Sarvam unless
        ``TEAMMEETINGAI_SARVAM_LANGUAGE_HINT`` is configured.  Raises
        :class:`TranscriptionCancelled` when ``cancel_cb`` returns True; the
        remote job is abandoned (Sarvam jobs expire on their own).
        """

        if not self._is_configured():
            raise SarvamConfigurationError(
                "Sarvam transcription is not configured. Set "
                f"{SARVAM_KEY_ENV} in the server environment."
            )
        client = self._client or self._build_client()
        job = self._create_job(client, cancel_cb)
        self._upload_and_start(job, audio_path, progress_cb, cancel_cb)
        payload = self._wait_for_completion(job, progress_cb, cancel_cb)
        return self._result_from_payload(payload)

    # -- internal helpers ---------------------------------------------------

    def _is_configured(self) -> bool:
        if self._client is not None:
            return True
        return bool(os.environ.get(SARVAM_KEY_ENV, "").strip())

    def _build_client(self) -> Any:
        try:
            from sarvamai import SarvamAI
        except ImportError as exc:  # pragma: no cover - dependency pin
            raise TranscriptionError(
                "The 'sarvamai' package is not installed on the server."
            ) from exc
        return SarvamAI(api_subscription_key=os.environ[SARVAM_KEY_ENV].strip())

    def _create_job(self, client: Any, cancel_cb: CancelCallback | None) -> Any:
        self._check_cancelled(cancel_cb)
        params: dict[str, Any] = {
            "model": MODEL_NAME,
            "mode": self.transcript_mode,
            "with_diarization": True,
        }
        # Optional language hint (BCP-47, e.g. "hi-IN") for audio that the
        # auto-detector reliably misdetects (Hinglish code-switching).
        hint = (self._settings.sarvam_language_hint or "").strip()
        if hint:
            params["language_code"] = hint
        try:
            return client.speech_to_text_job.create_job(**params)
        except TranscriptionCancelled:
            raise
        except Exception as exc:
            raise self._wrap("creating the Sarvam job", exc)

    def _upload_and_start(
        self,
        job: Any,
        audio_path: Path,
        progress_cb: ProgressCallback | None,
        cancel_cb: CancelCallback | None,
    ) -> None:
        self._check_cancelled(cancel_cb)
        try:
            if progress_cb is not None:
                progress_cb(0.05, "Uploading audio to Sarvam...")
            job.upload_files(file_paths=[str(audio_path)])
            self._check_cancelled(cancel_cb)
            job.start()
            if progress_cb is not None:
                progress_cb(0.10, "Sarvam is processing the audio...")
        except TranscriptionCancelled:
            raise
        except Exception as exc:
            raise self._wrap("uploading audio to Sarvam", exc)

    def _wait_for_completion(
        self,
        job: Any,
        progress_cb: ProgressCallback | None,
        cancel_cb: CancelCallback | None,
    ) -> dict[str, Any]:
        """Poll the job status until terminal state, then fetch the output.

        Polling is implemented here (instead of the SDK helper) so a
        cancellation request is honoured between polls.
        """

        deadline = time.monotonic() + self._timeout
        while True:
            self._check_cancelled(cancel_cb)
            status = self._get_status(job)
            state_raw = status.get("job_state", "")
            state = str(getattr(state_raw, "value", state_raw) or "").lower()
            if state == "completed":
                return self._download_result(job)
            if state == "failed":
                message = str(status.get("error_message") or "unknown error")
                raise TranscriptionError(f"Sarvam reported a failed job: {message}")
            if time.monotonic() >= deadline:
                raise TranscriptionError(
                    "Sarvam did not finish in time; the job was abandoned."
                )
            if progress_cb is not None:
                progress_cb(0.4, "Transcribing with Sarvam (cloud)...")
            time.sleep(self._poll_interval)

    def _get_status(self, job: Any) -> dict[str, Any]:
        try:
            status = job.get_status()
        except AttributeError:
            # SDK versions without get_status(): fall back to the blocking
            # helper (cancellation then applies after the wait returns).
            status = job.wait_until_complete(
                poll_interval=self._poll_interval, timeout=self._timeout
            )
        except Exception as exc:
            raise self._wrap("checking the Sarvam job status", exc)
        if isinstance(status, dict):
            return status
        return _normalise_payload(status)

    def _download_result(self, job: Any) -> dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="tma-sarvam-") as out_dir:
                job.download_outputs(output_dir=out_dir)
                files = sorted(Path(out_dir).glob("*.json"))
                if not files:
                    raise TranscriptionError(
                        "Sarvam completed but returned no transcript file."
                    )
                return json.loads(files[0].read_text(encoding="utf-8"))
        except TranscriptionError:
            raise
        except Exception as exc:
            raise self._wrap("downloading the Sarvam transcript", exc)

    def _result_from_payload(self, payload: dict[str, Any]) -> TranscriptionResult:
        text = str(payload.get("transcript") or "").strip()
        if not text:
            raise TranscriptionError("Sarvam returned an empty transcript.")
        segments: list[dict[str, Any]] = []
        diarized = payload.get("diarized_transcript") or {}
        entries = diarized.get("entries") if isinstance(diarized, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_text = str(entry.get("transcript") or "").strip()
                if not entry_text:
                    continue
                try:
                    start = float(entry.get("start_time_seconds") or 0.0)
                    end = float(entry.get("end_time_seconds") or start)
                except (TypeError, ValueError):
                    start, end = 0.0, 0.0
                speaker = entry.get("speaker_id")
                segments.append(
                    {
                        "start": max(0.0, start),
                        "end": max(0.0, end),
                        "text": entry_text,
                        "speaker": f"Speaker {speaker}" if speaker is not None else None,
                    }
                )
        if not segments:
            timestamps = payload.get("timestamps") or {}
            chunks = timestamps.get("chunks") or []
            starts = timestamps.get("start_time_seconds") or []
            ends = timestamps.get("end_time_seconds") or []
            for index, chunk_text in enumerate(chunks):
                clean = str(chunk_text or "").strip()
                if not clean:
                    continue
                try:
                    start = float(starts[index]) if index < len(starts) else 0.0
                    end = float(ends[index]) if index < len(ends) else start
                except (TypeError, ValueError):
                    start, end = 0.0, 0.0
                segments.append({"start": max(0.0, start), "end": max(0.0, end), "text": clean})
        language = payload.get("language_code")
        return TranscriptionResult(
            text=text,
            language=str(language) if language else None,
            segments=segments,
        )

    @staticmethod
    def _wrap(stage: str, exc: Exception) -> TranscriptionError:
        logger.warning("Sarvam %s failed: %s", stage, type(exc).__name__)
        return TranscriptionError(
            f"Sarvam transcription failed while {stage}. "
            f"The Whisper fallback can be attempted."
        )

    @staticmethod
    def _check_cancelled(cancel_cb: CancelCallback | None) -> None:
        if cancel_cb is not None and cancel_cb():
            raise TranscriptionCancelled("Transcription cancelled by the user.")


def _normalise_payload(status: Any) -> dict[str, Any]:
    """Convert SDK response objects into plain dicts."""

    if isinstance(status, dict):
        return status
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(status):
            return asdict(status)
    except Exception:  # pragma: no cover
        pass
    if hasattr(status, "model_dump"):
        try:
            return status.model_dump()
        except Exception:  # pragma: no cover
            pass
    if hasattr(status, "to_dict"):
        try:
            return status.to_dict()
        except Exception:  # pragma: no cover
            pass
    return {}
