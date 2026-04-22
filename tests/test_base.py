from unittest.mock import MagicMock, patch

import pytest

from app.go_client.client import GoClientError
from app.workers.base import run_document_task


@pytest.fixture
def mock_task():
    task = MagicMock()
    task.request.id = "celery-task-123"
    # Make retry raise so we can assert it was called without infinite loops.
    task.retry.side_effect = RuntimeError("retried")
    return task


@pytest.fixture
def mock_go():
    go_instance = MagicMock()
    ctx = MagicMock()
    ctx.__enter__.return_value = go_instance
    ctx.__exit__.return_value = False
    with patch("app.workers.base.GoClient", return_value=ctx):
        yield go_instance


@pytest.fixture
def mock_minio():
    minio_instance = MagicMock()
    minio_instance.download.return_value = b"file bytes"
    with patch("app.workers.base.MinIOClient", return_value=minio_instance):
        yield minio_instance


def test_success_path_calls_go_in_order(mock_task, mock_go, mock_minio):
    handler = MagicMock(return_value={"parsed": True})

    result = run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    assert result == {"parsed": True}
    assert mock_go.update_task.call_count == 2
    mock_go.update_task.assert_any_call(
        task_id="t-1", status="processing", celery_task_id="celery-task-123"
    )
    mock_go.update_task.assert_any_call(
        task_id="t-1", status="completed", result_payload={"parsed": True}
    )
    handler.assert_called_once_with(b"file bytes", "tenders/f.docx", mock_minio)


def test_handler_failure_reports_failed_and_retries(mock_task, mock_go, mock_minio):
    handler = MagicMock(side_effect=RuntimeError("parse error"))

    with pytest.raises(RuntimeError, match="retried"):
        run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    mock_go.update_task.assert_called_with(
        task_id="t-1", status="failed", error_message="parse error"
    )
    mock_task.retry.assert_called_once()


def test_go_client_error_is_not_retried(mock_task, mock_go, mock_minio):
    # Completed call raises GoClientError — should propagate without retry.
    mock_go.update_task.side_effect = [None, GoClientError("go 500"), None]
    handler = MagicMock(return_value={})

    with pytest.raises(GoClientError):
        run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    mock_task.retry.assert_not_called()


def test_not_implemented_error_is_not_retried(mock_task, mock_go, mock_minio):
    handler = MagicMock(side_effect=NotImplementedError("stub"))

    with pytest.raises(NotImplementedError):
        run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    mock_task.retry.assert_not_called()


def test_value_error_is_not_retried(mock_task, mock_go, mock_minio):
    handler = MagicMock(side_effect=ValueError("empty document"))

    with pytest.raises(ValueError):
        run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    mock_task.retry.assert_not_called()


def test_failed_go_report_is_swallowed(mock_task, mock_go, mock_minio):
    # handler fails; Go also fails on the "failed" status update — should not propagate.
    mock_go.update_task.side_effect = [None, RuntimeError("broke"), RuntimeError("go down")]
    handler = MagicMock(return_value={})

    # The original RuntimeError triggers retry.
    with pytest.raises(RuntimeError, match="retried"):
        run_document_task(mock_task, "t-1", "d-1", "tenders/f.docx", handler)

    mock_task.retry.assert_called_once()
