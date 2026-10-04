"""Tests for the Code Execution agent tool (app/agent/tools/code_exec.py).

Scope:
- Tool schema (parameters, required params, aliases) and registry entry.
- execute() happy paths running REAL short-lived python subprocesses in
  the tool's temp-file sandbox: stdout capture, stderr appending,
  non-zero exit codes.
- _run_code() guards: unsupported languages, workspace markers, the
  dangerous-operation blocklist, subprocess timeout, temp-file cleanup.
- History persistence via persist_general_code_command (mocked — it
  talks to the real DB), including the non-fatal failure branch.
- Config helpers: _configured_timeout_s clamping + the
  _validate_code_exec_custom validator.

Mocks:
- app.services.sandbox_commands.persist_general_code_command is patched
  (never a real DB session).
- subprocess.run is patched to raise TimeoutExpired for the timeout
  branch (no real slow child processes).
- config_store.get_tool_config is patched where a persisted tool
  configuration is needed.
"""

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.agent.tools  # noqa: E402,F401
from app.agent.base import ToolType, get_tool_registry  # noqa: E402
from app.agent.tools.code_exec import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    CODE_EXEC_CONFIG,
    CodeExecTool,
    _configured_timeout_s,
    _validate_code_exec_custom,
)
from app.agent.tools.config_base import config_registry  # noqa: E402


# ── Schema ──────────────────────────────────────────────────────────


class TestCodeExecSchema:
    def test_parameters_and_required_params(self):
        tool = CodeExecTool()
        assert set(tool.get_parameters()) == {"code"}
        assert tool.get_parameters()["code"]["type"] == "string"
        assert tool.get_required_params() == ["code"]

    def test_aliases_map_script_and_program(self):
        assert CodeExecTool.param_aliases == {
            "code": "code",
            "script": "code",
            "program": "code",
        }

    def test_tool_metadata_and_registry(self):
        tool = CodeExecTool()
        assert tool.name == "use_code_exec"
        assert tool.tool_type is ToolType.CODE_EXEC
        assert get_tool_registry().has_tool("use_code_exec")

    def test_config_definition_registered(self):
        definition = config_registry.get("use_code_exec")
        assert definition is CODE_EXEC_CONFIG
        assert definition.custom_defaults == {"timeout_s": DEFAULT_TIMEOUT_S}
        schema = definition.schema_dict()
        assert schema[0]["key"] == "execution"
        assert schema[0]["fields"][0]["key"] == "timeout_s"


# ── execute() with real subprocesses (fast, safe snippets) ─────────


class TestCodeExecExecute:
    @pytest.mark.asyncio
    async def test_happy_path_stdout(self):
        result = await CodeExecTool().execute(code="print(6 * 7)")
        assert result.success is True
        assert "42" in result.output
        assert "exit code 0" in result.output
        call = result.tool_call
        assert call.status == "completed"
        assert call.exit_code == 0
        assert "42" in call.output
        assert call.language == "python"

    @pytest.mark.asyncio
    async def test_stderr_appended_to_output(self):
        result = await CodeExecTool().execute(
            code="import sys\nprint('out')\nsys.stderr.write('a warning')"
        )
        assert result.success is True
        assert "out" in result.output
        assert "[stderr]" in result.output
        assert "a warning" in result.output

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_reports_failure(self):
        result = await CodeExecTool().execute(code="raise SystemExit(3)")
        assert result.success is False
        assert "exit code 3" in result.output
        assert result.tool_call.exit_code == 3
        assert result.tool_call.status == "completed"

    @pytest.mark.asyncio
    async def test_traceback_captured_as_stderr(self):
        result = await CodeExecTool().execute(code="1 / 0")
        assert result.success is False
        assert "ZeroDivisionError" in result.output

    @pytest.mark.asyncio
    async def test_unsupported_language(self):
        result = await CodeExecTool().execute(code="puts 'hi'", language="ruby")
        assert result.success is False
        assert "Unsupported language: ruby" in result.output

    @pytest.mark.asyncio
    async def test_python3_language_alias_accepted(self):
        result = await CodeExecTool().execute(code="print('ok')", language="python3")
        assert result.success is True
        assert "ok" in result.output

    @pytest.mark.asyncio
    async def test_persists_history_for_conversation(self):
        persist = AsyncMock()
        conv_id = str(uuid.uuid4())
        with patch(
            "app.agent.tools.code_exec.persist_general_code_command", persist
        ):
            result = await CodeExecTool().execute(
                code="print('persisted')", conversation_id=conv_id
            )
        assert result.success is True
        persist.assert_awaited_once()
        kwargs = persist.await_args.kwargs
        assert kwargs["command"] == "print('persisted')"
        assert kwargs["exit_code"] == 0
        assert "persisted" in kwargs["stdout"]

    @pytest.mark.asyncio
    async def test_persistence_called_with_none_conversation(self):
        # The tool always calls the persist helper; the helper itself
        # no-ops when conversation_id is None (checked in its own tests).
        persist = AsyncMock()
        with patch(
            "app.agent.tools.code_exec.persist_general_code_command", persist
        ):
            await CodeExecTool().execute(code="print('anon')")
        persist.assert_awaited_once()
        assert persist.await_args.args[0] is None

    @pytest.mark.asyncio
    async def test_persistence_failure_is_non_fatal(self):
        persist = AsyncMock(side_effect=RuntimeError("db down"))
        with patch(
            "app.agent.tools.code_exec.persist_general_code_command", persist
        ):
            result = await CodeExecTool().execute(
                code="print('still works')", conversation_id=str(uuid.uuid4())
            )
        assert result.success is True
        assert "still works" in result.output

    @pytest.mark.asyncio
    async def test_execution_error_returns_failure(self):
        with patch.object(
            CodeExecTool, "_run_code", side_effect=RuntimeError("sandbox exploded")
        ):
            result = await CodeExecTool().execute(code="print('x')")
        assert result.success is False
        assert result.output == "Code execution failed: sandbox exploded"
        assert result.tool_call.status == "error"
        assert result.tool_call.error == "sandbox exploded"


# ── _run_code() guards ──────────────────────────────────────────────


class TestRunCodeGuards:
    def test_unsupported_language_direct(self):
        stdout, stderr, code = CodeExecTool()._run_code("print(1)", "javascript")
        assert (stdout, code) == ("", 1)
        assert "Unsupported language: javascript" in stderr

    @pytest.mark.parametrize(
        "code",
        [
            "from fastapi import FastAPI",
            "import fastapi",
            "app = uvicorn.Server()",
            "from flask import Flask",
            "import django",
            "open('/workspace/x')",
        ],
    )
    def test_workspace_markers_blocked(self, code):
        stdout, stderr, code_rc = CodeExecTool()._run_code(code, "python")
        assert stdout == ""
        assert code_rc == 1
        assert "cannot build or modify workspace" in stderr

    @pytest.mark.parametrize(
        "snippet",
        [
            "import subprocess",
            "os.system('rm -rf /')",
            "import shutil\nshutil.rmtree('/tmp')",
            "__import__('os')",
        ],
    )
    def test_dangerous_operations_blocked(self, snippet):
        stdout, stderr, code_rc = CodeExecTool()._run_code(snippet, "python")
        assert stdout == ""
        assert code_rc == 1
        assert stderr.startswith("Blocked dangerous operation:")

    def test_timeout_returns_error_tuple(self):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="python3", timeout=30)

        with (
            patch("app.agent.tools.code_exec.subprocess.run", raise_timeout),
            patch("app.agent.tools.code_exec._configured_timeout_s", lambda: 30),
        ):
            stdout, stderr, code = CodeExecTool()._run_code("import time", "python")
        assert stdout == ""
        assert code == 1
        assert stderr == "Error: Code execution timed out (30s)"

    @pytest.mark.asyncio
    async def test_timeout_surfaces_as_failed_result(self):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="python3", timeout=30)

        with (
            patch("app.agent.tools.code_exec.subprocess.run", raise_timeout),
            patch("app.agent.tools.code_exec._configured_timeout_s", lambda: 30),
        ):
            result = await CodeExecTool().execute(code="import time; time.sleep(60)")
        assert result.success is False
        assert "timed out (30s)" in result.output

    def test_temp_script_file_is_cleaned_up(self):
        created = []
        real = tempfile.NamedTemporaryFile

        def spy(*args, **kwargs):
            handle = real(*args, **kwargs)
            created.append(handle.name)
            return handle

        with patch(
            "app.agent.tools.code_exec.tempfile.NamedTemporaryFile", side_effect=spy
        ):
            stdout, stderr, code = CodeExecTool()._run_code("print('cleanup')", "python")

        assert code == 0
        assert "cleanup" in stdout
        assert created, "expected the tool to create a temp script"
        for path in created:
            assert not Path(path).exists()


# ── Config helpers + validator ──────────────────────────────────────


class TestCodeExecConfigHelpers:
    def test_timeout_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"timeout_s": 45}},
        ):
            assert _configured_timeout_s() == 45

    def test_timeout_clamped_to_bounds(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"timeout_s": 100_000}},
        ):
            assert _configured_timeout_s() == 600
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"timeout_s": 0}},
        ):
            assert _configured_timeout_s() == 1

    def test_timeout_invalid_or_missing_falls_back(self):
        for cfg in (
            None,
            {},
            {"custom": None},
            {"custom": {}},
            {"custom": {"timeout_s": "later"}},
        ):
            with patch(
                "app.agent.tools.config_store.get_tool_config", return_value=cfg
            ):
                assert _configured_timeout_s() == DEFAULT_TIMEOUT_S


class TestCodeExecCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_code_exec_custom("nope")

    def test_rejects_non_numeric_timeout(self):
        with pytest.raises(ValueError, match="'timeout_s' must be a number"):
            _validate_code_exec_custom({"timeout_s": "ten"})

    @pytest.mark.parametrize("value", [0, -1, 601])
    def test_rejects_out_of_range_timeout(self, value):
        with pytest.raises(ValueError, match="between 1 and 600"):
            _validate_code_exec_custom({"timeout_s": value})

    def test_numeric_string_timeout_coerced(self):
        cleaned = _validate_code_exec_custom({"timeout_s": "120"})
        assert cleaned["timeout_s"] == 120

    def test_valid_timeout_kept_as_int(self):
        cleaned = _validate_code_exec_custom({"timeout_s": 30.0})
        assert cleaned["timeout_s"] == 30

    def test_empty_custom_accepted(self):
        assert _validate_code_exec_custom({}) == {}
