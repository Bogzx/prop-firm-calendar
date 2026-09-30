"""Construct the configured LLM backend (SDKs imported lazily)."""

from __future__ import annotations

from prop_firm_calendar.config import ConfigError, LLMConfig
from prop_firm_calendar.parsing.llm import LLMBackend


def make_backend(cfg: LLMConfig) -> LLMBackend:
    if not cfg.api_key:
        raise ConfigError(
            "no API key found — set the LLM_API_KEY environment variable "
            "(or GEMINI_API_KEY for backward compatibility), e.g. in a .env file"
        )
    if cfg.provider == "gemini":
        from prop_firm_calendar.parsing.gemini import GeminiBackend

        return GeminiBackend(cfg.api_key)
    from prop_firm_calendar.parsing.openai_compat import OpenAICompatBackend

    return OpenAICompatBackend(cfg.api_key, cfg.base_url, timeout=cfg.request_timeout_sec)
