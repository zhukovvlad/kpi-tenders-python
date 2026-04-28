"""Unit tests for the resolve_keys worker.

All external dependencies are mocked:
- GoClient       — validated via mock assertions
- GeminiClient   — replaced with a MagicMock
- get_client()   — patched to return the mock

Tests cover:
1. Result payload shape (new_keys, resolved_schema)
2. Gemini call arguments (correct model, schema, prompt content)
3. Error handling (missing kwargs → ValueError, Gemini failure → retry)
4. LLM response conversion (is_new splits into new_keys vs existing)
"""

from unittest.mock import MagicMock, patch

import pytest

from app.llm.gemini_client import GeminiAPIError
from app.llm.resolve_keys_llm import _ResolvedKeyItem, _ResolveKeysResponse, resolve_keys
from app.workers.resolve_keys import _run_resolve_keys, resolve_keys_task

# ─── Fixtures ────────────────────────────────────────────────────────────────

_TASK_ID = "task-uuid-001"
_DOC_ID = "doc-uuid-001"
_STORAGE_PATH = "tenders/docs/contract.docx"

_RAW_QUESTIONS = [
    "Какова общая площадь объекта?",
    "Каков размер авансового платежа?",
    "Когда дата окончания работ?",
]
_EXISTING_KEYS = [
    {"key_name": "contract_value", "source_query": "Сумма договора?", "data_type": "number"},
]


def _make_llm_response(
    items: list[tuple[str, str, str, bool]],
) -> _ResolveKeysResponse:
    """Build a mock ``_ResolveKeysResponse`` from (key_name, source_query, data_type, is_new) tuples."""
    return _ResolveKeysResponse(
        keys=[
            _ResolvedKeyItem(
                key_name=key_name,
                source_query=source_query,
                data_type=data_type,
                is_new=is_new,
            )
            for key_name, source_query, data_type, is_new in items
        ]
    )


def _make_gemini_client(response: _ResolveKeysResponse) -> MagicMock:
    client = MagicMock()
    client.generate.return_value = response
    return client


def _make_go_client() -> tuple[MagicMock, MagicMock]:
    go_instance = MagicMock()
    ctx_manager = MagicMock()
    ctx_manager.__enter__.return_value = go_instance
    ctx_manager.__exit__.return_value = False
    return ctx_manager, go_instance


def _make_celery_task() -> MagicMock:
    task = MagicMock()
    task.request.id = "celery-task-abc"
    task.retry.side_effect = RuntimeError("retried")
    return task


# ═══════════════════════════════════════════════════════════════════════════
# resolve_keys_llm.resolve_keys — pure function tests
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveKeysFunction:
    def test_returns_new_keys_for_new_questions(self):
        client = _make_gemini_client(
            _make_llm_response(
                [
                    ("total_square", _RAW_QUESTIONS[0], "number", True),
                    ("advance_payment", _RAW_QUESTIONS[1], "number", True),
                ]
            )
        )
        result = resolve_keys(
            client,
            raw_questions=_RAW_QUESTIONS[:2],
            existing_keys=_EXISTING_KEYS,
        )
        assert len(result["new_keys"]) == 2
        assert result["new_keys"][0]["key_name"] == "total_square"
        assert result["new_keys"][1]["key_name"] == "advance_payment"

    def test_existing_keys_not_in_new_keys(self):
        client = _make_gemini_client(
            _make_llm_response(
                [
                    ("contract_value", _RAW_QUESTIONS[0], "number", False),
                ]
            )
        )
        result = resolve_keys(
            client,
            raw_questions=[_RAW_QUESTIONS[0]],
            existing_keys=_EXISTING_KEYS,
        )
        assert result["new_keys"] == []

    def test_resolved_schema_contains_all_keys(self):
        client = _make_gemini_client(
            _make_llm_response(
                [
                    ("contract_value", _RAW_QUESTIONS[0], "number", False),
                    ("total_square", _RAW_QUESTIONS[1], "number", True),
                ]
            )
        )
        result = resolve_keys(
            client,
            raw_questions=_RAW_QUESTIONS[:2],
            existing_keys=_EXISTING_KEYS,
        )
        key_names = [k["key_name"] for k in result["resolved_schema"]]
        assert "contract_value" in key_names
        assert "total_square" in key_names

    def test_result_has_no_md_document_id(self):
        client = _make_gemini_client(_make_llm_response([("k", "q", "string", True)]))
        result = resolve_keys(
            client,
            raw_questions=["q"],
            existing_keys=[],
        )
        assert "md_document_id" not in result

    def test_empty_questions_raises_value_error(self):
        client = _make_gemini_client(_make_llm_response([]))
        with pytest.raises(ValueError, match="raw_questions"):
            resolve_keys(client, raw_questions=[], existing_keys=[])

    def test_resolved_schema_entry_has_key_name_and_data_type(self):
        client = _make_gemini_client(_make_llm_response([("deadline_date", "q", "date", True)]))
        result = resolve_keys(
            client,
            raw_questions=["q"],
            existing_keys=[],
        )
        schema_entry = result["resolved_schema"][0]
        assert "key_name" in schema_entry
        assert "data_type" in schema_entry

    def test_new_key_entry_has_source_query(self):
        client = _make_gemini_client(_make_llm_response([("total_sq", "Площадь?", "number", True)]))
        result = resolve_keys(
            client,
            raw_questions=["Площадь?"],
            existing_keys=[],
        )
        assert result["new_keys"][0]["source_query"] == "Площадь?"

    def test_gemini_called_with_correct_model(self):
        client = _make_gemini_client(_make_llm_response([("k", "q", "string", True)]))
        with patch("app.llm.resolve_keys_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_light_model = "gemini-2.0-flash"
            resolve_keys(
                client,
                raw_questions=["q"],
                existing_keys=[],
            )
        call_kwargs = client.generate.call_args[1]
        assert call_kwargs["model"] == "gemini-2.0-flash"

    def test_gemini_called_with_correct_response_schema(self):
        client = _make_gemini_client(_make_llm_response([("k", "q", "string", True)]))
        with patch("app.llm.resolve_keys_llm.get_settings") as mock_settings:
            mock_settings.return_value.gemini_light_model = "gemini-2.0-flash"
            resolve_keys(
                client,
                raw_questions=["q"],
                existing_keys=[],
            )
        call_kwargs = client.generate.call_args[1]
        assert call_kwargs["response_schema"] is _ResolveKeysResponse

    def test_cardinality_mismatch_raises_value_error(self):
        """LLM returned fewer keys than raw_questions — must raise ValueError."""
        # 2 questions but LLM returns only 1 key
        client = _make_gemini_client(
            _make_llm_response([("total_square", _RAW_QUESTIONS[0], "number", True)])
        )
        with (
            patch("app.llm.resolve_keys_llm.get_settings") as mock_settings,
            pytest.raises(ValueError, match="expected 2"),
        ):
            mock_settings.return_value.gemini_light_model = "gemini-2.0-flash"
            resolve_keys(
                client,
                raw_questions=_RAW_QUESTIONS[:2],
                existing_keys=[],
            )


# ═══════════════════════════════════════════════════════════════════════════
# _run_resolve_keys — lifecycle tests (GoClient mocked)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestRunResolveKeys:
    def _patch_go(self, go_ctx):
        return patch("app.workers.resolve_keys.GoClient", return_value=go_ctx)

    def _patch_client(self, gemini_client):
        return patch("app.workers.resolve_keys.get_client", return_value=gemini_client)

    def _patch_resolve_keys(self, result):
        return patch("app.workers.resolve_keys.resolve_keys", return_value=result)

    def test_marks_processing_then_completed(self):
        go_ctx, go = _make_go_client()
        task = _make_celery_task()
        payload = {"new_keys": [], "resolved_schema": []}

        with (
            self._patch_go(go_ctx),
            self._patch_client(MagicMock()),
            self._patch_resolve_keys(payload),
        ):
            _run_resolve_keys(task, _TASK_ID, _RAW_QUESTIONS, _EXISTING_KEYS)

        go.update_task.assert_any_call(
            task_id=_TASK_ID, status="processing", celery_task_id="celery-task-abc"
        )
        go.update_task.assert_any_call(task_id=_TASK_ID, status="completed", result_payload=payload)

    def test_returns_result_payload(self):
        go_ctx, _ = _make_go_client()
        task = _make_celery_task()
        payload = {
            "new_keys": [{"key_name": "k", "source_query": "q", "data_type": "string"}],
            "resolved_schema": [],
        }

        with (
            self._patch_go(go_ctx),
            self._patch_client(MagicMock()),
            self._patch_resolve_keys(payload),
        ):
            result = _run_resolve_keys(task, _TASK_ID, _RAW_QUESTIONS, _EXISTING_KEYS)

        assert result == payload

    def test_gemini_api_error_reports_failed_and_not_retried(self):
        go_ctx, go = _make_go_client()
        task = _make_celery_task()

        with (
            self._patch_go(go_ctx),
            self._patch_client(MagicMock()),
            patch("app.workers.resolve_keys.resolve_keys", side_effect=GeminiAPIError("bad key")),
            pytest.raises(GeminiAPIError),
        ):
            _run_resolve_keys(task, _TASK_ID, _RAW_QUESTIONS, _EXISTING_KEYS)

        go.update_task.assert_called_with(
            task_id=_TASK_ID, status="failed", error_message="bad key"
        )
        task.retry.assert_not_called()

    def test_transient_error_retries(self):
        go_ctx, go = _make_go_client()
        task = _make_celery_task()

        with (
            self._patch_go(go_ctx),
            self._patch_client(MagicMock()),
            patch("app.workers.resolve_keys.resolve_keys", side_effect=ConnectionError("network")),
            pytest.raises(RuntimeError, match="retried"),
        ):
            _run_resolve_keys(task, _TASK_ID, _RAW_QUESTIONS, _EXISTING_KEYS)

        task.retry.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# resolve_keys_task — kwarg validation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestResolveKeysTaskValidation:
    """Tests for kwarg validation logic inside resolve_keys_task entry-point.

    ``.run()`` is already bound to the task instance (Celery's bind=True), so
    we call it without an explicit ``self`` argument and patch
    ``_run_resolve_keys`` to prevent actual Go/Gemini calls.

    Empty-questions validation now lives inside ``_run_resolve_keys`` (so
    Go receives a ``failed`` status update); the test mocks GoClient and
    get_client so no real connections are made.
    """

    def test_missing_raw_questions_raises_value_error(self):
        go_ctx, _ = _make_go_client()
        with (
            patch("app.workers.resolve_keys.GoClient", return_value=go_ctx),
            patch("app.workers.resolve_keys.get_client", return_value=MagicMock()),
            pytest.raises(ValueError, match="raw_questions"),
        ):
            resolve_keys_task.run(
                _TASK_ID,
                _DOC_ID,
                _STORAGE_PATH,
                raw_questions=[],
                existing_keys=_EXISTING_KEYS,
            )

    def test_valid_kwargs_delegates_to_run_resolve_keys(self):
        payload = {"new_keys": [], "resolved_schema": []}

        with patch("app.workers.resolve_keys._run_resolve_keys", return_value=payload) as mock_run:
            result = resolve_keys_task.run(
                _TASK_ID,
                _DOC_ID,
                _STORAGE_PATH,
                raw_questions=_RAW_QUESTIONS,
                existing_keys=_EXISTING_KEYS,
            )

        mock_run.assert_called_once()
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["task_id"] == _TASK_ID
        assert call_kwargs["raw_questions"] == _RAW_QUESTIONS
        assert result == payload

    def test_no_md_document_id_kwarg_required(self):
        """resolve_keys_task must succeed without md_document_id in kwargs."""
        payload = {"new_keys": [], "resolved_schema": []}

        with patch("app.workers.resolve_keys._run_resolve_keys", return_value=payload):
            result = resolve_keys_task.run(
                _TASK_ID,
                _DOC_ID,
                _STORAGE_PATH,
                raw_questions=_RAW_QUESTIONS,
                existing_keys=_EXISTING_KEYS,
                # md_document_id intentionally omitted
            )
        assert result == payload
