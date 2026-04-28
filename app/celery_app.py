from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "python_kpi_tenders",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.convert",
        "app.workers.anonymize",
        "app.workers.resolve_keys",
        "app.workers.extract",
        "app.workers.parse_invoice",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_default_queue="io",
    # io  — I/O-bound parsing (DOCX/XLSX/PDF), scale with --concurrency
    # llm — LLM + NER-heavy tasks (Gemini, Natasha), keep concurrency low
    task_routes={
        "app.workers.convert.convert_task": {"queue": "io"},
        "app.workers.parse_invoice.parse_invoice_task": {"queue": "io"},
        "app.workers.anonymize.anonymize_task": {"queue": "llm"},
        "app.workers.resolve_keys.resolve_keys_task": {"queue": "llm"},
        "app.workers.extract.extract_task": {"queue": "llm"},
    },
)
