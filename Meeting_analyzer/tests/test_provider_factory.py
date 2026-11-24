"""Tests for the analysis-backend provider factory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ai.faster_whisper_provider import FasterWhisperProvider
from ai.groq_provider import GroqAnalysisProvider
from ai.provider_factory import (
    ProviderConfigurationError,
    build_analysis_provider,
    build_transcription_provider,
    describe_analysis_mode,
)


def test_default_backend_is_none(settings):
    assert settings.analysis_backend == "none"
    assert build_analysis_provider(settings) is None


def test_groq_backend_requires_explicit_cloud_opt_in(settings):
    groq_settings = replace(settings, analysis_backend="groq")
    with pytest.raises(ProviderConfigurationError, match="ALLOW_CLOUD_ANALYSIS"):
        build_analysis_provider(groq_settings)


def test_groq_backend_builds_provider_when_opted_in(settings):
    groq_settings = replace(
        settings,
        analysis_backend="groq",
        allow_cloud_analysis=True,
        groq_model="test-model",
    )
    provider = build_analysis_provider(groq_settings)
    assert isinstance(provider, GroqAnalysisProvider)
    assert provider.model_name == "test-model"
    assert provider.is_cloud is True


def test_ollama_backend_is_unsupported(settings):
    ollama_settings = replace(
        settings,
        analysis_backend="ollama",
        allow_cloud_analysis=True,
    )
    with pytest.raises(ProviderConfigurationError, match="not supported"):
        build_analysis_provider(ollama_settings)


def test_unknown_backend_is_rejected(settings):
    unknown = replace(settings, analysis_backend="wasm")
    with pytest.raises(ProviderConfigurationError, match="Unknown analysis backend"):
        build_analysis_provider(unknown)


def test_backend_value_is_normalised(settings):
    groq_settings = replace(
        settings,
        analysis_backend="  GROQ ",
        allow_cloud_analysis=True,
        groq_model="test-model",
    )
    assert isinstance(build_analysis_provider(groq_settings), GroqAnalysisProvider)


def test_transcription_provider_is_always_local(settings):
    provider = build_transcription_provider(settings)
    assert isinstance(provider, FasterWhisperProvider)
    assert provider.is_cloud is False


def test_describe_analysis_mode(settings):
    assert (
        describe_analysis_mode(settings, None)
        == "transcription only (no analysis backend configured)"
    )
    cloud = replace(
        settings,
        analysis_backend="groq",
        allow_cloud_analysis=True,
        groq_model="llama-test",
    )
    provider = build_analysis_provider(cloud)
    assert "leaves this computer" in describe_analysis_mode(cloud, provider)
    assert describe_analysis_mode(settings, FasterWhisperProvider(settings)) == (
        "local analysis via small"
    )
