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
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from google import genai
from google.genai.types import HttpOptions
from pydantic import ValidationError

from app.config import get_settings

log = logging.getLogger(__name__)


class GeminiAPIError(ValueError):
    """Permanent Gemini failure — do not retry this task."""


class GeminiClient:
    """Lightweight wrapper around ``genai.Client`` with structured-output support."""

    def __init__(self, api_key: str) -> None:
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        self._httpx_client: httpx.Client | None = None
        http_options: HttpOptions | None = None
        if proxy_url:
            self._httpx_client = httpx.Client(proxy=proxy_url, follow_redirects=True)
            http_options = HttpOptions(httpx_client=self._httpx_client)
            parsed = urlparse(proxy_url)
            log.debug("GeminiClient: using proxy %s://%s", parsed.scheme, parsed.hostname)
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    def close(self) -> None:
        """Close any owned network resources."""
        if self._httpx_client is not None:
            self._httpx_client.close()
            self._httpx_client = None
        if self._client is not None:
            self._client.close()
            self._client = None

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
            Gemini model name, e.g. ``"gemini-2.5-flash"``.
        contents:
            Prompt string sent to the model.
        response_schema:
            A Pydantic ``BaseModel`` class. Its JSON schema is sent via
            ``response_json_schema``; the response text is validated with
            ``model_validate_json``.
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
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": response_schema.model_json_schema(),
                    "temperature": temperature,
                },
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

        raw = resp.text or ""
        if not raw.strip():
            raise GeminiAPIError(f"Gemini returned empty response for model={model!r}")

        try:
            return response_schema.model_validate_json(raw)
        except ValidationError as exc:
            raise GeminiAPIError(
                f"Gemini response does not match expected schema for model={model!r}. "
                f"Raw preview: {raw[:200]!r}"
            ) from exc


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
