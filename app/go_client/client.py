import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.api.schemas import TaskStatus, TaskStatusUpdate
from app.config import get_settings

log = logging.getLogger(__name__)

_RETRYABLE = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)


class GoClientError(Exception):
    """4xx response from Go — permanent failure, do not retry."""


class GoServerError(Exception):
    """5xx response from Go — transient failure, safe to retry."""


class GoClient:
    """HTTP client to Go internal worker API.

    Authenticates with a static bearer token (ServiceBearerAuth on the Go side).
    Use as a context manager to ensure the underlying connection pool is closed.
    """

    def __init__(self, base_url: str | None = None) -> None:
        s = get_settings()
        self._base_url = (base_url or s.go_service_url).rstrip("/")
        self._token = s.service_token
        # trust_env=False — ignore http_proxy / HTTP_PROXY env vars.
        # The Go internal API is always local; routing through a proxy breaks it.
        self._client = httpx.Client(timeout=s.go_client_timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def update_task(
        self,
        task_id: str,
        status: TaskStatus,
        celery_task_id: str | None = None,
        result_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """PATCH /internal/worker/tasks/{task_id}/status"""
        body = TaskStatusUpdate(
            status=status,
            celery_task_id=celery_task_id,
            result_payload=result_payload,
            error_message=error_message,
        ).model_dump(exclude_none=True)

        url = f"{self._base_url}/internal/worker/tasks/{task_id}/status"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        try:
            response = self._client.patch(url, json=body, headers=headers)
        except _RETRYABLE:
            raise

        if response.status_code >= 500:
            raise GoServerError(f"go update_task failed: {response.status_code} {response.text}")
        if response.status_code >= 400:
            raise GoClientError(f"go update_task failed: {response.status_code} {response.text}")
