import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.api.schemas import HealthResponse, ProcessRequest, ProcessResponse
from app.celery_app import celery_app
from app.config import get_settings
from app.workers.router import dispatch

log = logging.getLogger(__name__)
router = APIRouter()


def _require_service_token(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {get_settings().service_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    celery_ok = False
    try:
        pong = celery_app.control.ping(timeout=0.5)
        celery_ok = bool(pong)
    except Exception as exc:
        log.warning("celery ping failed: %s", exc)
    return HealthResponse(
        status="ok",
        celery="ok" if celery_ok else "degraded",
    )


@router.post(
    "/process",
    response_model=ProcessResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_service_token)],
)
def process(payload: ProcessRequest) -> ProcessResponse:
    try:
        async_result = dispatch(
            task_id=str(payload.task_id),
            document_id=str(payload.document_id),
            module_name=payload.module_name,
            storage_path=payload.storage_path,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return ProcessResponse(
        task_id=payload.task_id,
        celery_task_id=async_result.id,
    )
