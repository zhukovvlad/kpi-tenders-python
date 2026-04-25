from unittest.mock import patch


def test_health_returns_ok():
    with patch("app.api.routes.celery_app") as mock_celery:
        mock_celery.control.ping.return_value = [{"worker@host": {"ok": "pong"}}]

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            resp = c.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["celery"] == "ok"


def test_health_returns_degraded_when_celery_down():
    with patch("app.api.routes.celery_app") as mock_celery:
        mock_celery.control.ping.return_value = []

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            resp = c.get("/health")

    assert resp.status_code == 200
    assert resp.json()["celery"] == "degraded"
