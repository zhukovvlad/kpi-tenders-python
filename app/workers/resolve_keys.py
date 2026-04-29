"""Celery task: resolve_keys — semantic key mapping via Gemini Flash.

This task does NOT download any file from MinIO. It only:
1. Validates ``raw_questions`` and ``existing_keys`` kwargs (fail-fast before any Go call).
2. Marks the task as ``processing`` in Go.
3. Calls Gemini Flash to map user questions → extraction keys.
4. Reports ``completed`` with the resolved schema back to Go.

Go then chains to the ``extract`` task using the ``resolved_schema``
from this task's ``result_payload``.

Kwargs contract (set by ``service_extraction.go``):
    raw_questions  : list[str]  — new user questions to map
    existing_keys  : list[dict] — existing DB keys {"key_name","source_query","data_type"}
"""

import logging
from typing import Any

from celery import Task

from app.celery_app import celery_app
from app.go_client.client import GoClient, GoClientError
from app.llm.gemini_client import GeminiAPIError, get_client
from app.llm.resolve_keys_llm import resolve_keys

log = logging.getLogger(__name__)

# Permanent failures — retrying will not help.
_NO_RETRY = (GoClientError, GeminiAPIError, NotImplementedError, ValueError)


def _run_resolve_keys(
    task: Task,
    task_id: str,
    raw_questions: list[str],
    existing_keys: list[dict[str, str]],
) -> dict[str, Any]:
    """Lifecycle for resolve_keys: mark processing → call LLM → report result."""
    with GoClient() as go:
        try:
            if not isinstance(raw_questions, list) or not raw_questions:
                raise ValueError(
                    f"resolve_keys_task: raw_questions must be a non-empty list for task_id={task_id!r}"
                )
            if not isinstance(existing_keys, list):
                raise ValueError(
                    f"resolve_keys_task: existing_keys must be a list for task_id={task_id!r}"
                )

            go.update_task(
                task_id=task_id,
                status="processing",
                celery_task_id=task.request.id,
            )

            client = get_client()
            try:
                result = resolve_keys(
                    client,
                    raw_questions=raw_questions,
                    existing_keys=existing_keys,
                )
            finally:
                client.close()

            go.update_task(
                task_id=task_id,
                status="completed",
                result_payload=result,
            )
            return result

        except Exception as exc:
            log.exception("resolve_keys task %s failed", task_id)
            try:
                go.update_task(
                    task_id=task_id,
                    status="failed",
                    error_message=str(exc),
                )
            except Exception:
                log.exception("resolve_keys: failed to report failure to Go for task %s", task_id)

            if isinstance(exc, _NO_RETRY):
                raise
            raise task.retry(exc=exc) from None


@celery_app.task(
    bind=True,
    name="app.workers.resolve_keys.resolve_keys_task",
    max_retries=3,
    default_retry_delay=30,
)
def resolve_keys_task(
    self,
    task_id: str,
    _document_id: str,
    _storage_path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Celery entry-point for the resolve_keys module.

    Positional args (from Celery v2 message):
        task_id      : Go ``document_tasks.id`` UUID string
        _document_id : Go ``documents.id`` — not used (no file download)
        _storage_path: Original document path in MinIO — not used (no file download)

    Keyword args (from ``Kwargs`` in the Go Celery message):
        raw_questions  : list[str]
        existing_keys  : list[dict]
    """
    raw_questions: list[str] = kwargs.get("raw_questions") or []
    existing_keys: list[dict] = kwargs.get("existing_keys", [])

    return _run_resolve_keys(
        self,
        task_id=task_id,
        raw_questions=raw_questions,
        existing_keys=existing_keys,
    )
