from celery.result import AsyncResult

from app.workers.anonymize import anonymize_task
from app.workers.convert import convert_task
from app.workers.extract import extract_task
from app.workers.parse_invoice import parse_invoice_task

MODULE_TASKS = {
    "convert": convert_task,
    "anonymize": anonymize_task,
    "extract": extract_task,
    "parse_invoice": parse_invoice_task,
}


def dispatch(
    task_id: str,
    document_id: str,
    module_name: str,
    storage_path: str,
) -> AsyncResult:
    task_fn = MODULE_TASKS.get(module_name)
    if task_fn is None:
        raise ValueError(f"Unknown module: {module_name}")
    return task_fn.delay(task_id, document_id, storage_path)
