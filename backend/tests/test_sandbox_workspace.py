import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "sandbox_runtime", ROOT / "scripts" / "sandbox_runtime.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["sandbox_runtime"] = MODULE
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    "path", ["/workspace", "/workspace/main.py", "workspace/src/app.ts"]
)
def test_safe_path_accepts_workspace_paths(path):
    assert MODULE.safe_path(path).startswith("/workspace")


def test_safe_path_resolves_repository_relative_paths_inside_workspace():
    assert MODULE.safe_path("tests/test_api.py") == "/workspace/tests/test_api.py"


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/workspace/../etc/passwd", "../../secret"]
)
def test_safe_path_rejects_escape(path):
    with pytest.raises(ValueError):
        MODULE.safe_path(path)


def test_host_payload_contains_only_runtime_fields():
    from app.services.sandbox_host import payload

    sandbox = SimpleNamespace(
        id="abc",
        volume_name="volume",
        container_name="container",
        image="image",
        cpu_limit=1.5,
        memory_limit_mb=768,
    )
    assert payload(sandbox) == {
        "sandbox_id": "abc",
        "volume_name": "volume",
        "container_name": "container",
        "image": "image",
        "cpu_limit": 1.5,
        "memory_limit_mb": 768,
    }


def test_coder_model_slot_uses_dedicated_role():
    from app.services.model_prefs import TASK_SLOTS

    assert TASK_SLOTS["coder"]["fallback_role"] == "default_coder"


def test_coder_tool_is_registered():
    import app.agent.tools  # noqa: F401
    from app.agent.base import get_tool_registry

    tool = get_tool_registry().get("delegate_to_coder")
    assert tool is not None
    assert tool.get_required_params() == ["task"]


def test_sandbox_exec_envelope_splits_stdout_and_stderr():
    from app.services.sandbox_commands import split_command_output

    stdout, stderr = split_command_output(
        json.dumps({"stdout": "ok\n", "stderr": "warning\n", "exit_code": 0}),
        0,
    )
    assert stdout == "ok\n"
    assert stderr == "warning\n"


def test_failed_plain_command_output_is_stderr():
    from app.services.sandbox_commands import split_command_output

    assert split_command_output("command refused", 2) == ("", "command refused")


def test_coder_contract_includes_toolchains_and_preview():
    from app.agent.tools import delegate_coder

    names = {item["function"]["name"] for item in delegate_coder.CODER_TOOLS}
    assert "start_preview" in names
    assert "setup_python_project" in names
    assert "uv sync" in delegate_coder.SYSTEM
    assert "never try pip" in delegate_coder.SYSTEM
    assert "written at most twice" in delegate_coder.SYSTEM


@pytest.mark.asyncio
async def test_coder_recovers_from_ollama_native_tool_parser_500(monkeypatch):
    from app.agent.tools import delegate_coder

    calls = []

    async def fake_chat_once(_model, _messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            request = httpx.Request("POST", "http://ollama/api/chat")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError(
                "parser failed", request=request, response=response
            )
        return {
            "message": {
                "content": json.dumps(
                    {
                        "tool": "read_file",
                        "args": {"path": "/workspace/main.py"},
                    }
                )
            }
        }

    monkeypatch.setattr(delegate_coder.providers, "chat_once", fake_chat_once)
    response = await delegate_coder._chat_with_tool_recovery(
        "qwen", [{"role": "user", "content": "continue"}]
    )
    function = response["message"]["tool_calls"][0]["function"]
    assert function == {
        "name": "read_file",
        "arguments": {"path": "/workspace/main.py"},
    }
    assert calls[0]["tools"] == delegate_coder.CODER_TOOLS
    assert calls[1]["tools"] is None
    assert calls[1]["format"] == "json"


@pytest.mark.asyncio
async def test_python_project_setup_writes_valid_pep621_and_syncs(monkeypatch):
    from app.agent.tools import delegate_coder

    calls = []

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        calls.append((action, payload))
        return {"exit_code": 0, "stdout": "synced", "stderr": ""}

    monkeypatch.setattr(delegate_coder.sandbox_host, "call", fake_call)
    _, event = await delegate_coder._dispatch(
        SimpleNamespace(),
        "setup_python_project",
        {
            "name": "notes-api",
            "dependencies": ["fastapi", "uvicorn"],
            "dev_dependencies": ["pytest", "httpx"],
        },
    )
    written = calls[0][1]["content"]
    assert 'dependencies = ["fastapi", "uvicorn"]' in written
    assert "[project.dependencies]" not in written
    assert calls[1][1]["command"] == "uv sync --all-groups"
    assert event["exit_code"] == 0


@pytest.mark.asyncio
async def test_coder_refuses_manual_python_toolchain_install(monkeypatch):
    from app.agent.tools import delegate_coder

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("prohibited command must not reach sandbox")

    monkeypatch.setattr(delegate_coder.sandbox_host, "call", unexpected_call)
    output, event = await delegate_coder._dispatch(
        SimpleNamespace(),
        "run_command",
        {"command": "pip install fastapi"},
    )
    assert "setup_python_project" in output
    assert event["exit_code"] == 2


@pytest.mark.asyncio
async def test_write_tool_returns_five_line_diff(monkeypatch):
    from app.agent.tools import delegate_coder

    calls = []

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        calls.append((action, payload))
        if action == "files/read":
            return {"content": "old\nvalue\n"}
        if action == "usage":
            return {"usage_bytes": 0}
        return {"written": 10}

    monkeypatch.setattr(delegate_coder.sandbox_host, "call", fake_call)
    sandbox = SimpleNamespace(workspace_quota_bytes=1024)
    _, event = await delegate_coder._dispatch(
        sandbox,
        "write_file",
        {"path": "main.py", "content": "new\nvalue\nextra\n"},
    )
    assert calls[-1][0] == "files/write"
    assert len(event["diff"].splitlines()) <= 5
    assert event["file_content"] == "new\nvalue\nextra\n"


def test_write_file_stops_when_parent_directory_cannot_be_created():
    class Container:
        def exec_run(self, *_args, **_kwargs):
            return SimpleNamespace(exit_code=1, output=(b"", b"permission denied"))

        def put_archive(self, *_args, **_kwargs):
            raise AssertionError("archive write must not run after mkdir failure")

    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: Container()
    spec = MODULE.SandboxSpec("id", "volume", "container", "image", 1, 256)
    with pytest.raises(RuntimeError, match="Could not create parent directory"):
        runtime.write_file(spec, "/workspace/new/file.txt", b"hello")


def test_workspace_owner_repair_uses_short_lived_root_helper():
    calls = []
    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime.client = SimpleNamespace(
        containers=SimpleNamespace(
            run=lambda *args, **kwargs: calls.append((args, kwargs))
        )
    )
    spec = MODULE.SandboxSpec("id", "volume", "container", "image", 1, 256)
    runtime._ensure_workspace_owner(spec)
    args, kwargs = calls[0]
    assert args[1] == ["chown", "-R", "1000:1000", "/workspace"]
    assert kwargs["remove"] is True
    assert kwargs["volumes"]["volume"]["bind"] == "/workspace"


def test_exec_route_is_not_shadowed_by_generic_lifecycle_action():
    from app.api.sandboxes import router

    post_paths = {
        route.path
        for route in router.routes
        if "POST" in getattr(route, "methods", set())
    }
    assert "/sandboxes/{sandbox_id}/{action}" not in post_paths
    assert "/sandboxes/{sandbox_id}/exec" in post_paths
    assert {
        "/sandboxes/{sandbox_id}/start",
        "/sandboxes/{sandbox_id}/stop",
        "/sandboxes/{sandbox_id}/restart",
    }.issubset(post_paths)


@pytest.mark.asyncio
async def test_agent_stream_forwards_nested_coder_events(monkeypatch):
    from app.agent import service
    from app.agent.base import ToolCall, ToolResult, ToolType, get_tool_registry
    from app.services import providers

    provider_round = 0
    execution_count = 0

    async def fake_stream_chat(*_args, **_kwargs):
        nonlocal provider_round
        provider_round += 1
        if provider_round == 1:
            yield {
                "tool_calls": [
                    {
                        "function": {
                            "name": "delegate_to_coder",
                            "arguments": json.dumps({"task": "run one command"}),
                        }
                    },
                    {
                        "function": {
                            "name": "delegate_to_coder",
                            "arguments": json.dumps({"task": "retry unnecessarily"}),
                        }
                    },
                ],
                "done": True,
            }
        else:
            yield {"content": "done", "done": True}

    async def fake_execute(*, _event_queue, _parent_tool_call_id, **_kwargs):
        nonlocal execution_count
        execution_count += 1
        step_id = f"{_parent_tool_call_id}-step-1"
        _event_queue.put_nowait(
            {
                "id": step_id,
                "type": "code_exec",
                "status": "running",
                "title": "Running command",
                "language": "shell",
                "code": "echo hello",
                "startedAt": time.time(),
            }
        )
        _event_queue.put_nowait(
            {
                "id": step_id,
                "type": "code_exec",
                "status": "completed",
                "title": "Running command",
                "output": "hello",
                "exitCode": 0,
                "completedAt": time.time(),
            }
        )
        return ToolResult(
            True,
            "finished",
            ToolCall(
                id="inner",
                type=ToolType.SANDBOX,
                name="delegate_to_coder",
                status="completed",
                title="Coding in workspace",
            ),
        )

    monkeypatch.setattr(providers, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(
        get_tool_registry().get("delegate_to_coder"), "execute", fake_execute
    )
    raw_events = [
        event
        async for event in service.run_agent_stream(
            [{"role": "user", "content": "use the workspace coder"}],
            model="qwen3:4b",
        )
    ]
    parsed = [
        json.loads(event.removeprefix("data: ").strip())
        for event in raw_events
        if event.startswith("data: {")
    ]
    nested = [
        event["tool_call"]
        for event in parsed
        if event.get("event") == "tool_call"
        and event.get("tool_call", {}).get("title") == "Running command"
    ]
    assert [event["status"] for event in nested] == ["running", "completed"]
    assert nested[0]["code"] == "echo hello"
    assert nested[1]["output"] == "hello"
    assert execution_count == 1
