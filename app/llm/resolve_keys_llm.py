"""LLM logic for the resolve_keys worker.

Responsibilities
----------------
* Build the prompt for Gemini Flash from ``raw_questions`` and ``existing_keys``.
* Call Gemini with a strict JSON schema (Structured Outputs).
* Convert the LLM response into the ``result_payload`` dict expected by Go:

  .. code-block:: json

      {
          "new_keys": [{"key_name": "...", "source_query": "...", "data_type": "..."}],
          "resolved_schema": [{"key_name": "...", "data_type": "..."}, ...]
      }
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel

from app.config import get_settings
from app.llm.gemini_client import GeminiClient

log = logging.getLogger(__name__)

# ── Response schema for Gemini ────────────────────────────────────────────────


class _ResolvedKeyItem(BaseModel):
    """Single resolved key returned by the LLM."""

    key_name: str
    source_query: str
    data_type: Literal["string", "number", "date"]
    is_new: bool


class _ResolveKeysResponse(BaseModel):
    """Top-level structured output from Gemini for resolve_keys."""

    keys: list[_ResolvedKeyItem]


# ── Prompt builder ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a semantic router for a tender document analysis system.

Your task:
1. Analyse the list of NEW USER QUESTIONS below.
2. For each question, decide if its meaning overlaps with an EXISTING KEY.
   - If YES → use the existing key_name exactly as-is (is_new = false).
   - If NO  → generate a concise snake_case English key_name (is_new = true)
              and determine its data_type: "string", "number", or "date".
3. Return ONE entry per question. Preserve the original question text in
   source_query.

Rules:
- key_name must be snake_case, lowercase English letters only (no special chars).
- Semantic overlap means the same concept, even if phrased differently.
- If two questions map to the same existing key, return the key twice (once per
  question) with is_new = false.
- For new keys, pick the most concise descriptive name (e.g. "advance_payment",
  "contract_value", "deadline_date").
"""


def _build_prompt(
    raw_questions: list[str],
    existing_keys: list[dict[str, str]],
) -> str:
    existing_section = (
        json.dumps(existing_keys, ensure_ascii=False, indent=2)
        if existing_keys
        else "[]  (no existing keys — all questions are new)"
    )
    questions_section = json.dumps(raw_questions, ensure_ascii=False, indent=2)
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"EXISTING KEYS:\n{existing_section}\n\n"
        f"NEW USER QUESTIONS:\n{questions_section}\n\n"
        "Respond with a JSON object matching the required schema."
    )


# ── Public function ───────────────────────────────────────────────────────────


def resolve_keys(
    client: GeminiClient,
    *,
    raw_questions: list[str],
    existing_keys: list[dict[str, str]],
) -> dict[str, Any]:
    """Map user questions to extraction keys using Gemini Flash.

    Parameters
    ----------
    client:
        Initialised ``GeminiClient``.
    raw_questions:
        New questions from the user (non-empty list).
    existing_keys:
        Existing extraction keys from the Go DB.
        Each entry: ``{"key_name": str, "source_query": str, "data_type": str}``.

    Returns
    -------
    dict
        ``result_payload`` ready for ``GoClient.update_task``.
    """
    if not raw_questions:
        raise ValueError("raw_questions must not be empty")

    settings = get_settings()
    prompt = _build_prompt(raw_questions, existing_keys)

    log.info(
        "resolve_keys: calling Gemini %s with %d question(s), %d existing key(s)",
        settings.gemini_light_model,
        len(raw_questions),
        len(existing_keys),
    )

    llm_response: _ResolveKeysResponse = client.generate(
        model=settings.gemini_light_model,
        contents=prompt,
        response_schema=_ResolveKeysResponse,
    )

    if len(llm_response.keys) != len(raw_questions):
        raise ValueError(
            f"resolve_keys: LLM returned {len(llm_response.keys)} key(s) "
            f"but expected {len(raw_questions)} (one per question). "
            "Gemini may have merged or dropped questions."
        )

    new_keys: list[dict[str, str]] = []
    resolved_schema: list[dict[str, str]] = []

    for item in llm_response.keys:
        resolved_schema.append({"key_name": item.key_name, "data_type": item.data_type})
        if item.is_new:
            new_keys.append(
                {
                    "key_name": item.key_name,
                    "source_query": item.source_query,
                    "data_type": item.data_type,
                }
            )

    log.info(
        "resolve_keys: resolved %d key(s), %d new",
        len(resolved_schema),
        len(new_keys),
    )

    return {
        "new_keys": new_keys,
        "resolved_schema": resolved_schema,
    }
