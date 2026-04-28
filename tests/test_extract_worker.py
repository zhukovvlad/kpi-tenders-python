"""Unit tests for the extract worker.

Tests cover:
1. extract_llm.extract_values — pure LLM function (Gemini mocked)
2. extract_llm._build_extraction_model — dynamic Pydantic model builder
3. workers/extract._handle — handler receiving bytes + schema
4. workers/extract.extract_task — kwarg validation
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from app.llm.extract_llm import _build_extraction_model, extract_values
from app.storage.minio_client import MinIOClient
from app.workers.extract import _handle, extract_task

# ─── Helpers ─────────────────────────────────────────────────────────────────

_TASK_ID = "task-uuid-002"
_DOC_ID = "doc-uuid-002"
_STORAGE_PATH = "tenders/docs/contract_anonymized.md"
_MD_DOC_ID = "md-doc-uuid-002"

_SCHEMA = [
    {"key_name": "total_square", "data_type": "number"},
    {"key_name": "advance_payment", "data_type": "number"},
    {"key_name": "deadline_date", "data_type": "date"},
]

_DOCUMENT_TEXT = """\
# Договор подряда

Общая площадь объекта: 5 400 м².
Авансовый платёж: 30% от суммы договора.
Дата окончания работ: 31.12.2025.
Заказчик: <ORGANIZATION_1>.
"""


def _make_pydantic_result(data: dict[str, Any]) -> BaseModel:
    """Create a Pydantic model instance matching the extraction schema."""
    model_cls = _build_extraction_model(_SCHEMA)
    return model_cls(**data)


def _make_gemini_client(return_value: Any) -> MagicMock:
    client = MagicMock()
    client.generate.return_value = return_value
    return client


def _make_minio() -> MagicMock:
    mock = MagicMock(spec=MinIOClient)
    mock.default_bucket = "tenders"
    return mock


# ═══════════════════════════════════════════════════════════════════════════
# _build_extraction_model
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildExtractionModel:
    def test_creates_model_with_correct_fields(self):
        schema = [{"key_name": "total_square", "data_type": "number"}]
        model_cls = _build_extraction_model(schema)
        assert hasattr(model_cls, "model_fields")
        assert "total_square" in model_cls.model_fields

    def test_all_fields_optional_with_none_default(self):
        model_cls = _build_extraction_model(_SCHEMA)
        instance = model_cls()
        assert instance.total_square is None
        assert instance.advance_payment is None
        assert instance.deadline_date is None

    def test_fields_accept_string_values(self):
        model_cls = _build_extraction_model(_SCHEMA)
        instance = model_cls(total_square="5 400 м²", deadline_date="31.12.2025")
        assert instance.total_square == "5 400 м²"
        assert instance.deadline_date == "31.12.2025"

    def test_empty_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="extraction_schema"):
            _build_extraction_model([])

    def test_unknown_data_type_defaults_to_optional_str(self):
        schema = [{"key_name": "weird_field", "data_type": "unknown_type"}]
        model_cls = _build_extraction_model(schema)
        instance = model_cls(weird_field="value")
        assert instance.weird_field == "value"

    def test_non_dict_entry_raises_value_error(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _build_extraction_model(["not_a_dict"])

    def test_missing_key_name_raises_value_error(self):
        with pytest.raises(ValueError, match="key_name"):
            _build_extraction_model([{"data_type": "string"}])

    def test_empty_key_name_raises_value_error(self):
        with pytest.raises(ValueError, match="key_name"):
            _build_extraction_model([{"key_name": "", "data_type": "string"}])

    def test_invalid_identifier_key_name_raises_value_error(self):
        with pytest.raises(ValueError, match="not a valid Python identifier"):
            _build_extraction_model([{"key_name": "total-square", "data_type": "string"}])

    def test_digit_leading_key_name_raises_value_error(self):
        with pytest.raises(ValueError, match="not a valid Python identifier"):
            _build_extraction_model([{"key_name": "2bad", "data_type": "string"}])


# ═══════════════════════════════════════════════════════════════════════════
# extract_llm.extract_values — pure function
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractValues:
    def test_returns_flat_dict_with_extracted_values(self):
        pydantic_result = _make_pydantic_result(
            {
                "total_square": "5 400 м²",
                "advance_payment": None,
                "deadline_date": "31.12.2025",
            }
        )
        client = _make_gemini_client(pydantic_result)

        with patch("app.llm.extract_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            result = extract_values(client, document_text=_DOCUMENT_TEXT, extraction_schema=_SCHEMA)

        assert result["total_square"] == "5 400 м²"
        assert result["advance_payment"] is None
        assert result["deadline_date"] == "31.12.2025"

    def test_null_values_are_none_in_result(self):
        pydantic_result = _make_pydantic_result(
            {
                "total_square": None,
                "advance_payment": None,
                "deadline_date": None,
            }
        )
        client = _make_gemini_client(pydantic_result)

        with patch("app.llm.extract_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            result = extract_values(client, document_text=_DOCUMENT_TEXT, extraction_schema=_SCHEMA)

        assert all(v is None for v in result.values())

    def test_all_schema_keys_present_in_result(self):
        pydantic_result = _make_pydantic_result(
            {
                "total_square": "5400",
                "advance_payment": "30%",
                "deadline_date": "31.12.2025",
            }
        )
        client = _make_gemini_client(pydantic_result)

        with patch("app.llm.extract_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            result = extract_values(client, document_text=_DOCUMENT_TEXT, extraction_schema=_SCHEMA)

        for entry in _SCHEMA:
            assert entry["key_name"] in result

    def test_gemini_called_with_correct_model(self):
        pydantic_result = _make_pydantic_result(
            {"total_square": None, "advance_payment": None, "deadline_date": None}
        )
        client = _make_gemini_client(pydantic_result)

        with patch("app.llm.extract_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            extract_values(client, document_text=_DOCUMENT_TEXT, extraction_schema=_SCHEMA)

        call_kwargs = client.generate.call_args[1]
        assert call_kwargs["model"] == "gemini-2.5-pro"

    def test_empty_document_text_raises_value_error(self):
        client = _make_gemini_client(None)
        with pytest.raises(ValueError, match="document_text"):
            extract_values(client, document_text="   ", extraction_schema=_SCHEMA)

    def test_empty_schema_raises_value_error(self):
        client = _make_gemini_client(None)
        with pytest.raises(ValueError, match="extraction_schema"):
            extract_values(client, document_text=_DOCUMENT_TEXT, extraction_schema=[])

    def test_anonymised_tags_preserved_in_prompt(self):
        pydantic_result = _make_pydantic_result(
            {"total_square": None, "advance_payment": None, "deadline_date": None}
        )
        client = _make_gemini_client(pydantic_result)

        doc_with_tags = "Заказчик: <ORGANIZATION_1>. Сумма: 1 000 руб."

        with patch("app.llm.extract_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            extract_values(client, document_text=doc_with_tags, extraction_schema=_SCHEMA[:1])

        prompt_arg = client.generate.call_args[1]["contents"]
        assert "<ORGANIZATION_1>" in prompt_arg


# ═══════════════════════════════════════════════════════════════════════════
# workers/extract._handle — handler
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractHandle:
    def test_decodes_bytes_and_returns_extraction_result(self):
        pydantic_result = _make_pydantic_result(
            {"total_square": "5400", "advance_payment": None, "deadline_date": None}
        )
        client = _make_gemini_client(pydantic_result)
        minio = _make_minio()

        with (
            patch("app.workers.extract.get_client", return_value=client),
            patch("app.llm.extract_llm.get_settings") as mock_settings,
        ):
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            result = _handle(
                _DOCUMENT_TEXT.encode("utf-8"),
                _STORAGE_PATH,
                minio,
                _SCHEMA,
            )

        assert result["total_square"] == "5400"
        assert result["advance_payment"] is None

    def test_utf8_document_decoded_correctly(self):
        pydantic_result = _make_pydantic_result(
            {"total_square": None, "advance_payment": None, "deadline_date": None}
        )
        client = _make_gemini_client(pydantic_result)
        minio = _make_minio()

        russian_text = "Площадь: 5 400 м²."

        with (
            patch("app.workers.extract.get_client", return_value=client),
            patch("app.llm.extract_llm.get_settings") as mock_settings,
        ):
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            _handle(russian_text.encode("utf-8"), _STORAGE_PATH, minio, _SCHEMA)

        prompt_arg = client.generate.call_args[1]["contents"]
        assert "5 400 м²" in prompt_arg


# ═══════════════════════════════════════════════════════════════════════════
# workers/extract.extract_task — kwarg validation
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractTaskValidation:
    """Tests for kwarg validation logic inside extract_task entry-point.

    ``.run()`` is already bound (Celery bind=True), no explicit self needed.
    Empty-schema validation now lives inside ``_handle`` (so ``run_document_task``
    can report ``failed`` to Go); it is tested via ``_handle`` directly.
    """

    def test_missing_extraction_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="extraction_schema"):
            _handle(b"text", _STORAGE_PATH, MagicMock(spec=MinIOClient), [])

    def test_valid_kwargs_calls_run_document_task(self):
        with patch("app.workers.extract.run_document_task", return_value={"k": "v"}) as mock_run:
            result = extract_task.run(
                _TASK_ID,
                _DOC_ID,
                _STORAGE_PATH,
                extraction_schema=_SCHEMA,
                md_document_id=_MD_DOC_ID,
            )

        assert mock_run.call_count == 1
        call_args = mock_run.call_args[0]
        assert call_args[1] == _TASK_ID
        assert call_args[2] == _DOC_ID
        assert call_args[3] == _STORAGE_PATH
        assert callable(call_args[4])  # the bound handler
        assert result == {"k": "v"}

    def test_bound_handler_passes_schema_to_handle(self):
        captured: dict = {}

        def fake_run(task, task_id, doc_id, sp, handler):
            captured["result"] = handler(b"text", sp, MagicMock(spec=MinIOClient))
            return captured["result"]

        pydantic_result = _make_pydantic_result(
            {"total_square": "42", "advance_payment": None, "deadline_date": None}
        )
        mock_client = _make_gemini_client(pydantic_result)

        with (
            patch("app.workers.extract.run_document_task", side_effect=fake_run),
            patch("app.workers.extract.get_client", return_value=mock_client),
            patch("app.llm.extract_llm.get_settings") as mock_settings,
        ):
            mock_settings.return_value.gemini_heavy_model = "gemini-2.5-pro"
            extract_task.run(
                _TASK_ID,
                _DOC_ID,
                _STORAGE_PATH,
                extraction_schema=_SCHEMA,
                md_document_id=_MD_DOC_ID,
            )

        assert "total_square" in captured.get("result", {})
