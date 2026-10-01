"""Construct the configured LLM backends and extractors (SDKs imported lazily)."""

from __future__ import annotations

from collections.abc import Callable

from prop_firm_calendar.config import ConfigError, LLMConfig
from prop_firm_calendar.parsing.llm import (
    EventExtractor,
    Juror,
    LLMBackend,
    PanelExtractor,
)

#: Builds one firm's extractor from that firm's prompt hints.
ExtractorFactory = Callable[[str], EventExtractor | PanelExtractor]


def make_backend(cfg: LLMConfig) -> LLMBackend:
    if not cfg.api_key:
        raise ConfigError(
            "no API key found — set the LLM_API_KEY environment variable "
            "(or GEMINI_API_KEY for backward compatibility), e.g. in a .env file"
        )
    return _backend(cfg.provider, cfg.api_key, cfg.base_url, cfg.request_timeout_sec)


def _backend(provider: str, api_key: str, base_url: str, timeout: float) -> LLMBackend:
    if provider == "gemini":
        from prop_firm_calendar.parsing.gemini import GeminiBackend

        return GeminiBackend(api_key)
    from prop_firm_calendar.parsing.openai_compat import OpenAICompatBackend

    return OpenAICompatBackend(api_key, base_url, timeout=timeout)


def make_jurors(cfg: LLMConfig) -> list[Juror]:
    """One Juror per `[[llm.panel]]` member; members sharing an endpoint share a client."""
    clients: dict[tuple[str, str, str], LLMBackend] = {}
    jurors: list[Juror] = []
    for member in cfg.panel:
        if not member.api_key:
            raise ConfigError(
                f"no API key for panel model {member.label!r} — set the "
                f"{member.api_key_env or 'LLM_API_KEY'} environment variable, e.g. in a .env file"
            )
        endpoint = (member.provider, member.base_url, member.api_key)
        if endpoint not in clients:
            clients[endpoint] = _backend(
                member.provider, member.api_key, member.base_url, cfg.request_timeout_sec
            )
        jurors.append(Juror(member.label, clients[endpoint], member.model))
    return jurors


def make_extractor_factory(cfg: LLMConfig) -> ExtractorFactory:
    """Clients are built once per sync; each firm gets an extractor with its own hints.

    Without `[[llm.panel]]` this is exactly the long-standing extractor: the
    first model in `models` that answers, `consensus_runs` times.
    """
    if cfg.panel:
        jurors = make_jurors(cfg)
        return lambda hints: PanelExtractor(jurors, cfg.quorum, prompt_hints=hints)
    backend = make_backend(cfg)
    return lambda hints: EventExtractor(
        backend, cfg.models, consensus_runs=cfg.consensus_runs, prompt_hints=hints
    )


def calls_per_extraction(cfg: LLMConfig) -> int:
    """LLM calls one post costs at least (repair retries and fallbacks add more)."""
    return len(cfg.panel) if cfg.panel else cfg.consensus_runs


def describe_extraction(cfg: LLMConfig) -> dict:
    """How events are extracted, for /healthz: lets anyone check what agrees with what."""
    if cfg.panel:
        return {
            "mode": "panel",
            "models": [m.label for m in cfg.panel],
            "quorum": cfg.quorum,
        }
    return {"mode": "consensus", "models": list(cfg.models), "runs": cfg.consensus_runs}
