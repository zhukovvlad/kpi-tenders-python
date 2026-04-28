"""LLM logic for the extract worker.

Responsibilities
----------------
* Dynamically build a Pydantic schema from the ``extraction_schema`` list.
* Build the extraction prompt for Gemini Pro.
* Call Gemini with Structured Outputs to guarantee valid JSON back.
* Return a **flat** ``{key_name: value_or_null}`` dict as the Go result_payload.

Go ``handleExtractCompleted`` expects:

.. code-block:: json

    {"total_square": "5400 м²", "advance_payment": null, "deadline_date": "31.12.2025"}

Values are strings (or null). The LLM must preserve anonymised entity tags
like ``<ORGANIZATION_1>`` verbatim.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, create_model, Field

from app.config import get_settings
from app.llm.gemini_client import GeminiClient

log = logging.getLogger(__name__)

# ── Dynamic schema builder ────────────────────────────────────────────────────

_FIELD_TYPE: dict[str, type] = {
    "string": Optional[str],
    "number": Optional[str],   # keep as string — LLM may return "5 400 м²" etc.
    "date": Optional[str],
}


def _build_extraction_model(
    extraction_schema: list[dict[str, str]],
) -> type[BaseModel]:
    """Dynamically create a Pydantic model from the extraction_schema list.

    All fields default to ``None`` (Gemini returns ``null`` when not found).
    We use ``Optional[str]`` for all types: Go stores everything as text and
    numeric formatting from Russian contracts is too inconsistent for float.
    """
    if not extraction_schema:
        raise ValueError("extraction_schema must not be empty")

    fields: dict[str, Any] = {}
    for entry in extraction_schema:
        key = entry["key_name"]
        data_type = entry.get("data_type", "string")
        py_type = _FIELD_TYPE.get(data_type, Optional[str])
        fields[key] = (py_type, Field(default=None))

    return create_model("ExtractionResult", **fields)


# ── Prompt builder ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a precise data extraction assistant for construction contract documents.

Instructions:
1. Read the DOCUMENT TEXT below (it may be in Russian, partially anonymised).
2. For each KEY in the EXTRACTION SCHEMA, find and return the corresponding value.
3. Return ONLY facts that are explicitly stated in the document.
4. If a value is not found, return null.
5. Preserve anonymised entity tags exactly (e.g. <ORGANIZATION_1>, <PERSON_2>).
6. For monetary amounts, include the unit (e.g. "1 500 000 руб.").
7. For dates, return in the format found in the document.
8. Do not interpret or infer values — extract verbatim or as close as possible.
"""


def _build_prompt(
    document_text: str,
    extraction_schema: list[dict[str, str]],
) -> str:
    schema_lines = "\n".join(
        f"  - {e['key_name']} ({e.get('data_type', 'string')})"
        for e in extraction_schema
    )
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"EXTRACTION SCHEMA:\n{schema_lines}\n\n"
        f"DOCUMENT TEXT:\n{document_text}\n\n"
        "Respond with a JSON object matching the required schema. "
        "Use null for any key not found in the document."
    )


# ── Public function ───────────────────────────────────────────────────────────


def extract_values(
    client: GeminiClient,
    *,
    document_text: str,
    extraction_schema: list[dict[str, str]],
) -> dict[str, Any]:
    """Extract structured values from document text using Gemini Pro.

    Parameters
    ----------
    client:
        Initialised ``GeminiClient``.
    document_text:
        Anonymised Markdown document content.
    extraction_schema:
        List of ``{"key_name": str, "data_type": str}`` dicts
        (passed from resolve_keys via Go).

    Returns
    -------
    dict
        Flat ``{key_name: value_or_null}`` mapping. Null values are strings
        parsed as Python ``None``. Go will skip null values when persisting.
    """
    if not document_text.strip():
        raise ValueError("document_text must not be empty")
    if not extraction_schema:
        raise ValueError("extraction_schema must not be empty")

    settings = get_settings()
    model_cls = _build_extraction_model(extraction_schema)
    prompt = _build_prompt(document_text, extraction_schema)

    log.info(
        "extract: calling Gemini %s to extract %d field(s)",
        settings.gemini_heavy_model,
        len(extraction_schema),
    )

    result_obj = client.generate(
        model=settings.gemini_heavy_model,
        contents=prompt,
        response_schema=model_cls,
    )

    # Convert Pydantic model to flat dict; None stays None (Go skips nulls).
    flat: dict[str, Any] = result_obj.model_dump()

    extracted_count = sum(1 for v in flat.values() if v is not None)
    log.info(
        "extract: extracted %d/%d field(s)",
        extracted_count,
        len(extraction_schema),
    )

    return flat
