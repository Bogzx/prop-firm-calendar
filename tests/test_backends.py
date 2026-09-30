import pytest

from prop_firm_calendar.config import ConfigError, LLMConfig
from prop_firm_calendar.parsing.factory import make_backend


def test_missing_api_key_rejected() -> None:
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        make_backend(LLMConfig(api_key=""))


def test_gemini_backend_selected() -> None:
    backend = make_backend(LLMConfig(provider="gemini", api_key="k"))
    assert type(backend).__name__ == "GeminiBackend"


def test_openai_compatible_backend_selected() -> None:
    cfg = LLMConfig(
        provider="openai-compatible", api_key="k", base_url="https://openrouter.ai/api/v1"
    )
    backend = make_backend(cfg)
    assert type(backend).__name__ == "OpenAICompatBackend"


def test_openai_compatible_backend_gets_a_request_timeout() -> None:
    # Without an explicit timeout the SDK waits up to 10 min per attempt, and one
    # hung request stalls the whole sync (seen live on 2026-09-30).
    backend = make_backend(
        LLMConfig(provider="openai-compatible", api_key="k", request_timeout_sec=45)
    )
    client = backend._client  # type: ignore[attr-defined]
    assert client.timeout == 45
    assert client.max_retries == 1


def test_default_request_timeout_is_bounded() -> None:
    backend = make_backend(LLMConfig(provider="openai-compatible", api_key="k"))
    assert backend._client.timeout == 120  # type: ignore[attr-defined]
