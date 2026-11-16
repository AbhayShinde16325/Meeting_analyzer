"""Desktop client: HTTP access to the TeamMeetingAI server.

The PySide6 UI never touches PostgreSQL directly; every read and write goes
through :mod:`client.api_client`.  The client is synchronous (httpx) and is
always called from ``QThread`` workers so the UI thread never blocks on the
network.
"""

from client.api_client import (
    ApiClientError,
    ApiConnectionError,
    TeamMeetingAIClient,
)

__all__ = ["ApiClientError", "ApiConnectionError", "TeamMeetingAIClient"]
