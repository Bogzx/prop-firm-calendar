"""Backend for Google Gemini via the google-genai SDK."""

from __future__ import annotations

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from prop_firm_calendar.parsing.llm import BackendError


class GeminiBackend:
    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    def complete(self, prompt: str, model: str) -> str:
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
        except genai_errors.APIError as e:
            raise BackendError(str(e)) from e
        if not response.text:
            raise BackendError(f"empty response from {model}")
        return response.text
