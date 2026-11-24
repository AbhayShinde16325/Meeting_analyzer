"""Frontend consolidation smoke checks.

Ensures the React
client remains the sole frontend and that key API contracts are still exposed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from api.app import build_app


def test_react_client_is_present():
    root = Path(__file__).resolve().parents[2]
    client = root / "client"
    assert (client / "package.json").is_file()
    assert (client / "src" / "App.jsx").is_file()
    assert (client / "src" / "pages" / "MeetingDetailsPage.jsx").is_file()


def test_legacy_desktop_ui_removed():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "ui").exists()
    assert not (root / "styles").exists()
    assert not (root / "client").exists()


def _collect_paths(routes):
    out = set()
    for r in routes:
        path = getattr(r, "path", None)
        if isinstance(r, APIRoute) and path:
            out.add(path)
        children = getattr(r, "routes", None)
        if children:
            out |= _collect_paths(children)
        original = getattr(r, "original_router", None)
        if original is not None:
            out |= _collect_paths(getattr(original, "routes", []))
    return out


def test_api_routes_cover_react_needs():
    app = build_app()
    paths = _collect_paths(app.routes) | _collect_paths(getattr(app.router, "routes", []))
    required = {
        "/health",
        "/system/status",
        "/users",
        "/meetings",
        "/meetings/{meeting_id}",
        "/meetings/{meeting_id}/transcribe",
        "/meetings/{meeting_id}/analyse",
        "/meetings/{meeting_id}/speakers",
        "/meetings/{meeting_id}/metrics",
        "/files/{file_id}",
    }
    missing = required - paths
    assert not missing, f"Missing routes: {sorted(missing)}"
