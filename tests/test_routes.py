from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER

TASK_ID = str(uuid4())
DOC_ID = str(uuid4())
CELERY_ID = "celery-abc-123"


@pytest.fixture
def client():
    with patch("app.api.routes.dispatch") as mock_dispatch, \
         patch("app.api.routes.celery_app") as mock_celery:
        mock_result = MagicMock()
        mock_result.id = CELERY_ID
        mock_dispatch.return_value = mock_result
        mock_celery.control.ping.return_value = [{"worker@host": {"ok": "pong"}}]

        from app.main import app
        with TestClient(app) as c:
            yield c, mock_dispatch


def test_health_returns_ok(client):
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["celery"] == "ok"


def test_process_accepted_with_valid_token(client):
    c, mock_dispatch = client
    resp = c.post(
        "/process",
        json={
            "task_id": TASK_ID,
            "document_id": DOC_ID,
            "module_name": "convert",
            "storage_path": "tenders/docs/file.docx",
        },
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["task_id"] == TASK_ID
    assert data["celery_task_id"] == CELERY_ID
    mock_dispatch.assert_called_once()


def test_process_returns_401_without_token(client):
    c, _ = client
    resp = c.post(
        "/process",
        json={
            "task_id": TASK_ID,
            "document_id": DOC_ID,
            "module_name": "convert",
            "storage_path": "tenders/docs/file.docx",
        },
    )
    assert resp.status_code == 401


def test_process_returns_401_with_wrong_token(client):
    c, _ = client
    resp = c.post(
        "/process",
        json={
            "task_id": TASK_ID,
            "document_id": DOC_ID,
            "module_name": "convert",
            "storage_path": "tenders/docs/file.docx",
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_process_returns_400_for_unknown_module(client):
    c, mock_dispatch = client
    mock_dispatch.side_effect = ValueError("Unknown module: bad_module")
    resp = c.post(
        "/process",
        json={
            "task_id": TASK_ID,
            "document_id": DOC_ID,
            "module_name": "convert",
            "storage_path": "tenders/docs/file.docx",
        },
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 400
