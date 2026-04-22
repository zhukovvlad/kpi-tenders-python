import logging
from collections.abc import Callable
from typing import Any

from celery import Task

from app.go_client.client import GoClient, GoClientError
from app.storage.minio_client import MinIOClient

log = logging.getLogger(__name__)

# Permanent failures: retrying will not help, propagate immediately.
_NO_RETRY = (GoClientError, NotImplementedError)


def run_document_task(
    task: Task,
    task_id: str,
    document_id: str,
    storage_path: str,
    handler: Callable[[bytes, str, MinIOClient], dict[str, Any]],
) -> dict[str, Any]:
    """
    Shared lifecycle for all module tasks:

    1. Mark task as `processing` in Go.
    2. Download the file from MinIO.
    3. Run the module-specific `handler(file_bytes, storage_path, minio) -> result_payload`.
    4. Mark task as `completed` with the result, or `failed` on error.

    `minio` is passed to the handler so modules that produce output files
    (e.g. `convert`) can upload them to the same MinIO instance.
    GoClientError and NotImplementedError are not retried (permanent failures).
    """
    minio = MinIOClient()

    with GoClient() as go:
        try:
            go.update_task(
                task_id=task_id,
                status="processing",
                celery_task_id=task.request.id,
            )

            file_bytes = minio.download(storage_path)
            result = handler(file_bytes, storage_path, minio)

            go.update_task(
                task_id=task_id,
                status="completed",
                result_payload=result,
            )
            return result

        except Exception as exc:
            log.exception(
                "task %s (document %s) failed at %s",
                task_id,
                document_id,
                storage_path,
            )
            try:
                go.update_task(
                    task_id=task_id,
                    status="failed",
                    error_message=str(exc),
                )
            except Exception:
                log.exception("failed to report failure to Go for task %s", task_id)

            if isinstance(exc, _NO_RETRY):
                raise
            raise task.retry(exc=exc) from None
