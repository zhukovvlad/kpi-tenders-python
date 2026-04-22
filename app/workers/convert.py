import os
from typing import Any

from app.celery_app import celery_app
from app.parsers.docx_parser import count_h1_h2, docx_to_markdown
from app.parsers.xlsx_parser import xlsx_to_markdown_sections
from app.storage.minio_client import MinIOClient, parse_storage_path
from app.workers.base import run_document_task


def _md_object_name(storage_path: str, default_bucket: str) -> str:
    """Derive the upload object_name (bucket-relative) from the source storage_path.

    Examples::

        "tenders/docs/uuid.docx"  ->  "docs/uuid.md"
        "docs/uuid.docx"          ->  "docs/uuid.md"   (not the bucket)
    """
    loc = parse_storage_path(storage_path, default_bucket)
    base, _ = os.path.splitext(loc.object_name)
    return base + ".md"


def _handle(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    ext = os.path.splitext(storage_path)[1].lower()
    if ext == ".docx":
        return _handle_docx(file_bytes, storage_path, minio)
    if ext == ".xlsx":
        return _handle_xlsx(file_bytes, storage_path, minio)
    raise ValueError(f"Unsupported file extension: {ext!r} (path: {storage_path!r})")


def _handle_docx(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    content = docx_to_markdown(file_bytes)
    if not content:
        raise ValueError("empty DOCX document: no content extracted")
    md_object = _md_object_name(storage_path, minio.default_bucket)
    md_storage_path = minio.upload(md_object, content.encode("utf-8"))
    return {
        "format": "markdown",
        "md_storage_path": md_storage_path,
        "char_count": len(content),
        "section_count": count_h1_h2(content),
    }


def _handle_xlsx(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    sections = xlsx_to_markdown_sections(file_bytes)
    if not sections:
        raise ValueError("empty XLSX document: no sheets with content")
    parts = [f"## Лист: {name}\n\n{table}" for name, table in sections]
    content = "\n\n".join(parts)
    md_object = _md_object_name(storage_path, minio.default_bucket)
    md_storage_path = minio.upload(md_object, content.encode("utf-8"))
    return {
        "format": "markdown",
        "md_storage_path": md_storage_path,
        "char_count": len(content),
        "sheet_count": len(sections),
    }


@celery_app.task(
    bind=True,
    name="app.workers.convert.convert_task",
    max_retries=3,
    default_retry_delay=30,
)
def convert_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
