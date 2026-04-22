from typing import Any

from app.celery_app import celery_app
from app.workers.base import run_document_task


def _handle(file_bytes: bytes, storage_path: str) -> dict[str, Any]:
    # TODO: XLSX (openpyxl) / PDF (Gemini) invoice parsing — see docs/PROMPT.md §Модули/parse_invoice.
    raise NotImplementedError("parse_invoice module is not implemented yet")


@celery_app.task(
    bind=True,
    name="app.workers.parse_invoice.parse_invoice_task",
    max_retries=3,
    default_retry_delay=30,
)
def parse_invoice_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
