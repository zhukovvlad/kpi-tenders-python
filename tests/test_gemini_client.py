"""Unit tests for GeminiClient.generate() — validation and error-mapping paths."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from app.llm.gemini_client import GeminiAPIError, GeminiClient


class _Schema(BaseModel):
    value: str


def _make_client() -> GeminiClient:
    return GeminiClient(api_key="test-key")


def _mock_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# generate() — happy path
# ---------------------------------------------------------------------------


def test_generate_returns_parsed_instance():
    client = _make_client()
    with patch.object(
        client._client.models,
        "generate_content",
        return_value=_mock_response('{"value": "hello"}'),
    ):
        result = client.generate(
            model="gemini-2.5-flash",
            contents="prompt",
            response_schema=_Schema,
        )
    assert isinstance(result, _Schema)
    assert result.value == "hello"


# ---------------------------------------------------------------------------
# generate() — empty response → GeminiAPIError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", None])
def test_generate_empty_response_raises(text):
    client = _make_client()
    resp = MagicMock()
    resp.text = text
    with (
        patch.object(client._client.models, "generate_content", return_value=resp),
        pytest.raises(GeminiAPIError, match="empty response"),
    ):
        client.generate(
            model="gemini-2.5-flash",
            contents="prompt",
            response_schema=_Schema,
        )


# ---------------------------------------------------------------------------
# generate() — schema mismatch → GeminiAPIError (not retried)
# ---------------------------------------------------------------------------


def test_generate_schema_mismatch_raises_gemini_api_error():
    # '{"value": 999}' — Pydantic v2 coerces int→str, so use a structurally
    # invalid payload (array instead of object) that always fails validation
    # regardless of field defaults.
    client = _make_client()
    with (
        patch.object(
            client._client.models,
            "generate_content",
            return_value=_mock_response("[1, 2, 3]"),
        ),
        pytest.raises(GeminiAPIError, match="does not match expected schema"),
    ):
        client.generate(
            model="gemini-2.5-flash",
            contents="prompt",
            response_schema=_Schema,
        )


# ---------------------------------------------------------------------------
# generate() — permanent API error → GeminiAPIError
# ---------------------------------------------------------------------------


def test_generate_permanent_api_error_raises():
    client = _make_client()
    with (
        patch.object(
            client._client.models,
            "generate_content",
            side_effect=Exception("400 bad request"),
        ),
        pytest.raises(GeminiAPIError, match="Permanent Gemini error"),
    ):
        client.generate(
            model="gemini-2.5-flash",
            contents="prompt",
            response_schema=_Schema,
        )


# ---------------------------------------------------------------------------
# generate() — transient error re-raised as-is
# ---------------------------------------------------------------------------


def test_generate_transient_error_reraises():
    client = _make_client()
    with (
        patch.object(
            client._client.models,
            "generate_content",
            side_effect=ConnectionError("network timeout"),
        ),
        pytest.raises(ConnectionError),
    ):
        client.generate(
            model="gemini-2.5-flash",
            contents="prompt",
            response_schema=_Schema,
        )


# ---------------------------------------------------------------------------
# close() — idempotent, no error when no httpx_client
# ---------------------------------------------------------------------------


def test_close_without_proxy_is_noop():
    client = _make_client()
    client.close()  # must not raise
    client.close()  # idempotent
