"""Tests for the Groq cloud analysis provider (fully mocked HTTP client)."""

from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from ai.groq_provider import GroqAnalysisProvider
from ai.summarization import SummarizationError


def cloud_settings(settings, **overrides):
    base = dict(
        analysis_backend="groq",
        allow_cloud_analysis=True,
        groq_model="test-model",
        groq_timeout=5.0,
        llm_temperature=0.1,
    )
    base.update(overrides)
    return replace(settings, **base)


class _FakeCompletions:
    def __init__(self, client) -> None:
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(kwargs)
        error = self._client.error
        if error is not None:
            raise error
        content = self._client.content

        class _Message:
            def __init__(self, text):
                self.content = text

        class _Choice:
            def __init__(self, text):
                self.message = _Message(text)

        class _Response:
            def __init__(self, text):
                self.choices = [_Choice(text)]

        return _Response(content)


class _FakeChat:
    def __init__(self, client) -> None:
        self.completions = _FakeCompletions(client)


class FakeGroqClient:
    """Records constructor and call arguments; returns a canned response."""

    instances: list["FakeGroqClient"] = []
    # Class-level knobs consumed at construction time, so tests can configure
    # a failure before the provider creates its client inside analyze().
    pending_error: Exception | None = None
    pending_content: str | None = None

    def __init__(self, api_key=None, timeout=None, max_retries=None):
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.calls: list[dict] = []
        self.content = FakeGroqClient.pending_content or '{"overview": "ok"}'
        self.error = FakeGroqClient.pending_error
        FakeGroqClient.pending_error = None
        FakeGroqClient.pending_content = None
        FakeGroqClient.instances.append(self)
        self.chat = _FakeChat(self)


def install_fake_groq(monkeypatch, *, module: types.ModuleType | None = None):
    FakeGroqClient.instances.clear()
    FakeGroqClient.pending_error = None
    FakeGroqClient.pending_content = None
    if module is None:
        module = types.ModuleType("groq")

        class AuthenticationError(Exception): ...

        class RateLimitError(Exception): ...

        class NotFoundError(Exception): ...

        class APITimeoutError(Exception): ...

        class APIConnectionError(Exception): ...

        class APIStatusError(Exception): ...

        module.AuthenticationError = AuthenticationError
        module.RateLimitError = RateLimitError
        module.NotFoundError = NotFoundError
        module.APITimeoutError = APITimeoutError
        module.APIConnectionError = APIConnectionError
        module.APIStatusError = APIStatusError
        module.Groq = FakeGroqClient
    monkeypatch.setitem(sys.modules, "groq", module)
    return module


@pytest.fixture
def provider(settings):
    return GroqAnalysisProvider(
        cloud_settings(settings),
        api_key="test-key",
        model="test-model",
        timeout=7.5,
    )


def test_cloud_analysis_requires_opt_in_flag(settings, monkeypatch):
    install_fake_groq(monkeypatch)
    # settings default: allow_cloud_analysis=False
    provider = GroqAnalysisProvider(settings, api_key="k", model="m")
    with pytest.raises(SummarizationError, match="not enabled"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_missing_api_key_is_rejected(settings, monkeypatch):
    install_fake_groq(monkeypatch)
    provider = GroqAnalysisProvider(cloud_settings(settings))
    assert provider.model_name == "test-model"
    with pytest.raises(SummarizationError, match="TEAMMEETINGAI_GROQ_API_KEY"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_missing_model_is_rejected(settings, monkeypatch):
    install_fake_groq(monkeypatch)
    provider = GroqAnalysisProvider(cloud_settings(settings, groq_model=""), api_key="k")
    with pytest.raises(SummarizationError, match="TEAMMEETINGAI_GROQ_MODEL"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_missing_groq_package_gives_clear_error(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "groq", None)  # forces ImportError on import
    provider = GroqAnalysisProvider(
        cloud_settings(settings), api_key="k", model="m"
    )
    with pytest.raises(SummarizationError, match="pip install groq"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_successful_call_returns_content_and_sends_prompts(provider, monkeypatch):
    install_fake_groq(monkeypatch)
    content = provider.analyze(system_prompt="SYSTEM", user_prompt="USER")
    assert content == '{"overview": "ok"}'

    client = FakeGroqClient.instances[-1]
    assert client.api_key == "test-key"
    assert client.timeout == 7.5
    assert client.max_retries == 0

    call = client.calls[0]
    assert call["model"] == "test-model"
    assert call["temperature"] == 0.1
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert call["messages"][1] == {"role": "user", "content": "USER"}


def test_empty_response_is_rejected(provider, monkeypatch):
    install_fake_groq(monkeypatch)
    FakeGroqClient.pending_content = "   "
    with pytest.raises(SummarizationError, match="empty response"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_authentication_error_is_mapped(provider, monkeypatch):
    module = install_fake_groq(monkeypatch)
    FakeGroqClient.pending_error = module.AuthenticationError("bad key")
    with pytest.raises(SummarizationError, match="rejected the API key"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_rate_limit_error_is_mapped(provider, monkeypatch):
    module = install_fake_groq(monkeypatch)
    FakeGroqClient.pending_error = module.RateLimitError("429")
    with pytest.raises(SummarizationError, match="rate limit"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_timeout_error_is_mapped(provider, monkeypatch):
    module = install_fake_groq(monkeypatch)
    FakeGroqClient.pending_error = module.APITimeoutError("timeout")
    with pytest.raises(SummarizationError, match="Could not reach Groq"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_unknown_model_error_is_mapped(provider, monkeypatch):
    module = install_fake_groq(monkeypatch)
    FakeGroqClient.pending_error = module.NotFoundError("no model")
    with pytest.raises(SummarizationError, match="not available"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_generic_http_error_is_mapped(provider, monkeypatch):
    module = install_fake_groq(monkeypatch)
    error = module.APIStatusError("boom")
    error.status_code = 500
    FakeGroqClient.pending_error = error
    with pytest.raises(SummarizationError, match="HTTP 500"):
        provider.analyze(system_prompt="s", user_prompt="u")


def test_api_key_is_read_from_environment(settings, monkeypatch):
    install_fake_groq(monkeypatch)
    monkeypatch.setenv("TEAMMEETINGAI_GROQ_API_KEY", "env-key")
    provider = GroqAnalysisProvider(cloud_settings(settings))
    provider.analyze(system_prompt="s", user_prompt="u")
    assert FakeGroqClient.instances[-1].api_key == "env-key"
