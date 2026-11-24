"""HTTP client for the TeamMeetingAI server (FastAPI).

One instance is shared by the whole application.  All methods are synchronous
and safe to call from ``QThread`` workers (a fresh httpx client per call would
defeat connection pooling; httpx clients are thread-safe for sequential use
and the UI serialises its calls through worker threads).

Errors are normalised into :class:`ApiClientError` (server answered with an
error envelope) and :class:`ApiConnectionError` (server unreachable), so UI
code only handles two exception types.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, BinaryIO

import httpx

DEFAULT_SERVER_URL = "http://localhost:8000"
SERVER_URL_ENV = "TEAMMEETINGAI_SERVER_URL"
_TIMEOUT_SECONDS = 30.0
_UPLOAD_TIMEOUT_SECONDS = 3600.0  # multi-hour recordings
_DOWNLOAD_CHUNK = 1024 * 1024


class ApiClientError(RuntimeError):
    """The server answered with an error envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ApiConnectionError(RuntimeError):
    """The server could not be reached."""


def server_url_from_env() -> str:
    """Return the configured server URL, or the localhost default."""

    raw = os.environ.get(SERVER_URL_ENV, "").strip()
    return raw or DEFAULT_SERVER_URL


def _error_from_response(response: httpx.Response) -> ApiClientError:
    """Extract the ``{"error": {"code", "message"}}`` envelope."""

    try:
        payload = response.json()
        error = payload.get("error") or {}
        code = str(error.get("code") or "http_error")
        message = str(error.get("message") or response.text or "Request failed.")
    except (json.JSONDecodeError, ValueError):
        code, message = "http_error", f"Server error {response.status_code}."
    return ApiClientError(response.status_code, code, message)


class TeamMeetingAIClient:
    """Client for the hosted TeamMeetingAI server API."""

    def __init__(self, base_url: str | None = None, profile_id: str | None = None) -> None:
        self._base_url = (base_url or server_url_from_env()).rstrip("/")
        self._profile_id = profile_id
        # The host token (shared .env on the meeting host machine) proves this
        # client may record and manage meetings.  Never sent by remote clients.

    # -- configuration ---------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def profile_id(self) -> str | None:
        return self._profile_id

    def set_profile(self, profile_id: str | None) -> None:
        """Switch the acting profile (sent as ``X-Profile-Id``)."""

        self._profile_id = profile_id

    def _headers(self) -> dict[str, str]:
        """Headers for one request."""
        headers: dict[str, str] = {}
        if self._profile_id:
            headers["X-Profile-Id"] = self._profile_id
        return headers

    # -- request helpers ---------------------------------------------------

    def _client(self, timeout: float = _TIMEOUT_SECONDS) -> httpx.Client:
        return httpx.Client(base_url=self._base_url, headers=self._headers(), timeout=timeout)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiConnectionError(
                f"Could not reach the TeamMeetingAI server at {self._base_url}. "
                f"({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _download(self, path: str, target: Path, *, timeout: float) -> int:
        """Stream a binary endpoint to ``target``; returns bytes written."""

        written = 0
        try:
            with self._client(timeout=timeout) as client, client.stream(
                "GET", path
            ) as response:
                if response.status_code >= 400:
                    # Read the error envelope body before raising.
                    response.read()
                    raise _error_from_response(response)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as destination:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                        destination.write(chunk)
                        written += len(chunk)
        except ApiClientError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ApiConnectionError(
                f"Download from the server failed: {type(exc).__name__}"
            ) from exc
        return written

    # -- health / system -------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")  # type: ignore[return-value]

    def system_status(self) -> dict[str, Any]:
        return self._request("GET", "/system/status")  # type: ignore[return-value]

    # -- profiles ----------------------------------------------------------

    def list_users(self) -> list[dict[str, Any]]:
        return self._request("GET", "/users")  # type: ignore[return-value]

    def create_user(self, display_name: str) -> dict[str, Any]:
        return self._request(
            "POST", "/users", json={"display_name": display_name}
        )  # type: ignore[return-value]

    def get_or_create_user(self, display_name: str) -> dict[str, Any]:
        """Return the profile with this name, creating it when new."""

        for user in self.list_users():
            if str(user.get("display_name", "")).lower() == display_name.lower():
                return user
        return self.create_user(display_name)

    # -- meetings ----------------------------------------------------------

    def list_meetings(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if query:
            params["query"] = query
        if status:
            params["status"] = status
        return self._request("GET", "/meetings", params=params)  # type: ignore[return-value]

    def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        return self._request("GET", f"/meetings/{meeting_id}")  # type: ignore[return-value]

    def delete_meeting(self, meeting_id: str) -> None:
        self._request("DELETE", f"/meetings/{meeting_id}")

    def upload_recording(
        self,
        *,
        title: str,
        user_id: str,
        audio: Path | BinaryIO,
        filename: str = "recording.wav",
        duration_ms: int = 0,
        sample_rate: int = 48_000,
        channels: int = 2,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Host-only: upload a finished recording (creates the meeting)."""

        data: dict[str, Any] = {
            "title": title,
            "user_id": user_id,
            "duration_ms": str(duration_ms),
            "sample_rate": str(sample_rate),
            "channels": str(channels),
        }
        if notes:
            data["notes"] = notes
        if isinstance(audio, Path):
            handle = open(audio, "rb")
        else:
            handle = audio  # caller owns the lifecycle
        try:
            files = {"file": (filename, handle, "audio/wav")}
            return self._request(
                "POST", "/meetings", data=data, files=files,
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )  # type: ignore[return-value]
        finally:
            if isinstance(audio, Path):
                handle.close()

    # -- files -------------------------------------------------------------

    def list_files(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/meetings/{meeting_id}/files"
        )  # type: ignore[return-value]

    def upload_attachment(
        self, meeting_id: str, path: Path, *, content_type: str | None = None
    ) -> dict[str, Any]:
        """Host-only: attach a document or image (50 MB max)."""

        mime = content_type or _guess_mime(path.name)
        files = {"file": (path.name, open(path, "rb"), mime)}
        try:
            return self._request(
                "POST", f"/meetings/{meeting_id}/files", files=files,
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )  # type: ignore[return-value]
        finally:
            files["file"][1].close()

    def download_file(self, file_id: str, target: Path) -> int:
        """Download one stored file (audio or attachment) to ``target``."""

        return self._download(f"/files/{file_id}/download", target,
                              timeout=_UPLOAD_TIMEOUT_SECONDS)

    def delete_attachment(self, file_id: str) -> None:
        self._request("DELETE", f"/files/{file_id}")

    # -- transcripts / summaries --------------------------------------------

    def list_transcripts(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/meetings/{meeting_id}/transcripts"
        )  # type: ignore[return-value]

    def get_summary(self, meeting_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/meetings/{meeting_id}/summary")

    def get_meeting_metrics(self, meeting_id: str) -> dict[str, Any] | None:
        """Reference-free quality metrics for the primary transcript."""

        return self._request(
            "GET", f"/meetings/{meeting_id}/metrics"
        )  # type: ignore[return-value]

    def list_action_items(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/meetings/{meeting_id}/action-items"
        )  # type: ignore[return-value]

    def patch_action_item(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/action-items/{item_id}", json=fields
        )  # type: ignore[return-value]

    # -- speaker profiles -----------------------------------------------------

    def list_meeting_speakers(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/meetings/{meeting_id}/speakers"
        )  # type: ignore[return-value]

    def assign_meeting_speaker(
        self, meeting_id: str, local_label: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Host-only: confirm/reject/reset a speaker label's profile match."""

        return self._request(
            "POST",
            f"/meetings/{meeting_id}/speakers/{local_label}/assign",
            json=fields,
            
        )  # type: ignore[return-value]

    def request_profiling(self, meeting_id: str) -> dict[str, Any]:
        """Host-only: (re-)queue the voice-profiling job; 409 when active."""

        return self._request(
            "POST", f"/meetings/{meeting_id}/speakers/profile"
        )  # type: ignore[return-value]

    def list_speaker_profiles(self) -> list[dict[str, Any]]:
        return self._request("GET", "/profiles")  # type: ignore[return-value]

    def create_speaker_profile(self, display_name: str) -> dict[str, Any]:
        return self._request(
            "POST", "/profiles", json={"display_name": display_name}
        )  # type: ignore[return-value]

    def rename_speaker_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/profiles/{profile_id}",
            json={"display_name": display_name},
            
        )  # type: ignore[return-value]

    def delete_speaker_profile(self, profile_id: str) -> None:
        self._request("DELETE", f"/profiles/{profile_id}")

    # -- jobs ---------------------------------------------------------------

    def list_jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/meetings/{meeting_id}/jobs"
        )  # type: ignore[return-value]

    def transcribe(self, meeting_id: str) -> dict[str, Any]:
        """(Re-)queue transcription; 409 ``job_active`` when one is queued."""

        return self._request(
            "POST", f"/meetings/{meeting_id}/transcribe"
        )  # type: ignore[return-value]

    def analyse(self, meeting_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/meetings/{meeting_id}/analyse"
        )  # type: ignore[return-value]

    def cancel_job(self, meeting_id: str, job_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/meetings/{meeting_id}/jobs/{job_id}/cancel"
        )  # type: ignore[return-value]

    def active_jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        """Jobs of this meeting that are queued or running."""

        jobs = self.list_jobs(meeting_id)
        return [job for job in jobs if job.get("status") in ("queued", "running")]

    # -- search / export ------------------------------------------------------

    def search(
        self, query: str, *, kind: str = "all", limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/search", params={"q": query, "kind": kind, "limit": limit}
        )  # type: ignore[return-value]

    def export_meeting(self, meeting_id: str, target: Path, *, include_audio: bool = True) -> int:
        """Download the meeting export ZIP to ``target``."""

        return self._download(
            f"/meetings/{meeting_id}/export?include_audio={str(include_audio).lower()}",
            target,
            timeout=_UPLOAD_TIMEOUT_SECONDS,
        )


def _guess_mime(filename: str) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"
