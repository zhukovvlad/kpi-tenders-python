from unittest.mock import MagicMock, patch

import pytest

from app.storage.minio_client import MinIOClient, MinIOError, ObjectLocation, parse_storage_path


def test_strips_matching_bucket_prefix():
    loc = parse_storage_path("tenders/docs/file.docx", "tenders")
    assert loc == ObjectLocation(bucket="tenders", object_name="docs/file.docx")


def test_keeps_non_matching_prefix_as_object_name():
    loc = parse_storage_path("otherbucket/docs/file.docx", "tenders")
    assert loc == ObjectLocation(bucket="tenders", object_name="otherbucket/docs/file.docx")


def test_strips_leading_slash_then_matches_bucket():
    loc = parse_storage_path("/tenders/docs/file.docx", "tenders")
    assert loc == ObjectLocation(bucket="tenders", object_name="docs/file.docx")


def test_strips_leading_slash_non_matching():
    loc = parse_storage_path("/other/docs/file.docx", "tenders")
    assert loc == ObjectLocation(bucket="tenders", object_name="other/docs/file.docx")


def test_raises_on_path_with_no_slash():
    with pytest.raises(MinIOError, match="invalid storage_path"):
        parse_storage_path("noslash", "tenders")


def test_raises_on_empty_path():
    with pytest.raises(MinIOError, match="invalid storage_path"):
        parse_storage_path("", "tenders")


def test_raises_on_slash_only():
    with pytest.raises(MinIOError, match="invalid storage_path"):
        parse_storage_path("/", "tenders")


def test_nested_path_under_matching_bucket():
    loc = parse_storage_path("tenders/a/b/c/file.xlsx", "tenders")
    assert loc == ObjectLocation(bucket="tenders", object_name="a/b/c/file.xlsx")


# ---------------------------------------------------------------------------
# MinIOClient.upload() — mocked Minio + settings
# ---------------------------------------------------------------------------

_FAKE_SETTINGS = dict(
    minio_endpoint="localhost:9000",
    minio_access_key="user",
    minio_secret_key="secret",
    minio_use_ssl=False,
    minio_bucket="tenders",
)


def _make_client(mock_minio_cls: MagicMock) -> MinIOClient:
    """Construct a MinIOClient using already-patched Minio class."""
    with patch("app.storage.minio_client.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(**_FAKE_SETTINGS)
        return MinIOClient()


class TestUpload:
    def test_returns_storage_path(self):
        with patch("app.storage.minio_client.Minio") as MockMinio:
            client = _make_client(MockMinio)
            result = client.upload("docs/file.md", b"# Hello")
        assert result == "tenders/docs/file.md"

    def test_put_object_called_with_correct_args(self):
        with patch("app.storage.minio_client.Minio") as MockMinio:
            client = _make_client(MockMinio)
            client.upload("docs/file.md", b"data")
        call_kwargs = MockMinio.return_value.put_object.call_args
        assert call_kwargs.kwargs["bucket_name"] == "tenders"
        assert call_kwargs.kwargs["object_name"] == "docs/file.md"
        assert call_kwargs.kwargs["length"] == 4

    def test_raises_minio_error_on_s3_failure(self):
        from minio.error import S3Error

        with patch("app.storage.minio_client.Minio") as MockMinio:
            client = _make_client(MockMinio)
            MockMinio.return_value.put_object.side_effect = S3Error(
                "NoSuchBucket", "bucket not found", "resource", "req_id", "host_id", None
            )
            with pytest.raises(MinIOError, match="minio put_object failed"):
                client.upload("docs/file.md", b"data")
