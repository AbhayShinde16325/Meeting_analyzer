"""Application configuration and local storage paths.

Everything in TeamMeetingAI is local to the current user's computer.  This
module centralises the filesystem locations and processing defaults so that no
other module has to guess where user data lives.

Configuration can be overridden with environment variables (see
``load_settings``) which keeps secrets and machine-specific paths out of the
source tree.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

APP_NAME = "TeamMeetingAI"
APP_DISPLAY_NAME = "TeamMeetingAI"
APP_VERSION = "0.1.0"


def _default_data_dir() -> Path:
    """Return the per-user application data directory for this platform."""

    override = os.environ.get("TEAMMEETINGAI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME

    if sys.platform == "darwin":  # pragma: no cover - non-Windows path
        return Path.home() / "Library" / "Application Support" / APP_NAME

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def config_json_path(data_dir: Path) -> Path:
    """Return the path of the small app-settings JSON file."""

    return Path(data_dir) / "config.json"


def load_library_dir(data_dir: Path) -> Path | None:
    """Return the configured meeting library directory, if any.

    The ``TEAMMEETINGAI_LIBRARY_DIR`` environment variable always wins over
    the value persisted in ``<data_dir>\\config.json``.
    """

    override = os.environ.get("TEAMMEETINGAI_LIBRARY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    path = config_json_path(data_dir)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    raw = config.get("library_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()


def save_library_dir(data_dir: Path, library_dir: Path) -> None:
    """Persist the meeting library directory in the app settings file."""

    path = config_json_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, TypeError, ValueError):
        pass
    existing["library_dir"] = str(Path(library_dir))
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temp_path, path)


def validate_library_dir(path: Path | str) -> Path:
    """Validate (and create) a meeting library directory.

    Returns the resolved absolute path.  Raises :class:`ValueError` with a
    user-facing message when the directory cannot be created or written to.
    """

    candidate = Path(path).expanduser()
    if not str(candidate).strip():
        raise ValueError("Choose a folder for the meeting library.")
    try:
        resolved = candidate.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        handle, probe_name = tempfile.mkstemp(dir=resolved, prefix=".tma-")
        os.close(handle)
        Path(probe_name).unlink()
    except OSError as exc:
        raise ValueError(
            f"That folder cannot be used as the meeting library: {exc}"
        ) from exc
    return resolved


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings.

    Values are resolved once at startup.  A frozen dataclass keeps accidental
    mutation out of the UI and worker threads.
    """

    data_dir: Path
    recordings_dir: Path

    # The meeting library is the user-chosen directory that holds every
    # meeting folder (audio, transcript.md, analysis.md, meeting.json).
    # ``None`` until the user picks one on first launch.
    library_dir: Path | None = None

    # Audio capture defaults.  Microphone and system audio are kept as separate
    # tracks while recording and mixed into one WAV for transcription/playback.
    audio_sample_rate: int = 48_000
    audio_block_frames: int = 1_024
    microphone_channels: int = 1
    system_channels: int = 2

    # Local speech-to-text (Faster-Whisper).
    whisper_model_size: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    # 1 selects greedy decoding, which is the fastest option and the default
    # here.  Higher values switch to beam search (slower, marginally better).
    whisper_beam_size: int = 1
    # Blank means auto-detect the spoken language.  Pinning it (for example
    # "hi" for Hindi) is the most effective guardrail against gibberish.
    whisper_language: str = ""
    # Optional prompt that biases vocabulary and script (for example a short
    # Hindi sentence to keep the output in Devanagari).
    whisper_initial_prompt: str = ""
    # Silence longer than this is skipped by voice-activity detection, which
    # stops the model inventing sentences in quiet passages.
    whisper_vad_min_silence_ms: int = 500
    # 0 means use the Faster-Whisper / ctranslate2 default.
    whisper_cpu_threads: int = 0

    # Meeting analysis backend selection.  "none" keeps the pipeline at
    # transcription only; "groq" sends the transcript to the Groq cloud API and
    # therefore requires an explicit opt-in flag; "ollama" is reserved for a
    # future local backend and is not supported in this build.
    analysis_backend: str = "none"
    allow_cloud_analysis: bool = False

    # Groq cloud analysis.  The API key itself is deliberately NOT stored in
    # Settings; the provider reads TEAMMEETINGAI_GROQ_API_KEY from the
    # environment directly so it never appears in a Settings repr or logs.
    groq_model: str = ""
    groq_timeout: float = 60.0

    # Local LLM (Ollama).  Reserved for a future local analysis backend.
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    llm_temperature: float = 0.2
    llm_max_transcript_chars: int = 40_000

    # Search/listing limits.
    meeting_list_limit: int = 500

    extra: Mapping[str, str] = field(default_factory=dict)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """Build :class:`Settings` from defaults augmented by environment values.

    Supported overrides:
        TEAMMEETINGAI_DATA_DIR
        TEAMMEETINGAI_LIBRARY_DIR
        TEAMMEETINGAI_SAMPLE_RATE
        TEAMMEETINGAI_WHISPER_MODEL
        TEAMMEETINGAI_WHISPER_DEVICE
        TEAMMEETINGAI_WHISPER_COMPUTE_TYPE
        TEAMMEETINGAI_WHISPER_LANGUAGE
        TEAMMEETINGAI_WHISPER_INITIAL_PROMPT
        TEAMMEETINGAI_WHISPER_BEAM_SIZE
        TEAMMEETINGAI_WHISPER_VAD_MIN_SILENCE_MS
        TEAMMEETINGAI_WHISPER_CPU_THREADS
        TEAMMEETINGAI_ANALYSIS_BACKEND      ("none" | "groq")
        TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS  ("1" to opt in to cloud analysis)
        TEAMMEETINGAI_GROQ_MODEL
        TEAMMEETINGAI_GROQ_TIMEOUT
        TEAMMEETINGAI_GROQ_API_KEY          (read by the provider, not stored here)
        TEAMMEETINGAI_OLLAMA_HOST
        TEAMMEETINGAI_OLLAMA_MODEL
        TEAMMEETINGAI_LLM_TEMPERATURE
        TEAMMEETINGAI_LLM_MAX_TRANSCRIPT_CHARS
    """

    data_dir = _default_data_dir()
    recordings_dir = data_dir / "recordings"
    library_dir = load_library_dir(data_dir)

    return Settings(
        data_dir=data_dir,
        recordings_dir=recordings_dir,
        library_dir=library_dir,
        audio_sample_rate=_int_env("TEAMMEETINGAI_SAMPLE_RATE", 48_000),
        whisper_model_size=os.environ.get("TEAMMEETINGAI_WHISPER_MODEL", "small"),
        whisper_device=os.environ.get("TEAMMEETINGAI_WHISPER_DEVICE", "cpu"),
        whisper_compute_type=os.environ.get(
            "TEAMMEETINGAI_WHISPER_COMPUTE_TYPE", "int8"
        ),
        whisper_beam_size=_int_env("TEAMMEETINGAI_WHISPER_BEAM_SIZE", 1),
        whisper_language=os.environ.get("TEAMMEETINGAI_WHISPER_LANGUAGE", ""),
        whisper_initial_prompt=os.environ.get(
            "TEAMMEETINGAI_WHISPER_INITIAL_PROMPT", ""
        ),
        whisper_vad_min_silence_ms=_int_env(
            "TEAMMEETINGAI_WHISPER_VAD_MIN_SILENCE_MS", 500
        ),
        whisper_cpu_threads=_int_env("TEAMMEETINGAI_WHISPER_CPU_THREADS", 0),
        analysis_backend=os.environ.get("TEAMMEETINGAI_ANALYSIS_BACKEND", "none"),
        allow_cloud_analysis=_bool_env("TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS"),
        groq_model=os.environ.get("TEAMMEETINGAI_GROQ_MODEL", ""),
        groq_timeout=_float_env("TEAMMEETINGAI_GROQ_TIMEOUT", 60.0),
        ollama_host=os.environ.get(
            "TEAMMEETINGAI_OLLAMA_HOST", "http://localhost:11434"
        ),
        ollama_model=os.environ.get("TEAMMEETINGAI_OLLAMA_MODEL", "qwen3:4b"),
        llm_temperature=_float_env("TEAMMEETINGAI_LLM_TEMPERATURE", 0.2),
        llm_max_transcript_chars=_int_env(
            "TEAMMEETINGAI_LLM_MAX_TRANSCRIPT_CHARS", 40_000
        ),
    )


def ensure_directories(settings: Settings) -> None:
    """Create the application data directory if it does not exist yet."""

    settings.data_dir.mkdir(parents=True, exist_ok=True)
