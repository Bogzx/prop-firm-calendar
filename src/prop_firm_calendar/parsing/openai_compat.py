"""Backend for any OpenAI-protocol endpoint: OpenRouter, OpenAI, Groq, Ollama, …"""

from __future__ import annotations

from openai import OpenAI, OpenAIError

from prop_firm_calendar.parsing.llm import BackendError


class OpenAICompatBackend:
    def __init__(self, api_key: str, base_url: str = "") -> None:
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)

    def complete(self, prompt: str, model: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                seed=42,  # determinism hint; honored by some providers, ignored by others
            )
        except OpenAIError as e:
            raise BackendError(str(e)) from e
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise BackendError(f"empty response from {model}")
        return content
