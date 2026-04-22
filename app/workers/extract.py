from typing import Any

from app.celery_app import celery_app
from app.storage.minio_client import MinIOClient
from app.workers.base import run_document_task


def _handle(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    # TODO: two-stage Gemini Flash (keys) + Gemini Pro (values) extraction.
    # Queries (JSONB) must be passed alongside the file — see docs/PROMPT.md §Модули/extract.
    raise NotImplementedError("extract module is not implemented yet")


@celery_app.task(
    bind=True,
    name="app.workers.extract.extract_task",
    max_retries=3,
    default_retry_delay=30,
)
def extract_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
