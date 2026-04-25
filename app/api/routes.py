import logging

from fastapi import APIRouter

from app.api.schemas import HealthResponse
from app.celery_app import celery_app

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    celery_ok = False
    try:
        pong = celery_app.control.ping(timeout=0.5)
        celery_ok = bool(pong)
    except Exception as exc:
        log.warning("celery ping failed: %s", exc)
    return HealthResponse(
        status="ok" if celery_ok else "degraded",
        celery="ok" if celery_ok else "degraded",
    )
