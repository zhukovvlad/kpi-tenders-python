"""Thin wrapper around the google-genai SDK.

A single `GeminiClient` instance is constructed per-task via `get_client()`.
It is intentionally NOT a singleton because Celery forks workers and sharing a
connection across forks is unsafe.

Raises
------
GeminiAPIError
    Permanent Gemini failure (bad key, invalid schema, 4xx). Not retried by
    the Celery lifecycle because it inherits from ``ValueError`` which is in
    ``workers.base._NO_RETRY``.
Exception (any other)
    Transient failure (5xx, network). Celery will retry.
"""

import logging
from typing import Any

from google import genai
from google.genai import types

from app.config import get_settings

log = logging.getLogger(__name__)


class GeminiAPIError(ValueError):
    """Permanent Gemini failure — do not retry this task."""


class GeminiClient:
    """Lightweight wrapper around ``genai.Client`` with structured-output support."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    def generate(
        self,
        *,
        model: str,
        contents: str,
        response_schema: type,
        temperature: float = 0.0,
    ) -> Any:
        """Call Gemini and return the **parsed** Pydantic instance.

        Parameters
        ----------
        model:
            Gemini model name, e.g. ``"gemini-2.0-flash"``.
        contents:
            Prompt string sent to the model.
        response_schema:
            A Pydantic ``BaseModel`` class. The SDK enforces the schema via
            ``response_mime_type="application/json"`` + ``response_schema``.
        temperature:
            Sampling temperature. 0.0 for deterministic extraction.

        Returns
        -------
        Any
            Instance of *response_schema*.

        Raises
        ------
        GeminiAPIError
            On permanent failures (authentication, quota, schema mismatch).
        Exception
            On transient failures (5xx, network).
        """
        try:
            resp = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=temperature,
                ),
            )
        except Exception as exc:
            # Classify permanent vs transient failures.
            exc_str = str(exc).lower()
            is_permanent = any(
                marker in exc_str
                for marker in (
                    "invalid argument",
                    "permission denied",
                    "api key",
                    "unauthorized",
                    "bad request",
                    "400",
                    "403",
                )
            )
            if is_permanent:
                raise GeminiAPIError(f"Permanent Gemini error: {exc}") from exc
            raise  # transient — let Celery retry

        if resp.parsed is None:
            raw_preview = (resp.text or "")[:200]
            raise GeminiAPIError(
                f"Gemini returned no parsed output for model={model!r}. "
                f"Raw text preview: {raw_preview!r}"
            )

        return resp.parsed


def get_client() -> GeminiClient:
    """Construct a fresh ``GeminiClient`` from settings.

    Call once per task invocation (not at module import time) so that Celery
    forks don't share a connection.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        raise GeminiAPIError(
            "GEMINI_API_KEY is not set. Set it in .env or environment before running LLM workers."
        )
    return GeminiClient(api_key=settings.gemini_api_key)
