"""Celery task: extract — data extraction from anonymised Markdown via Gemini Pro.

Kwargs contract (set by ``service_worker.go::triggerExtract``):
    extraction_schema : list[dict]  — [{"key_name": str, "data_type": str}, ...]

The ``storage_path`` positional arg points to the anonymised Markdown file in
MinIO (Go sets ``input_storage_path = mdDoc.StoragePath``). The extract worker
downloads this file and sends its text to Gemini Pro for extraction.

Result payload: flat ``{key_name: str | None}`` dict (JSON ``null`` for missing values).
Go ``handleExtractCompleted`` persists non-null values into
``document_extracted_data``.
"""

import logging
from typing import Any

from app.celery_app import celery_app
from app.llm.extract_llm import extract_values
from app.llm.gemini_client import get_client
from app.storage.minio_client import MinIOClient
from app.workers.base import run_document_task

log = logging.getLogger(__name__)


def _handle(
    file_bytes: bytes,
    storage_path: str,
    minio: MinIOClient,
    extraction_schema: list[dict[str, str]],
) -> dict[str, Any]:
    """Module-specific handler: decode bytes → call Gemini Pro → return flat dict."""
    if not extraction_schema:
        raise ValueError(
            f"extract_task: extraction_schema is empty (storage_path={storage_path!r}). "
            "Go must pass it as a kwarg."
        )
    document_text = file_bytes.decode("utf-8")
    client = get_client()
    return extract_values(client, document_text=document_text, extraction_schema=extraction_schema)


@celery_app.task(
    bind=True,
    name="app.workers.extract.extract_task",
    max_retries=3,
    default_retry_delay=30,
)
def extract_task(
    self,
    task_id: str,
    document_id: str,
    storage_path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Celery entry-point for the extract module.

    Positional args:
        task_id      : Go ``document_tasks.id``
        document_id  : Go ``documents.id`` (original document)
        storage_path : Storage path of the anonymised Markdown in MinIO

    Keyword args:
        extraction_schema : list[dict]  — keys to extract
    """
    extraction_schema: list[dict] = kwargs.get("extraction_schema") or []

    def _bound_handle(file_bytes: bytes, sp: str, minio: MinIOClient) -> dict[str, Any]:
        return _handle(file_bytes, sp, minio, extraction_schema)

    return run_document_task(self, task_id, document_id, storage_path, _bound_handle)
