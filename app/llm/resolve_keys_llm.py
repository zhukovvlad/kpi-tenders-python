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
import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from app.config import get_settings
from app.llm.gemini_client import GeminiClient

log = logging.getLogger(__name__)

# ── Response schema for Gemini ────────────────────────────────────────────────


_KEY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_REQUIRED_EXISTING_KEY_FIELDS = frozenset({"key_name", "source_query", "data_type"})
_ALLOWED_EXISTING_DATA_TYPES = frozenset({"string", "number", "date"})


class _ResolvedKeyItem(BaseModel):
    """Single resolved key returned by the LLM."""

    key_name: str
    source_query: str
    data_type: Literal["string", "number", "date"]
    is_new: bool

    @field_validator("key_name")
    @classmethod
    def _validate_key_name(cls, v: str) -> str:
        if not _KEY_NAME_RE.match(v):
            raise ValueError(
                f"key_name {v!r} must match ^[a-z][a-z0-9_]*$ "
                "(lowercase snake_case, no leading digits or special chars)"
            )
        return v


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
    existing_section = json.dumps(existing_keys, ensure_ascii=False, indent=2)
    existing_keys_note = (
        "\nNote: there are no existing keys, so all questions should be treated as new.\n"
        if not existing_keys
        else ""
    )
    questions_section = json.dumps(raw_questions, ensure_ascii=False, indent=2)
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"EXISTING KEYS:\n{existing_section}{existing_keys_note}\n"
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
    if not isinstance(raw_questions, list):
        raise ValueError("raw_questions must be a list, got " + type(raw_questions).__name__)
    if not raw_questions:
        raise ValueError("raw_questions must not be empty")
    if not all(isinstance(q, str) and q.strip() for q in raw_questions):
        raise ValueError("raw_questions must be a list of non-blank strings")
    if not isinstance(existing_keys, list):
        raise ValueError("existing_keys must be a list, got " + type(existing_keys).__name__)
    for index, k in enumerate(existing_keys):
        if not isinstance(k, dict):
            raise ValueError(f"existing_keys[{index}] must be a dict, got {type(k).__name__}")
        if not all(isinstance(field, str) for field in k):
            raise ValueError(f"existing_keys[{index}] must have only string keys")
        missing = _REQUIRED_EXISTING_KEY_FIELDS - set(k)
        if missing:
            raise ValueError(
                f"existing_keys[{index}] is missing required field(s): {', '.join(sorted(missing))}"
            )
        for field in _REQUIRED_EXISTING_KEY_FIELDS:
            if not isinstance(k[field], str):
                raise ValueError(
                    f"existing_keys[{index}][{field!r}] must be a string, "
                    f"got {type(k[field]).__name__}"
                )
        if k["data_type"] not in _ALLOWED_EXISTING_DATA_TYPES:
            raise ValueError(
                f"existing_keys[{index}]['data_type'] must be one of "
                f"{', '.join(sorted(_ALLOWED_EXISTING_DATA_TYPES))}, got {k['data_type']!r}"
            )

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

    existing_keys_by_name: dict[str, dict[str, str]] = {k["key_name"]: k for k in existing_keys}

    new_keys: list[dict[str, str]] = []
    resolved_schema: list[dict[str, str]] = []
    seen_resolved: set[str] = set()
    seen_new: set[str] = set()
    skipped_resolved = 0
    skipped_new = 0

    for item in llm_response.keys:
        if item.key_name not in seen_resolved:
            # Prefer existing data_type over LLM-provided to prevent drift
            data_type = (
                existing_keys_by_name[item.key_name]["data_type"]
                if item.key_name in existing_keys_by_name
                else item.data_type
            )
            resolved_schema.append({"key_name": item.key_name, "data_type": data_type})
            seen_resolved.add(item.key_name)
        else:
            skipped_resolved += 1
        if item.is_new:
            if item.key_name not in seen_new:
                new_keys.append(
                    {
                        "key_name": item.key_name,
                        "source_query": item.source_query,
                        "data_type": item.data_type,
                    }
                )
                seen_new.add(item.key_name)
            else:
                skipped_new += 1

    log.info(
        "resolve_keys: resolved %d unique key(s), %d new; skipped %d duplicate resolved, %d duplicate new",
        len(resolved_schema),
        len(new_keys),
        skipped_resolved,
        skipped_new,
    )

    return {
        "new_keys": new_keys,
        "resolved_schema": resolved_schema,
    }
