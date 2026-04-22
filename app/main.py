import logging

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="python-kpi-tenders",
        version="0.1.0",
        description="AI worker for tender document processing",
    )
    app.include_router(router)
    return app


app = create_app()
