"""TeamMeetingAI entry point.

The React frontend lives in `client/`. This module keeps a lightweight launcher for the FastAPI server and
prints clear frontend run instructions. Existing backend logic is unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import APP_VERSION, Settings, ensure_directories, load_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _load_local_env(env_file: Path | None = None) -> bool:
    """Load optional .env file if present; return True if file was valid."""

    env_file = env_file if env_file is not None else (
        Path(__file__).parent / ".env"
    )
    if not env_file.is_file():
        return False
    import os
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in os.environ:
                # Existing env var found - don't override, but file is valid
                continue
            os.environ[key] = value
    except Exception:
        return False
    return True


def _configure_logging(settings: Settings) -> None:
    """Send operational logs to the app data directory.

    The log contains operational events and meeting IDs only - never audio,
    transcript text, passwords, tokens, or other sensitive content.
    """

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handler = RotatingFileHandler(
            settings.data_dir / "teammeetingai.log",
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _frontend_hint() -> str:
    root = Path(__file__).resolve().parents[1]
    client_dir = root / "client"
    if client_dir.is_dir():
        return (
            "React client:\n"
            f"  cd {client_dir}\n"
            "  npm install\n"
            "  npm run dev\n"
            "  open http://localhost:5173"
        )
    return "React client directory not found."


def main() -> int:
    """Run the FastAPI server (React frontend lives in client/)."""

    _load_local_env()
    settings = load_settings()
    ensure_directories(settings)
    _configure_logging(settings)
    logger.info("TeamMeetingAI %s server starting.", APP_VERSION)

    host = os.environ.get("TEAMMEETINGAI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TEAMMEETINGAI_API_PORT", "8000"))
    print(f"Starting TeamMeetingAI API on http://{host}:{port}")
    print(_frontend_hint())

    import uvicorn

    uvicorn.run("api.app:app", host=host, port=port)
    return 0





if __name__ == "__main__":
    sys.exit(main())
