from typing import Any

from app.celery_app import celery_app
from app.storage.minio_client import MinIOClient
from app.workers.base import run_document_task


def _handle(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    # TODO: Natasha + Presidio NER pipeline (see docs/PROMPT.md §Модули/anonymize)
    raise NotImplementedError("anonymize module is not implemented yet")


@celery_app.task(
    bind=True,
    name="app.workers.anonymize.anonymize_task",
    max_retries=3,
    default_retry_delay=30,
)
def anonymize_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
