"""Configuration and path-safety tests."""

from __future__ import annotations

import pytest

from config import ensure_directories, load_settings


def test_sandbox_settings_use_temp_data_dir(settings):
    assert settings.data_dir.name == "data"
    assert settings.recordings_dir == settings.data_dir / "recordings"
    assert settings.library_dir == settings.data_dir / "library"
    assert settings.data_dir.is_dir()


def test_environment_overrides(tmp_path, monkeypatch):
    data_dir = tmp_path / "custom"
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TEAMMEETINGAI_SAMPLE_RATE", "44100")
    monkeypatch.setenv("TEAMMEETINGAI_OLLAMA_MODEL", "custom-model")
    monkeypatch.setenv("TEAMMEETINGAI_LLM_MAX_TRANSCRIPT_CHARS", "12345")

    loaded = load_settings()
    assert loaded.data_dir == data_dir.resolve()
    assert loaded.audio_sample_rate == 44100
    assert loaded.ollama_model == "custom-model"
    assert loaded.llm_max_transcript_chars == 12345


def test_invalid_environment_numbers_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEAMMEETINGAI_SAMPLE_RATE", "not-a-number")
    monkeypatch.setenv("TEAMMEETINGAI_LLM_TEMPERATURE", "not-a-number")
    monkeypatch.setenv("TEAMMEETINGAI_GROQ_TIMEOUT", "not-a-number")
    monkeypatch.setenv("TEAMMEETINGAI_WHISPER_BEAM_SIZE", "not-a-number")
    loaded = load_settings()
    assert loaded.audio_sample_rate == 48_000
    assert loaded.llm_temperature == 0.2
    assert loaded.groq_timeout == 60.0
    assert loaded.whisper_beam_size == 5


def test_analysis_backend_and_cloud_opt_in_defaults(settings):
    assert settings.analysis_backend == "none"
    assert settings.allow_cloud_analysis is False
    assert settings.groq_model == ""
    assert settings.groq_timeout == 60.0


def test_analysis_backend_and_cloud_opt_in_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEAMMEETINGAI_ANALYSIS_BACKEND", "groq")
    monkeypatch.setenv("TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS", "1")
    monkeypatch.setenv("TEAMMEETINGAI_GROQ_MODEL", "llama-test")
    monkeypatch.setenv("TEAMMEETINGAI_GROQ_TIMEOUT", "12.5")
    loaded = load_settings()
    assert loaded.analysis_backend == "groq"
    assert loaded.allow_cloud_analysis is True
    assert loaded.groq_model == "llama-test"
    assert loaded.groq_timeout == 12.5


def test_allow_cloud_analysis_truthy_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    for raw in ("true", "YES", "on"):
        monkeypatch.setenv("TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS", raw)
        assert load_settings().allow_cloud_analysis is True
    monkeypatch.setenv("TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS", "0")
    assert load_settings().allow_cloud_analysis is False


def test_whisper_tuning_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEAMMEETINGAI_WHISPER_LANGUAGE", "en")
    monkeypatch.setenv("TEAMMEETINGAI_WHISPER_BEAM_SIZE", "3")
    monkeypatch.setenv("TEAMMEETINGAI_WHISPER_CPU_THREADS", "4")
    loaded = load_settings()
    assert loaded.whisper_language == "en"
    assert loaded.whisper_beam_size == 3
    assert loaded.whisper_cpu_threads == 4


def test_ensure_directories_creates_data_dir(tmp_path):
    # Use an explicit isolated directory without depending on the user env.
    from dataclasses import replace

    isolated = replace(
        load_settings(),
        data_dir=tmp_path / "app",
        recordings_dir=tmp_path / "app" / "recordings",
        library_dir=None,
    )
    ensure_directories(isolated)
    assert isolated.data_dir.is_dir()
    # The library is only created when the app opens it, not at startup.
    assert not isolated.recordings_dir.exists()


def test_library_dir_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TEAMMEETINGAI_LIBRARY_DIR", raising=False)
    assert load_settings().library_dir is None


def test_library_dir_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    from config import save_library_dir

    # A persisted value would normally win, but the env var has priority.
    save_library_dir(tmp_path / "data", tmp_path / "from-config")
    monkeypatch.setenv("TEAMMEETINGAI_LIBRARY_DIR", str(tmp_path / "from-env"))
    assert load_settings().library_dir == (tmp_path / "from-env").resolve()


def test_library_dir_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMMEETINGAI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TEAMMEETINGAI_LIBRARY_DIR", raising=False)

    from config import load_library_dir, save_library_dir

    save_library_dir(tmp_path / "data", tmp_path / "mylib")
    assert load_library_dir(tmp_path / "data") == (tmp_path / "mylib").resolve()
    assert load_settings().library_dir == (tmp_path / "mylib").resolve()


def test_validate_library_dir_creates_directory(tmp_path):
    from config import validate_library_dir

    target = tmp_path / "newlib" / "nested"
    resolved = validate_library_dir(target)
    assert resolved == target.resolve()
    assert resolved.is_dir()


def test_validate_library_dir_rejects_unusable_path(tmp_path):
    from config import validate_library_dir

    blocker = tmp_path / "occupied.txt"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_library_dir(blocker)
