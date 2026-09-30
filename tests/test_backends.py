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
