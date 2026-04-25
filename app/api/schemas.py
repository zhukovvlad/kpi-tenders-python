from typing import Literal

from pydantic import BaseModel

TaskStatus = Literal["pending", "processing", "completed", "failed"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    celery: Literal["ok", "degraded"]


class TaskStatusUpdate(BaseModel):
    """Body for PATCH /internal/worker/tasks/{task_id}/status on Go side."""

    status: TaskStatus
    celery_task_id: str | None = None
    result_payload: dict | None = None
    error_message: str | None = None
