"""Unit tests for app/workers/anonymize._handle().

The Natasha pipeline is mocked so no model loading occurs.
MinIOClient is replaced with a MagicMock.
"""

import json
from unittest.mock import MagicMock, call, patch

import pytest

from app.storage.minio_client import MinIOClient, MinIOError
from app.workers.anonymize import _handle


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_minio(
    upload_paths: list[str] | None = None,
    bucket: str = "tenders",
) -> MagicMock:
    mock = MagicMock(spec=MinIOClient)
    mock.default_bucket = bucket
    mock.upload.side_effect = upload_paths or [
        f"{bucket}/docs/uuid_anonymized.md",
        f"{bucket}/docs/uuid_entities.json",
    ]
    return mock


def _md_bytes(text: str = "# Документ\n\nТекст") -> bytes:
    return text.encode("utf-8")


_ANON_RESULT = ("<PERSON_1> подписал договор.", {"<PERSON_1>": "Иванов И.И."})
_STORAGE_PATH = "tenders/docs/uuid.md"


# ═══════════════════════════════════════════════════════════════════════════
# Result payload shape
# ═══════════════════════════════════════════════════════════════════════════


class TestHandlePayload:
    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_returns_anonymized_storage_path(self, _mock):
        minio = _make_minio()
        result = _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert "anonymized_storage_path" in result

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_returns_entities_map_storage_path(self, _mock):
        minio = _make_minio()
        result = _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert "entities_map_storage_path" in result

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_returns_entity_count(self, _mock):
        minio = _make_minio()
        result = _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert result["entity_count"] == 1

    @patch("app.workers.anonymize.anonymize", return_value=("clean text", {}))
    def test_entity_count_zero_when_no_pii(self, _mock):
        minio = _make_minio()
        result = _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert result["entity_count"] == 0

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_storage_paths_from_minio(self, _mock):
        paths = ["tenders/docs/uuid_anonymized.md", "tenders/docs/uuid_entities.json"]
        minio = _make_minio(upload_paths=paths)
        result = _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert result["anonymized_storage_path"] == paths[0]
        assert result["entities_map_storage_path"] == paths[1]


# ═══════════════════════════════════════════════════════════════════════════
# MinIO upload calls
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleUploads:
    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_upload_called_twice(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        assert minio.upload.call_count == 2

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_first_upload_is_markdown(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        first_call_object = minio.upload.call_args_list[0][0][0]
        assert first_call_object.endswith(".md")

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_second_upload_is_json(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        second_call_object = minio.upload.call_args_list[1][0][0]
        assert second_call_object.endswith(".json")

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_json_upload_uses_json_content_type(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        json_call_kwargs = minio.upload.call_args_list[1][1]
        assert "json" in json_call_kwargs.get("content_type", "").lower()

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_anonymized_text_encoded_correctly(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        md_bytes = minio.upload.call_args_list[0][0][1]
        assert md_bytes == _ANON_RESULT[0].encode("utf-8")

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_entities_json_is_valid_json(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), _STORAGE_PATH, minio)
        json_bytes = minio.upload.call_args_list[1][0][1]
        parsed = json.loads(json_bytes)
        assert parsed == {"<PERSON_1>": "Иванов И.И."}


# ═══════════════════════════════════════════════════════════════════════════
# Object name derivation
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleObjectNames:
    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_object_name_strips_bucket_prefix(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), "tenders/docs/uuid.md", minio)
        md_obj = minio.upload.call_args_list[0][0][0]
        assert not md_obj.startswith("tenders/")

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_object_name_contains_anonymized_suffix(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), "tenders/docs/uuid.md", minio)
        md_obj = minio.upload.call_args_list[0][0][0]
        assert "anonymized" in md_obj

    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_object_name_preserves_directory(self, _mock):
        minio = _make_minio()
        _handle(_md_bytes(), "tenders/docs/subdir/uuid.md", minio)
        md_obj = minio.upload.call_args_list[0][0][0]
        assert "subdir" in md_obj


# ═══════════════════════════════════════════════════════════════════════════
# Input decoding
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleInputDecoding:
    @patch("app.workers.anonymize.anonymize")
    def test_passes_decoded_text_to_anonymize(self, mock_anon):
        mock_anon.return_value = ("text", {})
        minio = _make_minio()
        input_text = "# Документ\n\nИванов И.И."
        _handle(input_text.encode("utf-8"), _STORAGE_PATH, minio)
        mock_anon.assert_called_once_with(input_text)

    @patch("app.workers.anonymize.anonymize")
    def test_handles_cyrillic_content(self, mock_anon):
        mock_anon.return_value = ("", {})
        minio = _make_minio()
        text = "Текст на кириллице с символами: ёЁ"
        _handle(text.encode("utf-8"), _STORAGE_PATH, minio)
        mock_anon.assert_called_once_with(text)


# ═══════════════════════════════════════════════════════════════════════════
# Error propagation
# ═══════════════════════════════════════════════════════════════════════════


class TestHandleErrors:
    @patch("app.workers.anonymize.anonymize", return_value=_ANON_RESULT)
    def test_minio_error_propagates(self, _mock):
        minio = _make_minio()
        minio.upload.side_effect = MinIOError("S3 error")
        with pytest.raises(MinIOError):
            _handle(_md_bytes(), _STORAGE_PATH, minio)
