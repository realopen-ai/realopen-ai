"""Tests for the Past Conversation Search agent tool
(app/agent/tools/search_past_conversations.py).

Scope:
- Tool schema (parameters, aliases, tool type) and registry entry.
- execute() happy path with results (formatted via the real
  format_results_for_llm), exclusion of the current conversation, blank
  queries, empty result sets, and DB failures.
- _log()'s defensive formatting branch.
- Config helpers: _configured_max_results (clamping + bad values) and
  the _validate_spc_custom validator.

Mocks:
- async_session_factory is replaced by an in-memory fake context manager
  (NO database connection).
- app.services.session_search.search_past_messages is patched in the
  tool's namespace — the real SQL never runs; result formatting still
  uses the real implementation.
- config_store.get_tool_config is patched where a persisted tool
  configuration is needed.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.agent.tools  # noqa: E402,F401
from app.agent.base import ToolType, get_tool_registry  # noqa: E402
from app.agent.tools.config_base import config_registry  # noqa: E402
from app.agent.tools.search_past_conversations import (  # noqa: E402
    DEFAULT_MAX_RESULTS,
    SPC_CONFIG,
    SearchPastConversationsTool,
    _configured_max_results,
    _log,
    _validate_spc_custom,
)


class FakeSessionCtx:
    """Async context manager standing in for async_session_factory()."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _result_rows(n=1):
    return [
        {
            "message_id": f"m{i}",
            "conversation_id": f"c{i}",
            "conversation_title": f"Conversation {i}",
            "role": "user",
            "content_snippet": f"We discussed topic {i} in detail",
            "content_full": f"We discussed topic {i} in detail",
            "rank": 0.1 + i / 10,
            "created_at": 1_700_000_000 + i,
        }
        for i in range(1, n + 1)
    ]


def _patched_session(db):
    return patch(
        "app.agent.tools.search_past_conversations.async_session_factory",
        return_value=FakeSessionCtx(db),
    )


# ── Schema ──────────────────────────────────────────────────────────


class TestSearchSchema:
    def test_parameters_and_required_params(self):
        tool = SearchPastConversationsTool()
        assert set(tool.get_parameters()) == {"query"}
        assert tool.get_required_params() == ["query"]

    def test_aliases(self):
        aliases = SearchPastConversationsTool.param_aliases
        assert aliases["q"] == "query"
        assert aliases["search"] == "query"
        assert aliases["question"] == "query"

    def test_tool_metadata_and_registry(self):
        tool = SearchPastConversationsTool()
        assert tool.name == "search_past_conversations"
        assert tool.tool_type is ToolType.FILE_READ
        assert get_tool_registry().has_tool("search_past_conversations")

    def test_config_definition_registered(self):
        definition = config_registry.get("search_past_conversations")
        assert definition is SPC_CONFIG
        assert definition.custom_defaults == {"max_results": DEFAULT_MAX_RESULTS}
        assert DEFAULT_MAX_RESULTS == 10


# ── execute() ───────────────────────────────────────────────────────


class TestSearchExecute:
    @pytest.mark.asyncio
    async def test_happy_path_formats_results(self):
        db = object()
        search = AsyncMock(return_value=_result_rows(2))
        with (
            _patched_session(db),
            patch("app.agent.tools.search_past_conversations.search_past_messages", search),
        ):
            result = await SearchPastConversationsTool().execute(query="topic")

        assert result.success is True
        assert "Found 2 matching past message(s):" in result.output
        assert "[1] From 'Conversation 1' (user, rank=0.200):" in result.output
        assert "We discussed topic 1 in detail" in result.output
        assert "[2] From 'Conversation 2'" in result.output
        call = result.tool_call
        assert call.status == "completed"
        assert call.output == result.output
        assert call.query == "topic"

    @pytest.mark.asyncio
    async def test_search_called_with_db_and_defaults(self):
        db = object()
        search = AsyncMock(return_value=[])
        with (
            _patched_session(db),
            patch("app.agent.tools.search_past_conversations.search_past_messages", search),
        ):
            await SearchPastConversationsTool().execute(query="anything")

        search.assert_awaited_once()
        args, kwargs = search.await_args
        assert args[0] is db
        assert kwargs == {
            "query": "anything",
            "limit": DEFAULT_MAX_RESULTS,
            "exclude_conversation_id": None,
        }

    @pytest.mark.asyncio
    async def test_current_conversation_excluded(self):
        search = AsyncMock(return_value=[])
        conv_id = str(uuid.uuid4())
        with (
            _patched_session(object()),
            patch("app.agent.tools.search_past_conversations.search_past_messages", search),
        ):
            await SearchPastConversationsTool().execute(
                query="q", conversation_id=conv_id
            )
        assert search.await_args.kwargs["exclude_conversation_id"] == conv_id

    @pytest.mark.asyncio
    async def test_blank_query_short_circuits(self):
        search = AsyncMock()
        with _patched_session(object()), patch(
            "app.agent.tools.search_past_conversations.search_past_messages", search
        ):
            result = await SearchPastConversationsTool().execute(query="   ")

        assert result.success is True
        assert result.output == "No search query provided."
        assert result.tool_call.status == "completed"
        search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_results_message(self):
        with _patched_session(object()), patch(
            "app.agent.tools.search_past_conversations.search_past_messages",
            AsyncMock(return_value=[]),
        ):
            result = await SearchPastConversationsTool().execute(query="nothinghere")
        assert result.success is True
        assert result.output == "No past conversations found matching 'nothinghere'."

    @pytest.mark.asyncio
    async def test_long_snippet_truncated_in_formatting(self):
        rows = _result_rows(1)
        rows[0]["content_snippet"] = "x" * 400
        with _patched_session(object()), patch(
            "app.agent.tools.search_past_conversations.search_past_messages",
            AsyncMock(return_value=rows),
        ):
            result = await SearchPastConversationsTool().execute(query="q")
        assert "…" in result.output
        assert len([ln for ln in result.output.splitlines() if "xxxx" in ln][0]) < 320

    @pytest.mark.asyncio
    async def test_search_failure_returns_error(self):
        with _patched_session(object()), patch(
            "app.agent.tools.search_past_conversations.search_past_messages",
            AsyncMock(side_effect=RuntimeError("tsvector unavailable")),
        ):
            result = await SearchPastConversationsTool().execute(query="q")
        assert result.success is False
        assert result.output == "Past conversation search failed: tsvector unavailable"
        assert result.tool_call.status == "error"
        assert result.tool_call.error == "tsvector unavailable"

    @pytest.mark.asyncio
    async def test_session_factory_failure_returns_error(self):
        def boom():
            raise RuntimeError("pool exhausted")

        with patch(
            "app.agent.tools.search_past_conversations.async_session_factory", boom
        ):
            result = await SearchPastConversationsTool().execute(query="q")
        assert result.success is False
        assert "pool exhausted" in result.output

    @pytest.mark.asyncio
    async def test_configured_limit_forwarded_to_search(self):
        cfg = {"custom": {"max_results": 3}}
        search = AsyncMock(return_value=[])
        with (
            _patched_session(object()),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
            patch("app.agent.tools.search_past_conversations.search_past_messages", search),
        ):
            await SearchPastConversationsTool().execute(query="q")
        assert search.await_args.kwargs["limit"] == 3


# ── Config helpers + validator + misc ───────────────────────────────


class TestSearchConfigHelpers:
    def test_max_results_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_results": 7}},
        ):
            assert _configured_max_results() == 7

    def test_max_results_clamped_to_bounds(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_results": 500}},
        ):
            assert _configured_max_results() == 50
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_results": 0}},
        ):
            assert _configured_max_results() == 1

    def test_max_results_invalid_or_missing_falls_back(self):
        for cfg in (
            None,
            {},
            {"custom": None},
            {"custom": {}},
            {"custom": {"max_results": "many"}},
        ):
            with patch(
                "app.agent.tools.config_store.get_tool_config", return_value=cfg
            ):
                assert _configured_max_results() == DEFAULT_MAX_RESULTS


class TestSearchCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_spc_custom(42)

    def test_rejects_non_numeric_max_results(self):
        with pytest.raises(ValueError, match="'max_results' must be a number"):
            _validate_spc_custom({"max_results": "ten"})

    @pytest.mark.parametrize("value", [0, -3, 51])
    def test_rejects_out_of_range(self, value):
        with pytest.raises(ValueError, match="between 1 and 50"):
            _validate_spc_custom({"max_results": value})

    def test_numeric_string_coerced_to_int(self):
        cleaned = _validate_spc_custom({"max_results": "25"})
        assert cleaned["max_results"] == 25

    def test_valid_values_kept(self):
        assert _validate_spc_custom({"max_results": 50}) == {"max_results": 50}
        assert _validate_spc_custom({}) == {}


class TestLogHelper:
    def test_log_formats_args(self, capsys):
        _log("hello %s", "world")
        assert "[search_past_conv] hello world" in capsys.readouterr().out

    def test_log_without_args(self, capsys):
        _log("plain message")
        assert "[search_past_conv] plain message" in capsys.readouterr().out

    def test_log_survives_bad_format_string(self, capsys):
        # "%d" % "text" raises TypeError → falls back to concatenation.
        _log("count=%d", "not-a-number")
        assert "not-a-number" in capsys.readouterr().out
