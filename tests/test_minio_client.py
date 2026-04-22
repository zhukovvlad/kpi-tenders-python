import pytest

from app.storage.minio_client import MinIOError, ObjectLocation, parse_storage_path


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
