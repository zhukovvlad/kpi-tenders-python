from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from app.go_client.client import GoClient, GoClientError

BASE_URL = "http://go-test"
TASK_URL = f"{BASE_URL}/internal/worker/tasks/task-1/status"


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.go_service_url = BASE_URL
    s.service_token = "test-token"
    s.go_client_timeout = 1.0
    return s


@pytest.fixture
def client(mock_settings):
    with patch("app.go_client.client.get_settings", return_value=mock_settings), GoClient() as c:
        yield c


@respx.mock
def test_update_task_succeeds_on_2xx(client):
    respx.patch(TASK_URL).mock(return_value=httpx.Response(200))
    client.update_task("task-1", "processing", celery_task_id="celery-abc")


@respx.mock
def test_update_task_raises_go_client_error_on_4xx(client):
    respx.patch(TASK_URL).mock(return_value=httpx.Response(400, text="bad request"))
    with pytest.raises(GoClientError, match="400"):
        client.update_task("task-1", "failed")


@respx.mock
def test_update_task_raises_go_client_error_on_5xx(client):
    respx.patch(TASK_URL).mock(return_value=httpx.Response(500, text="server error"))
    with pytest.raises(GoClientError, match="500"):
        client.update_task("task-1", "failed")


@respx.mock
def test_update_task_sends_bearer_token(client):
    route = respx.patch(TASK_URL).mock(return_value=httpx.Response(200))
    client.update_task("task-1", "processing")
    assert route.calls[0].request.headers["authorization"] == "Bearer test-token"


@respx.mock
def test_update_task_excludes_none_fields(client):
    route = respx.patch(TASK_URL).mock(return_value=httpx.Response(200))
    client.update_task("task-1", "processing", celery_task_id="c-1")
    import json
    body = json.loads(route.calls[0].request.content)
    assert "error_message" not in body
    assert "result_payload" not in body
    assert body["celery_task_id"] == "c-1"
