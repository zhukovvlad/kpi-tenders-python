from typing import Any

from app.celery_app import celery_app
from app.workers.base import run_document_task


def _handle(file_bytes: bytes, storage_path: str) -> dict[str, Any]:
    # TODO: DOCX/XLSX → Markdown (see docs/PROMPT.md §Модули/convert)
    raise NotImplementedError("convert module is not implemented yet")


@celery_app.task(
    bind=True,
    name="app.workers.convert.convert_task",
    max_retries=3,
    default_retry_delay=30,
)
def convert_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
