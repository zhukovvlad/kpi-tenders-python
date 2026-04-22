from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ModuleName = Literal["convert", "anonymize", "extract", "parse_invoice"]
TaskStatus = Literal["pending", "processing", "completed", "failed"]


class ProcessRequest(BaseModel):
    task_id: UUID
    document_id: UUID
    module_name: ModuleName
    storage_path: str = Field(..., min_length=1)


class ProcessResponse(BaseModel):
    task_id: UUID
    celery_task_id: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    celery: Literal["ok", "degraded"]


class TaskStatusUpdate(BaseModel):
    """Body for PATCH /internal/worker/tasks/{task_id}/status on Go side."""

    status: TaskStatus
    celery_task_id: str | None = None
    result_payload: dict | None = None
    error_message: str | None = None
