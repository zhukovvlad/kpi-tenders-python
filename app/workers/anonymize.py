import json
import os
from typing import Any

from app.celery_app import celery_app
from app.nlp.anonymizer import anonymize
from app.storage.minio_client import MinIOClient, parse_storage_path
from app.workers.base import run_document_task


def _handle(file_bytes: bytes, storage_path: str, minio: MinIOClient) -> dict[str, Any]:
    text = file_bytes.decode("utf-8")
    anonymized_text, entities_map = anonymize(text)

    loc = parse_storage_path(storage_path, minio.default_bucket)
    stem, _ = os.path.splitext(loc.object_name)

    anon_path = minio.upload(
        f"{stem}_anonymized.md",
        anonymized_text.encode("utf-8"),
    )
    map_path = minio.upload(
        f"{stem}_entities.json",
        json.dumps(entities_map, ensure_ascii=False, indent=2).encode("utf-8"),
        content_type="application/json; charset=utf-8",
    )

    return {
        "anonymized_storage_path": anon_path,
        "entities_map_storage_path": map_path,
        "entity_count": len(entities_map),
    }


@celery_app.task(
    bind=True,
    name="app.workers.anonymize.anonymize_task",
    max_retries=3,
    default_retry_delay=30,
)
def anonymize_task(self, task_id: str, document_id: str, storage_path: str) -> dict[str, Any]:
    return run_document_task(self, task_id, document_id, storage_path, _handle)
