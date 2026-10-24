import asyncio
import json
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from app.agent import service, tracing
from app.agent.coder import agent as coder_agent, provider as coder_provider
from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, ToolRegistry
from app.config import settings
from app.services import coder_settings


@pytest.mark.asyncio
async def test_original_react_request_rejects_vanilla_before_dispatch_and_checks_correct_scaffold(
    coder_setup, monkeypatch
):
    wrong = {
        "name": "Publishing",
        "backend": "fastapi",
        "frontend": "vite",
        "entities": [
            {"name": "posts", "fields": [{"name": "title", "type": "string"}]}
        ],
    }
    correct = {**wrong, "frontend": "react-vite"}
    commands = ["pytest", "vitest run", "tsc --noEmit", "vite build"]
    dispatched = []

    async def dispatch(sandbox, name, args, *rest):
        dispatched.append((name, args))
        if name == "scaffold_crud_app":
            assert args["frontend"] == "react-vite"
            project = {
                "backend": {"test": "pytest"},
                "frontend": {
                    "test": "vitest run",
                    "typecheck": "tsc --noEmit",
                    "build": "vite build",
                },
            }
            return json.dumps({"created_files": ["app.py"], "setup": []}), {
                "changed": True,
                "exit_code": 0,
                "files": [
                    "/workspace/backend/app.py",
                    "/workspace/backend/tests/test_posts.py",
                    "/workspace/frontend/src/App.tsx",
                    "/workspace/frontend/src/App.test.tsx",
                ],
                "project": project,
                "project_handoff": {"application_generated": True},
            }
        if name == "run_checks":
            assert args["commands"] == commands
            checks = [
                {"command": command, "exit_code": 0, "output": "passed"}
                for command in commands
            ]
            return json.dumps({"checks": checks}), {"exit_code": 0, "checks": checks}
        if name == "start_preview":
            return "started", {"preview_url": "local", "port": args["port"]}
        if name == "check_preview":
            return "OK", {"exit_code": 0}
        raise AssertionError(name)

    monkeypatch.setattr(coder_agent, "dispatch_tool", dispatch)
    coder_responses(
        monkeypatch,
        [
            {"tool_calls": [native(wrong, "scaffold_crud_app")]},
            {"tool_calls": [native(correct, "scaffold_crud_app")]},
            {
                "tool_calls": [
                    native({"port": 6969, "command": "backend"}, "start_preview"),
                    native({"port": 6767, "command": "frontend"}, "start_preview"),
                    native({"port": 6767, "path": "/api/health"}, "check_preview"),
                ]
            },
            {"content": "Done"},
        ],
    )
    result = await coder_agent.CoderAgent().execute(
        task="Use a vanilla frontend (assistant mistake)",
        _user_request="Build a publishing app with FastAPI and a Vite React frontend.",
        conversation_id="conversation-1",
    )
    assert result.success, result.output
    assert [name for name, _ in dispatched][:2] == ["scaffold_crud_app", "run_checks"]
    row = (await saved(coder_setup / "coder"))[0]
    failed = row["rounds"][0]["tool_calls"][0]
    assert failed["execution_status"] == "failed" and "react-vite" in failed["error"]
    checks = row["rounds"][1]["tool_calls"][1]
    assert checks["name"] == "run_checks" and checks["origin"] == "application"


@pytest.mark.asyncio
async def test_completed_coder_thinking_stays_in_trace_but_not_next_model_request(
    coder_setup, monkeypatch
):
    requests = []
    responses = iter(
        [
            {
                "thinking": "RAW_CODER_REASONING",
                "content": "Inspect the relevant source.",
                "tool_calls": [native({"path": "/workspace"}, "list_files")],
            },
            {"content": "Done"},
        ]
    )

    async def chat(model, messages, **kwargs):
        requests.append(messages)
        return {"message": next(responses)}

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    await coder_agent.CoderAgent().execute(
        task="Inspect the project", conversation_id="conversation-1"
    )
    assert "RAW_CODER_REASONING" not in json.dumps(requests[1])
    row = (await saved(coder_setup / "coder"))[0]
    assert row["rounds"][0]["model_response"]["thinking"] == "RAW_CODER_REASONING"


@pytest.mark.asyncio
async def test_read_loop_receives_trusted_actionable_recovery_before_further_inference(
    coder_setup, monkeypatch
):
    requests = []
    responses = iter(
        [
            {
                "tool_calls": [
                    native(
                        {"path": "/workspace/source.py", "content": "pass"},
                        "write_file",
                    )
                ]
            },
            {"tool_calls": [native({"path": "/workspace/a.py"}, "read_file")]},
            {"tool_calls": [native({"path": "/workspace/b.py"}, "read_file")]},
            {"tool_calls": [native({"command": "pytest"}, "run_tests")]},
            {"content": "Done"},
        ]
    )

    async def chat(model, messages, **kwargs):
        requests.append(messages)
        return {"message": next(responses)}

    async def dispatch(sandbox, name, args, *rest):
        if name == "write_file":
            return "written", {"file": args["path"], "changed": True}
        if name == "run_tests":
            return "passed", {"command": "pytest", "exit_code": 0}
        return "source", {
            "read": {
                "path": args["path"],
                "content": "source",
                "sha256": "hash",
                "offset": 0,
                "total_chars": 6,
                "start_line": 1,
            }
        }

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    monkeypatch.setattr(coder_agent, "dispatch_tool", dispatch)
    result = await coder_agent.CoderAgent().execute(
        task="Implement a library", conversation_id="conversation-1"
    )
    assert result.success, result.output
    assert "Application recovery:" in json.dumps(requests[3])
    await saved(coder_setup / "coder")


@pytest.mark.asyncio
async def test_parser_retry_keeps_failed_attempts_separate_from_selected_response(
    trace_setup, monkeypatch
):
    count = 0

    async def stream(*args, **kwargs):
        nonlocal count
        count += 1
        if count <= 2:
            yield {
                "content": f"Failed attempt {count}",
                "tool_calls": [native({"input": "discarded"})],
            }
            raise service.providers.ProviderStreamError(
                "XML syntax error: <function> closed by </parameter>"
            )
        if count == 3:
            yield {
                "content": '```tool\n{"tool":"echo","args":{"input":"selected"}}\n```',
                "done": True,
            }
        else:
            yield {"content": "Done", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    await run()
    (row,) = await saved(trace_setup)
    attempts = row["rounds"][0]["model_attempts"]
    assert len(attempts) == 3
    assert [attempt["recovery"] for attempt in attempts] == [False, True, True]
    assert attempts[0]["response"]["message"]["tool_calls"][0]["function"][
        "arguments"
    ] == {"input": "discarded"}
    assert attempts[0]["error"]["type"] == "ProviderStreamError"
    assert attempts[1]["response"]["message"]["content"] == "Failed attempt 2"
    assert attempts[2]["exposed_tool_schemas"] is None
    first = row["rounds"][0]
    assert "Failed attempt" not in first["model_response"]["content"]
    assert first["model_response"]["native_tool_calls"] == []
    assert first["tool_calls"][0]["arguments"] == {"input": "selected"}
    assert row["outcome"]["successful_tool_calls"] == 1
    assert row["outcome"]["failed_tool_calls"] == 0


@pytest.mark.asyncio
async def test_argument_rebuild_keeps_raw_failed_attempts_and_json_response(
    trace_setup, monkeypatch
):
    raw = {"input": {"invalid": "RAW_REJECTED_PAYLOAD"}}
    rebuilt = '{"tool":"echo","args":{"input":"fixed"}}'
    scripted(
        monkeypatch,
        [
            [{"tool_calls": [native(raw)], "done": True}],
            [{"content": rebuilt, "done": True}],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run()
    record = (await saved(trace_setup))[0]
    assert record["outcome"]["status"] == "completed"
    assert record["outcome"]["failed_tool_calls"] == 1
    assert record["outcome"]["successful_tool_calls"] == 1
    assert record["rounds"][0]["tool_calls"][0]["arguments"] == raw
    second = record["rounds"][1]
    assert second["model_response"]["content"] == rebuilt
    assert second["parsed_tool_calls"][0]["args"] == {"input": "fixed"}
    assert "RAW_REJECTED_PAYLOAD" not in json.dumps(second["input_messages"])
    assert second["available_tools"] == ["echo"]
    assert not any(rebuilt in event for event in events)


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo input"
    tool_type = ToolType.CODE_EXEC

    async def execute(self, **kwargs):
        value = kwargs.get("input")
        if value == "raise":
            raise RuntimeError("execution failed")
        success = isinstance(value, str) and value != "invalid"
        output = value if success else "Invalid arguments: input is required"
        return ToolResult(
            success,
            output,
            ToolCall(
                id="echo",
                type=self.tool_type,
                name=self.name,
                error=None if success else output,
            ),
        )


@pytest.fixture
def trace_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TRACE_DIR", str(tmp_path))
    registry = ToolRegistry()
    registry.register(EchoTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {"echo"})
    monkeypatch.setattr(
        service, "_build_system_prompt", AsyncMock(return_value="Stable system prompt")
    )
    monkeypatch.setattr(service, "_model_supports_native_tools", lambda _: True)
    monkeypatch.setattr(
        service.model_prefs,
        "resolve_chat_request_model",
        AsyncMock(return_value="local:test"),
    )
    monkeypatch.setattr(
        service.config_store, "tool_model_override", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service.skill_store, "routing_catalog", lambda _: [])
    monkeypatch.setattr(
        service,
        "compact_conversation",
        AsyncMock(side_effect=lambda msgs, model, **kwargs: (msgs, False)),
    )
    monkeypatch.setattr(service, "get_relevant_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service, "build_cross_session_context", AsyncMock(return_value="")
    )
    monkeypatch.setattr(service, "for_conversation", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "catalog", AsyncMock(return_value={"items": []}))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(service, "async_session_factory", session)
    return tmp_path


def native(args, name="echo"):
    return {"function": {"name": name, "arguments": args}}


def scripted(monkeypatch, rounds):
    index = 0

    async def stream(*args, **kwargs):
        nonlocal index
        current = rounds[min(index, len(rounds) - 1)]
        index += 1
        for chunk in current:
            yield chunk

    monkeypatch.setattr(service.providers, "stream_chat", stream)


async def run():
    return [
        event
        async for event in service.run_agent_stream(
            [{"role": "user", "content": "Please use tools"}],
            model="local:test",
            conversation_id="conversation-1",
            think=False,
        )
    ]


async def saved(path):
    await tracing.flush_traces()
    return [
        json.loads(line)
        for file in path.glob("*.jsonl")
        for line in file.read_text().splitlines()
    ]


@pytest.fixture
def coder_setup(trace_setup, monkeypatch):
    monkeypatch.setattr(
        coder_settings,
        "_overrides",
        {"context_window": 16384, "generation_limit": 4096, "max_rounds": 30},
    )
    monkeypatch.setattr(
        coder_provider,
        "compact_conversation",
        AsyncMock(side_effect=lambda msgs, model, **kw: (msgs, False)),
    )
    monkeypatch.setattr(
        coder_agent,
        "compact_conversation",
        AsyncMock(side_effect=lambda msgs, model, **kw: (msgs, False)),
    )
    sandbox = SimpleNamespace(
        id="sandbox-1",
        name="QA",
        status="running",
        desired_running=True,
        error=None,
        cpu_limit=2,
        memory_limit_mb=2048,
        workspace_quota_bytes=1_000_000,
        usage_bytes=0,
        idle_timeout_seconds=1800,
    )
    conversation = SimpleNamespace(id="conversation-1")
    monkeypatch.setattr(
        coder_agent, "_create_task", AsyncMock(return_value=(conversation, sandbox))
    )
    monkeypatch.setattr(coder_agent.sandbox_host, "call", AsyncMock(return_value={}))
    monkeypatch.setattr(
        coder_agent, "resolve_task_model", AsyncMock(return_value="coder:test")
    )
    monkeypatch.setattr(coder_agent.skill_store, "routing_catalog", lambda _: "")
    monkeypatch.setattr(coder_agent.skill_store, "install_builtin_skills", lambda: None)
    monkeypatch.setattr(coder_agent, "_save_progress", AsyncMock())
    monkeypatch.setattr(coder_agent, "_record_command", AsyncMock())
    monkeypatch.setattr(coder_agent.CoderAgent, "_finish_task", AsyncMock())
    monkeypatch.setattr(coder_agent.CoderAgent, "_fail_task", AsyncMock())
    monkeypatch.setattr(
        coder_agent, "dispatch_tool", AsyncMock(return_value=("ok", None))
    )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(cancellation_requested=False))
        )

    monkeypatch.setattr(coder_agent, "async_session_factory", session)

    async def stream(model, history, tools=None, **kwargs):
        response = await coder_provider.providers.chat_once(
            model, history, tools=tools, **kwargs
        )
        message = response.get("message", {})
        yield {
            "content": message.get("content", ""),
            "thinking": message.get("thinking", ""),
            "tool_calls": message.get("tool_calls", []),
            "done": True,
            "finish_reason": response.get("done_reason", response.get("finish_reason")),
            "usage": response.get("usage"),
        }

    monkeypatch.setattr(coder_provider.providers, "stream_chat", stream)
    return trace_setup


def coder_responses(monkeypatch, messages):
    responses = iter(messages)

    async def chat(model, history, tools=None, **kwargs):
        tracing.observe(
            "provider_request",
            model,
            {"model": model, **kwargs},
            {"provider": "ollama"},
        )
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return {"message": response}

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)


@pytest.mark.asyncio
async def test_coder_trace_is_separate_and_restores_parent(coder_setup, monkeypatch):
    parent = tracing.Trace({"messages": [{"role": "user", "content": "Build app"}]})
    parent.begin_tool("delegate_to_coder", {"task": "Build app"})
    parent.tool_identity("delegate-1")
    token = tracing.current_trace.set(parent)
    coder_responses(
        monkeypatch,
        [
            {
                "tool_calls": [
                    native({"path": "/app.py", "content": "pass"}, "write_file"),
                    native({"path": "/workspace"}, "list_files"),
                ]
            },
            {"content": "Done"},
        ],
    )
    event_queue = asyncio.Queue()
    try:
        result = await coder_agent.CoderAgent().execute(
            task="Build app",
            conversation_id="conversation-1",
            _parent_tool_call_id="delegate-1",
            _event_queue=event_queue,
        )
        assert result.success
        assert tracing.current_trace.get() is parent
        assert parent.data["rounds"] == []
        parent.end_tool("completed", output=result.output)
        parent.finish("completed")
        tracing.enqueue(parent)
    finally:
        tracing.current_trace.reset(token)
    general = await saved(coder_setup)
    child = (await saved(coder_setup / "coder"))[0]
    assert general[0]["agent"] == "general"
    assert (
        general[0]["preparation_tools"][0]["tool_call_id"]
        == child["parent_tool_call_id"]
    )
    assert child["agent"] == "coder"
    assert child["parent_trace_id"] == parent.data["trace_id"]
    assert child["parent_tool_call_id"] == "delegate-1"
    assert child["sandbox_id"] == "sandbox-1"
    assert child["coder_task_id"]
    assert child["model"]["resolved"] == "coder:test"
    assert child["outcome"]["total_rounds"] == 2
    assert child["outcome"]["total_tool_calls"] == 2
    assert child["outcome"]["final_assistant_response"] == "Done"
    queued_events = []
    while not event_queue.empty():
        queued_events.append(event_queue.get_nowait())
    checkpoints = [
        event
        for event in queued_events
        if event.get("event") == "persistence_checkpoint"
    ]
    assert [event["round"] for event in checkpoints] == [1, 2]
    assert all(event["parentId"] == "delegate-1" for event in checkpoints)
    first = child["rounds"][0]
    assert first["inference"]["max_output_tokens"] == 4096
    assert first["inference"]["temperature"] == 0.6
    assert first["tool_calls"][0]["arguments"]["path"] == "/app.py"
    assert first["tool_calls"][0]["effective_arguments"]["path"] == "/workspace/app.py"
    assert first["model_attempts"][0]["duration_ms"] >= 0
    assert (coder_setup / "coder" / first["input_messages"][0]["prompt_ref"]).is_file()
    cli = Path(__file__).resolve().parents[2] / "scripts/inspect-agent-traces.py"
    output = subprocess.check_output(
        [
            sys.executable,
            str(cli),
            "--dir",
            str(coder_setup),
            "--agent",
            "coder",
            "show",
            child["trace_id"],
        ],
        text=True,
    )
    assert "Model attempt:" in output
    assert parent.data["trace_id"] in output


@pytest.mark.asyncio
async def test_coder_recovery_retains_raw_attempt_and_parsing(coder_setup, monkeypatch):
    error = httpx.HTTPStatusError(
        "Invalid native call",
        request=httpx.Request("POST", "http://local"),
        response=httpx.Response(500, text="bad arguments"),
    )
    coder_responses(
        monkeypatch,
        [error, {"content": '{"tool":"list_files","args":{}}'}, {"content": "Done"}],
    )
    result = await coder_agent.CoderAgent().execute(
        task="List files", conversation_id="conversation-1"
    )
    assert result.success
    row = (await saved(coder_setup / "coder"))[0]
    attempts = row["rounds"][0]["model_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["error"]["status_code"] == 500
    assert attempts[0]["error"]["response"] == "bad arguments"
    assert attempts[1]["recovery"]
    assert attempts[1]["exposed_tool_schemas"] is None
    assert attempts[1]["response"]["message"]["content"].startswith('{"tool"')
    assert row["rounds"][0]["parsed_tool_calls"][0]["function"]["name"] == "list_files"


@pytest.mark.asyncio
async def test_coder_invalid_recovery_is_preserved(coder_setup, monkeypatch):
    error = httpx.HTTPStatusError(
        "bad",
        request=httpx.Request("POST", "http://local"),
        response=httpx.Response(500),
    )
    coder_responses(monkeypatch, [error, {"content": "not json"}])
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1"
    )
    assert not result.success
    row = (await saved(coder_setup / "coder"))[0]
    assert row["outcome"]["status"] == "failed"
    assert (
        row["rounds"][0]["model_attempts"][1]["response"]["message"]["content"]
        == "not json"
    )
    assert (
        row["rounds"][0]["parse_diagnostics"][0]["status"]
        == "invalid_recovery_response"
    )


@pytest.mark.asyncio
async def test_coder_parser_fallback_stays_local_to_one_execution(
    coder_setup, monkeypatch
):
    coder_responses(
        monkeypatch,
        [
            service.providers.ProviderStreamError("failed to parse tool call"),
            service.providers.ProviderStreamError("failed to parse tool call"),
            {"content": '{"tool":"list_files","args":{"path":"/workspace"}}'},
            {"content": '{"final":"Done"}'},
        ],
    )
    result = await coder_agent.CoderAgent().execute(
        task="List files", conversation_id="conversation-1"
    )
    assert result.success
    (row,) = await saved(coder_setup / "coder")
    attempts = row["rounds"][0]["model_attempts"]
    assert len(attempts) == 3
    assert [bool(attempt["exposed_tool_schemas"]) for attempt in attempts] == [
        True,
        True,
        False,
    ]
    assert row["rounds"][1]["exposed_tool_schemas"] is None
    assert service.tool_capabilities.should_try_native(row["model"]["resolved"])


@pytest.mark.asyncio
async def test_coder_rounds_keep_static_prefix_and_one_current_working_snapshot(
    coder_setup, monkeypatch
):
    requests = []
    answers = iter(
        [
            {"tool_calls": [native({"path": "/workspace/a"}, "list_files")]},
            {"tool_calls": [native({"path": "/workspace/b"}, "list_files")]},
            {"content": "Done"},
        ]
    )

    async def chat(model, messages, **kwargs):
        requests.append(messages)
        return {"message": next(answers)}

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    assert (
        await coder_agent.CoderAgent().execute(
            task="Inspect files", conversation_id="conversation-1"
        )
    ).success

    # Derived state changes at the tail; keeping every old snapshot was filling
    # the context. Instructions/request and actual tool history remain stable.
    def snapshots(request):
        return [
            m
            for m in request
            if "application coder working state" in str(m.get("content", ""))
        ]

    history = []
    for request in requests:
        state_messages = snapshots(request)
        assert len(state_messages) == 1
        assert request[:2] == requests[0][:2]
        current = [m for m in request if m not in state_messages]
        assert current[: len(history)] == history
        history = current
    await saved(coder_setup / "coder")


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["limit", "command_failures", "invalid_args"])
async def test_coder_termination_and_invalid_args(
    coder_setup, monkeypatch, termination
):
    if termination == "limit":
        messages = [
            {"tool_calls": [native({"path": f"/workspace/dir{i}"}, "list_files")]}
            for i in range(30)
        ]
    elif termination == "command_failures":
        monkeypatch.setattr(
            coder_agent,
            "dispatch_tool",
            AsyncMock(return_value=("failed", {"command": "bad", "exit_code": 1})),
        )
        messages = [{"tool_calls": [native({"command": "bad"}, "run_command")]}] * 4
    else:
        messages = [
            {"tool_calls": [native("{broken", "list_files")]},
            {"content": "Done"},
        ]
    coder_responses(monkeypatch, messages)
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1"
    )
    row = (await saved(coder_setup / "coder"))[0]
    if termination == "limit":
        assert not result.success
        assert row["outcome"]["status"] == "max_rounds_reached"
        assert row["outcome"]["total_rounds"] == 30
    elif termination == "command_failures":
        assert not result.success
        assert row["outcome"]["failed_tool_calls"] == 3
        assert row["outcome"]["status"] == "failed"
    else:
        assert result.success
        assert row["rounds"][0]["parse_diagnostics"][0]["raw_arguments"] == "{broken"
        assert row["rounds"][0]["tool_calls"][0]["arguments"] == "{broken"
        assert row["rounds"][0]["tool_calls"][0]["execution_status"] == "failed"
        assert "effective_arguments" not in row["rounds"][0]["tool_calls"][0]


@pytest.mark.asyncio
async def test_coder_checks_cancel_requested_during_inference_before_tool_execution(
    coder_setup, monkeypatch
):
    current = SimpleNamespace(cancellation_requested=False)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=current))

    async def chat(*args, **kwargs):
        current.cancellation_requested = True
        return {
            "message": {
                "tool_calls": [
                    native({"path": "/workspace/x.py", "content": "pass"}, "write_file")
                ]
            }
        }

    monkeypatch.setattr(coder_agent, "async_session_factory", session)
    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1"
    )
    assert not result.success
    coder_agent.dispatch_tool.assert_not_awaited()
    coder_agent.CoderAgent._fail_task.assert_awaited_once()
    (trace,) = await saved(coder_setup / "coder")
    assert trace["outcome"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_coder_cancellation_restores_parent(coder_setup, monkeypatch):
    started = asyncio.Event()

    async def chat(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    parent = tracing.Trace({})
    token = tracing.current_trace.set(parent)
    try:
        task = asyncio.create_task(
            coder_agent.CoderAgent().execute(
                task="Build", conversation_id="conversation-1"
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tracing.current_trace.get() is parent
    finally:
        tracing.current_trace.reset(token)
    row = (await saved(coder_setup / "coder"))[0]
    assert row["outcome"]["status"] == "cancelled"
    assert row["rounds"][0]["model_attempts"][0]["error"]["type"] == "CancelledError"


@pytest.mark.asyncio
async def test_coder_disabled_and_logging_failure(coder_setup, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", False)
    coder_responses(monkeypatch, [{"content": "Done"}])
    assert (
        await coder_agent.CoderAgent().execute(
            task="Build", conversation_id="conversation-1"
        )
    ).success
    assert not (coder_setup / "coder").exists()
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", True)
    monkeypatch.setattr(
        tracing, "_persist", lambda _: (_ for _ in ()).throw(OSError("disk full"))
    )
    coder_responses(monkeypatch, [{"content": "Done"}])
    assert (
        await coder_agent.CoderAgent().execute(
            task="Build", conversation_id="conversation-1"
        )
    ).success
    await tracing.flush_traces()


@pytest.mark.asyncio
async def test_concurrent_coder_traces_redact_and_stay_distinct(
    coder_setup, monkeypatch
):
    async def chat(*args, **kwargs):
        await asyncio.sleep(0)
        return {"message": {"content": "password=secretvalue"}}

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    await asyncio.gather(
        *(
            coder_agent.CoderAgent().execute(
                task=f"Build {i}", conversation_id=f"conv-{i}"
            )
            for i in range(3)
        )
    )
    rows = await saved(coder_setup / "coder")
    assert len(rows) == 3
    assert len({r["trace_id"] for r in rows}) == 3
    assert {r["conversation_id"] for r in rows} == {"conv-0", "conv-1", "conv-2"}
    assert "secretvalue" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_coder_corrective_rewrites_and_automatic_preview(
    coder_setup, monkeypatch
):
    coder_responses(
        monkeypatch,
        [
            {
                "tool_calls": [
                    native(
                        {"path": "/workspace/a.py", "content": f"value = {i}"},
                        "write_file",
                    )
                    for i in range(3)
                ]
            },
            {"tool_calls": [native({"command": "pytest"}, "run_tests")]},
            {"content": "Done"},
        ],
    )

    async def dispatch(sandbox, name, args, *rest):
        if name == "run_tests":
            return "passed", {"command": "pytest", "exit_code": 0}
        if name == "start_preview":
            return "started", {"preview_url": "http://local", "port": 6969}
        return "written", {"file": "/workspace/a.py", "changed": True}

    monkeypatch.setattr(coder_agent, "dispatch_tool", dispatch)
    monkeypatch.setattr(
        coder_agent, "infer_preview", AsyncMock(return_value=("uv run main.py", 6969))
    )
    assert (
        await coder_agent.CoderAgent().execute(
            task="Build", conversation_id="conversation-1"
        )
    ).success
    row = (await saved(coder_setup / "coder"))[0]
    assert row["outcome"]["skipped_tool_calls"] == 0
    preview = row["rounds"][-1]["tool_calls"][0]
    assert preview["name"] == "start_preview"
    assert preview["origin"] == "application"
    assert row["outcome"]["final_assistant_response"] == "Done"


@pytest.mark.asyncio
async def test_coder_rejects_truncated_tool_calls_without_publishing_files(
    coder_setup, monkeypatch
):
    count = 0

    async def chat(*args, **kwargs):
        nonlocal count
        count += 1
        return {
            "done_reason": "length",
            "message": {
                "tool_calls": [
                    native(
                        {"path": "/workspace/file.ts", "content": "incomplete"},
                        "write_file",
                    )
                ]
            },
        }

    monkeypatch.setattr(coder_provider.providers, "chat_once", chat)
    queue = asyncio.Queue()
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1", _event_queue=queue
    )
    assert not result.success and count == 2
    coder_agent.dispatch_tool.assert_not_awaited()
    (trace,) = await saved(coder_setup / "coder")
    assert trace["outcome"]["failed_tool_calls"] == 2
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert sum(event.get("status") == "error" for event in events) == 2


@pytest.mark.asyncio
async def test_coder_schema_errors_stream_feedback_and_bound_repeated_attempts(
    coder_setup, monkeypatch
):
    coder_responses(
        monkeypatch,
        [{"tool_calls": [native({"path": "/workspace/x.py"}, "write_file")]}] * 3,
    )
    queue = asyncio.Queue()
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1", _event_queue=queue
    )
    assert not result.success
    coder_agent.dispatch_tool.assert_not_awaited()
    errors = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("status") == "error":
            errors.append(event)
    assert len(errors) == 3 and all("content" in event["error"] for event in errors)
    (trace,) = await saved(coder_setup / "coder")
    assert trace["outcome"]["failed_tool_calls"] == 3


@pytest.mark.asyncio
async def test_coder_records_skipped_siblings_after_repeated_validation_failures(
    coder_setup, monkeypatch
):
    invalid = native({"path": "/workspace/x.py"}, "write_file")
    coder_responses(
        monkeypatch,
        [
            {"tool_calls": [invalid]},
            {"tool_calls": [invalid]},
            {"tool_calls": [invalid, native({"path": "/workspace"}, "list_files")]},
        ],
    )
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1"
    )
    assert not result.success
    coder_agent.dispatch_tool.assert_not_awaited()
    (trace,) = await saved(coder_setup / "coder")
    calls = trace["rounds"][-1]["tool_calls"]
    assert len(calls) == 2
    assert calls[-1]["execution_status"] == "skipped"


@pytest.mark.asyncio
async def test_coder_database_failure_does_not_mask_original_tool_failure(
    coder_setup, monkeypatch
):
    monkeypatch.setattr(
        coder_agent,
        "_create_task",
        AsyncMock(side_effect=RuntimeError("original failure")),
    )
    monkeypatch.setattr(
        coder_agent.CoderAgent,
        "_fail_task",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    result = await coder_agent.CoderAgent().execute(
        task="Build", conversation_id="conversation-1"
    )
    assert not result.success and "original failure" in result.output
    (trace,) = await saved(coder_setup / "coder")
    assert trace["outcome"]["status"] == "failed"


@pytest.mark.asyncio
async def test_coder_initialization_failure_does_not_corrupt_parent(
    coder_setup, monkeypatch
):
    parent = tracing.Trace({})
    monkeypatch.setattr(
        tracing, "Trace", lambda _: (_ for _ in ()).throw(RuntimeError("logger broken"))
    )
    coder_responses(monkeypatch, [{"content": "Done"}])
    token = tracing.current_trace.set(parent)
    try:
        assert (
            await coder_agent.CoderAgent().execute(
                task="Build", conversation_id="conversation-1"
            )
        ).success
        assert tracing.current_trace.get() is parent
        assert parent.data["outcome"]["status"] == "running"
        assert parent.data["rounds"] == []
    finally:
        tracing.current_trace.reset(token)
    await tracing.flush_traces()


@pytest.mark.asyncio
async def test_coder_nonstream_provider_settings_are_observed(coder_setup, monkeypatch):
    client = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"parameters": "temperature 1"})
        return httpx.Response(200, json={"message": {"content": "Done"}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        coder_provider.providers,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kw: client(transport=transport, **kw)),
    )
    monkeypatch.setattr(
        tracing,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kw: client(transport=transport, **kw)),
    )
    monkeypatch.setattr(tracing, "_model_defaults", {})
    assert (
        await coder_agent.CoderAgent().execute(
            task="Build", conversation_id="conversation-1"
        )
    ).success
    row = (await saved(coder_setup / "coder"))[0]
    attempt = row["rounds"][0]["model_attempts"][0]
    assert attempt["inference"]["think"] is True
    assert attempt["inference"]["temperature"] == 0.6
    assert attempt["inference"]["max_output_tokens"] == 4096
    assert attempt["model_defaults"]["lookup_status"] == "available"


@pytest.mark.asyncio
async def test_coder_thinking_is_live_while_calls_wait_for_complete_response(
    coder_setup, monkeypatch
):
    release = asyncio.Event()
    queue = asyncio.Queue()
    original = "Build a FastAPI service with SQLite persistence."
    brief = "Build an in-memory service on port 8000."
    requests = []

    async def stream(model, messages, **kwargs):
        requests.append((messages, kwargs))
        if len(requests) == 1:
            yield {"thinking": "Read the files first.", "done": False}
            yield {
                "tool_calls": [native({"path": "/workspace"}, "list_files")],
                "done": False,
            }
            await release.wait()
            yield {
                "done": True,
                "finish_reason": "stop",
                "usage": {"output_tokens": 15},
            }
        else:
            yield {
                "thinking": "The user required SQLite, not in-memory storage.",
                "done": False,
            }
            yield {"content": "Inspected files.", "done": True, "finish_reason": "stop"}

    monkeypatch.setattr(coder_provider.providers, "stream_chat", stream)
    running = asyncio.create_task(
        coder_agent.CoderAgent().execute(
            task=brief,
            _user_request=original,
            conversation_id="conversation-1",
            _event_queue=queue,
            _parent_tool_call_id="delegate-1",
        )
    )
    received = []
    try:
        while not any(event.get("event") == "thinking" for event in received):
            received.append(await asyncio.wait_for(queue.get(), 1))
        assert not running.done()
        coder_agent.dispatch_tool.assert_not_awaited()
    finally:
        release.set()
        result = await running
    assert result.success
    while not queue.empty():
        received.append(queue.get_nowait())
    starts = [event for event in received if event.get("event") == "thinking_start"]
    assert [event["round"] for event in starts] == [1, 2]
    assert all(
        event["agent"] == "coder" and event["parentId"] == "delegate-1"
        for event in starts
    )
    assert (
        len([event for event in received if event.get("event") == "thinking_done"]) == 2
    )
    assert all(
        request[1]["think"] is True and request[1]["options"]["temperature"] == 0.6
        for request in requests
    )
    delegated = next(
        message["content"]
        for message in requests[0][0]
        if original in message.get("content", "")
    )
    assert (
        brief in delegated
        and "authoritative" in delegated
        and "not a new user request" in delegated
    )
    coder_agent.dispatch_tool.assert_awaited_once()
    (row,) = await saved(coder_setup / "coder")
    assert row["request"]["user_message"] == original
    assert row["request"]["delegated_task"] == brief
    assert row["rounds"][0]["model_response"]["thinking"] == "Read the files first."
    assert row["rounds"][0]["model_attempts"][0]["response"]["usage"] == {
        "output_tokens": 15
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["incomplete", "error", "cancelled"])
async def test_coder_interrupted_stream_closes_thinking_without_executing_partial_calls(
    coder_setup, monkeypatch, termination
):
    async def stream(*args, **kwargs):
        yield {"thinking": "Partial reasoning", "done": False}
        yield {
            "tool_calls": [native({"path": "/workspace"}, "list_files")],
            "done": False,
        }
        if termination == "error":
            raise RuntimeError("Connection lost")
        if termination == "cancelled":
            raise asyncio.CancelledError()

    monkeypatch.setattr(coder_provider.providers, "stream_chat", stream)
    queue = asyncio.Queue()
    execution = coder_agent.CoderAgent().execute(
        task="Inspect files", conversation_id="conversation-1", _event_queue=queue
    )
    if termination == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await execution
    else:
        assert not (await execution).success
    coder_agent.dispatch_tool.assert_not_awaited()
    events = list(queue._queue)
    assert (
        len([event for event in events if event.get("event") == "thinking_start"]) == 1
    )
    assert (
        len([event for event in events if event.get("event") == "thinking_done"]) == 1
    )
    (row,) = await saved(coder_setup / "coder")
    attempt = row["rounds"][0]["model_attempts"][0]
    assert attempt["response"]["message"]["thinking"] == "Partial reasoning"
    assert attempt["error"]


@pytest.mark.asyncio
async def test_coder_fallback_preserves_thinking_and_custom_inference_settings(
    coder_setup, monkeypatch
):
    monkeypatch.setattr(
        coder_settings, "_overrides", {"thinking": False, "temperature": 0}
    )
    monkeypatch.setattr(
        coder_provider.tool_capabilities, "should_try_native", lambda _: False
    )
    requests, tokens = [], []

    async def stream(model, messages, **kwargs):
        requests.append(kwargs)
        yield {"thinking": "Provider reasoning", "done": False}
        yield {"content": '{"final":"Done"}', "done": True, "finish_reason": "stop"}

    monkeypatch.setattr(coder_provider.providers, "stream_chat", stream)
    result = await coder_provider.chat_with_tool_recovery(
        "coder:test",
        [{"role": "user", "content": "Inspect files"}],
        on_thinking=tokens.append,
    )
    assert result["message"]["thinking"] == "Provider reasoning"
    assert result["message"]["content"] == "Done"
    assert tokens[0] == "Provider reasoning" and tokens[-1] is None
    assert requests[0]["think"] is False and requests[0]["options"]["temperature"] == 0
    assert requests[0]["tools"] is None


@pytest.mark.asyncio
async def test_success_context_and_multi_round_calls(trace_setup, monkeypatch):
    scripted(
        monkeypatch,
        [
            [
                {
                    "thinking": "Choose tools",
                    "tool_calls": [native({"input": "one"}), native({"input": "two"})],
                    "done": True,
                }
            ],
            [{"content": "Final answer", "done": True}],
        ],
    )
    events = await run()
    (row,) = await saved(trace_setup)
    assert row["schema_version"] == tracing.SCHEMA_VERSION
    assert row["request"]["user_message"] == "Please use tools"
    assert row["outcome"]["status"] == "completed"
    assert row["outcome"]["quality_evaluated"] is False
    assert row["outcome"]["total_tool_calls"] == 2
    assert row["outcome"]["successful_tool_calls"] == 2
    assert row["outcome"]["final_assistant_response"] == "Final answer"
    assert row["rounds"][0]["model_response"]["thinking"] == "Choose tools"
    assert (
        len([m for m in row["rounds"][1]["input_messages"] if m["role"] == "tool"]) == 2
    )
    assert row["rounds"][0]["exposed_tool_schemas"][0]["function"]["name"] == "echo"
    assert row["rounds"][0]["decision"] == "continued"
    ref = row["rounds"][0]["input_messages"][0]["prompt_ref"]
    assert (
        json.loads((trace_setup / ref).read_text())["content"] == "Stable system prompt"
    )
    assert len(list((trace_setup / "prompts").glob("*.json"))) == 1
    assert all(r["duration_ms"] >= 0 for r in row["rounds"])
    assert any('"event": "done"' in event for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,error",
    [
        ("{broken", "invalid_json"),
        ({"input": "invalid"}, "Invalid arguments"),
        ({"input": "raise"}, "execution failed"),
    ],
)
async def test_failed_calls_preserved(trace_setup, monkeypatch, args, error):
    scripted(
        monkeypatch,
        [
            [{"tool_calls": [native(args)], "done": True}],
            [
                {
                    "content": (
                        '{"tool":"echo","args":{"input":"Recovered"}}'
                        if isinstance(args, str)
                        else "Recovered"
                    ),
                    "done": True,
                }
            ],
            [{"content": "Recovered", "done": True}],
        ],
    )
    await run()
    (row,) = await saved(trace_setup)
    first = row["rounds"][0]
    assert (
        first["model_response"]["native_tool_calls"][0]["function"]["arguments"] == args
    )
    if isinstance(args, str):
        assert first["parse_diagnostics"][0]["status"] == error
    else:
        assert error in first["tool_calls"][0]["error"]
    assert first["tool_calls"][0]["execution_status"] == "failed"
    assert row["outcome"]["failed_tool_calls"] == 1


@pytest.mark.asyncio
async def test_text_parse_failure_preserved(trace_setup, monkeypatch):
    scripted(
        monkeypatch,
        [[{"content": '```tool\n{"tool": "echo", broken}\n```', "done": True}]],
    )
    await run()
    (row,) = await saved(trace_setup)
    assert row["rounds"][0]["parse_diagnostics"][0]["status"] == "invalid_json"
    assert "broken" in row["rounds"][0]["model_response"]["content"]
    assert row["outcome"]["total_tool_calls"] == 3
    assert row["outcome"]["unparsed_tool_attempts"] == 3
    assert row["outcome"]["failed_tool_calls"] == 3
    assert row["outcome"]["status"] == "failed"


@pytest.mark.asyncio
async def test_max_rounds_and_unknown_tool(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"tool_calls": [native({}, "missing")], "done": True}]])
    events = await run()
    (row,) = await saved(trace_setup)
    assert row["outcome"]["status"] == "max_rounds_reached"
    assert row["max_rounds"] == 15
    assert row["outcome"]["total_rounds"] == 15
    assert row["outcome"]["total_tool_calls"] == 15
    assert "15-round limit" in row["outcome"]["final_assistant_response"]
    assert "before confirming" in row["outcome"]["final_assistant_response"]
    assert any(
        json.loads(event.removeprefix("data: "))["event"] == "error" for event in events
    )
    assert row["outcome"]["final_response_source"] == "application"
    assert row["rounds"][0]["tool_calls"][0]["execution_status"] == "unavailable"


@pytest.mark.asyncio
async def test_cancelled_partial_generation(trace_setup, monkeypatch):
    ready = asyncio.Event()

    async def stream(*args, **kwargs):
        yield {"thinking": "Partial reasoning"}
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    task = asyncio.create_task(run())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = await saved(trace_setup)
    assert row["outcome"]["status"] == "cancelled"
    assert row["rounds"][0]["model_response"]["thinking"] == "Partial reasoning"


@pytest.mark.asyncio
async def test_explicit_generator_close(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Partial"}]])
    stream = service.run_agent_stream([{"role": "user", "content": "Hello"}])
    await anext(stream)
    await stream.aclose()
    (row,) = await saved(trace_setup)
    assert row["outcome"]["status"] == "cancelled"
    assert row["outcome"]["final_assistant_response"] == "Partial"


@pytest.mark.asyncio
async def test_provider_failure(trace_setup, monkeypatch):
    async def stream(*args, **kwargs):
        yield {"content": "Partial"}
        raise RuntimeError("provider offline")

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    await run()
    (row,) = await saved(trace_setup)
    assert row["outcome"]["status"] == "failed"
    assert "provider offline" in row["outcome"]["termination_reason"]


@pytest.mark.asyncio
async def test_concurrent_and_disabled(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])
    await asyncio.gather(*(run() for _ in range(16)))
    rows = await saved(trace_setup)
    assert len(rows) == 16
    assert len({row["trace_id"] for row in rows}) == 16
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", False)
    await run()
    assert len(await saved(trace_setup)) == 16


@pytest.mark.asyncio
async def test_logging_failure_does_not_affect_stream(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(tracing, "_write", fail)
    assert any("Answer" in event for event in await run())
    await tracing.flush_traces()
    assert not list(trace_setup.glob("*.jsonl"))


def test_redaction_and_unusual_values(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "never-persist-me")
    trace = tracing.Trace({"messages": []})
    cycle = []
    cycle.append(cycle)
    data = trace.safe(
        {
            "api_key": "hidden",
            "content": "never-persist-me Bearer abc.def password=secret https://user:pass@example.com",
            "images": ["base64"],
            "bytes": b"data",
            "object": object(),
            "cycle": cycle,
            "nan": float("nan"),
        }
    )
    text = json.dumps(data)
    for secret in (
        "never-persist-me",
        "abc.def",
        "password=secret",
        "user:pass",
        "base64",
    ):
        assert secret not in text
    assert data["api_key"] == "[REDACTED]"
    assert data["bytes"]["omitted"] == "binary"
    assert data["cycle"][0]["omitted"] == "cycle"


def test_tool_result_size_bound(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TRACE_MAX_TOOL_RESULT_CHARS", 100)
    trace = tracing.Trace({"messages": []})
    assert trace.safe("a" * 500, result=True)["truncated"]
    assert trace.safe(["abc" * 20] * 20, result=True)["truncated"]


def test_original_request_is_distinct_from_attachment_hint():
    trace = tracing.Trace(
        {
            "messages": [
                {"role": "user", "content": "Summarize my PDF"},
                {"role": "user", "content": "[System: attached document context]"},
            ],
            "trace_user_message": "Summarize my PDF",
        }
    )
    assert trace.data["request"]["user_message"] == "Summarize my PDF"
    assert len(trace.data["request"]["messages"]) == 2


@pytest.mark.asyncio
async def test_cli(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])
    await run()
    (row,) = await saved(trace_setup)
    cli = Path(__file__).resolve().parents[2] / "scripts/inspect-agent-traces.py"
    for command in (["list"], ["show", row["trace_id"]]):
        result = subprocess.run(
            [sys.executable, str(cli), "--dir", str(trace_setup), *command],
            capture_output=True,
            text=True,
            check=True,
        )
        assert row["trace_id"] in result.stdout


@pytest.mark.asyncio
async def test_parallel_writer_lock_and_prompt_publication(tmp_path):
    rows = [
        {"timestamp": "2026-10-13T00:00:00Z", "trace_id": str(index)}
        for index in range(32)
    ]
    await asyncio.gather(
        *(
            asyncio.to_thread(tracing._write, tmp_path, row, {"shared": "same prompt"})
            for row in rows
        )
    )
    saved_rows = [
        json.loads(line)
        for line in (tmp_path / "2026-10-13.jsonl").read_text().splitlines()
    ]
    assert len(saved_rows) == 32
    assert (
        json.loads((tmp_path / "prompts/shared.json").read_text())["content"]
        == "same prompt"
    )
    assert (tmp_path / "2026-10-13.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_slow_writer_does_not_block_streaming(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])
    entered = threading.Event()
    release = threading.Event()
    original = tracing._write

    def slow(*args):
        entered.set()
        release.wait(5)
        original(*args)

    monkeypatch.setattr(tracing, "_write", slow)
    try:
        await run()
        await asyncio.to_thread(entered.wait, 2)
        events = await asyncio.wait_for(run(), timeout=1)
        assert any("Answer" in event for event in events)
    finally:
        release.set()
        await tracing.flush_traces()


@pytest.mark.asyncio
async def test_observation_failure_does_not_affect_agent(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])

    def fail(*args):
        raise RuntimeError("observer error")

    monkeypatch.setattr(tracing.Trace, "chunk", fail)
    assert any("Answer" in event for event in await run())
    await tracing.flush_traces()


@pytest.mark.asyncio
async def test_redacts_raw_json_and_split_stream_secrets(trace_setup, monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "super-sensitive-key")
    scripted(
        monkeypatch,
        [
            [
                {"content": "super-sens"},
                {"content": 'itive-key {"password":"never-visible"}', "done": True},
            ]
        ],
    )
    await run()
    (row,) = await saved(trace_setup)
    serialized = json.dumps(row)
    assert "super-sensitive-key" not in serialized
    assert "never-visible" not in serialized


@pytest.mark.asyncio
async def test_effective_settings_and_malformed_provider_data(trace_setup, monkeypatch):
    async def stream(model, messages, **kwargs):
        tracing.observe(
            "provider_request",
            model,
            {"model": model, "temperature": 0.2, "think": False},
        )
        tracing.observe("malformed_frame", model, '{"message": broken}')
        yield {"content": "Answer", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    await run()
    (row,) = await saved(trace_setup)
    assert row["rounds"][0]["provider_settings"]["temperature"] == 0.2
    assert "broken" in row["rounds"][0]["malformed_provider_frames"][0]


@pytest.mark.asyncio
async def test_injected_memory_and_model_switch(trace_setup, monkeypatch):
    monkeypatch.setattr(
        service,
        "get_relevant_memories",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    pinned=True, text="Likes diagrams", category="preference"
                )
            ]
        ),
    )
    monkeypatch.setattr(
        service.config_store,
        "tool_model_override",
        AsyncMock(return_value="local:override"),
    )
    scripted(
        monkeypatch,
        [
            [{"tool_calls": [native({"input": "one"})], "done": True}],
            [{"content": "Answer", "done": True}],
        ],
    )
    await run()
    (row,) = await saved(trace_setup)
    assert "Likes diagrams" in json.dumps(row["rounds"][0]["context_messages"])
    assert row["rounds"][1]["model"] == "local:override"
    assert (
        row["rounds"][0]["tool_calls"][0]["effective_arguments"]["model"]
        == "local:override"
    )


@pytest.mark.asyncio
async def test_cancelled_tool_recorded(trace_setup, monkeypatch):
    entered = asyncio.Event()

    async def execute(self, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(EchoTool, "execute", execute)
    scripted(monkeypatch, [[{"tool_calls": [native({"input": "one"})], "done": True}]])
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = await saved(trace_setup)
    assert row["rounds"][0]["tool_calls"][0]["execution_status"] == "cancelled"


@pytest.mark.asyncio
async def test_trace_initialization_failure_is_nonfatal(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])

    def fail(*args):
        raise RuntimeError("trace initialization error")

    monkeypatch.setattr(tracing.Trace, "__init__", fail)
    assert any("Answer" in event for event in await run())
    assert await saved(trace_setup) == []


@pytest.mark.asyncio
async def test_queue_overflow_is_nonfatal(trace_setup, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TRACE_QUEUE_SIZE", 1)
    scripted(monkeypatch, [[{"content": "Answer", "done": True}]])
    entered, release = threading.Event(), threading.Event()
    original = tracing._write

    def slow(*args):
        entered.set()
        release.wait(5)
        original(*args)

    monkeypatch.setattr(tracing, "_write", slow)
    try:
        await run()
        await asyncio.to_thread(entered.wait, 2)
        await run()
        assert any("Answer" in event for event in await run())
    finally:
        release.set()
        await tracing.flush_traces()
    assert len(await saved(trace_setup)) == 2


@pytest.mark.asyncio
async def test_shutdown_snapshots_active_generator_once(trace_setup, monkeypatch):
    scripted(monkeypatch, [[{"content": "Partial"}]])
    stream = service.run_agent_stream([{"role": "user", "content": "Hello"}])
    await anext(stream)
    await tracing.flush_traces()
    await stream.aclose()
    (row,) = await saved(trace_setup)
    assert row["outcome"]["status"] == "shutdown"
    assert row["outcome"]["final_assistant_response"] == "Partial"


@pytest.mark.asyncio
async def test_effective_inference_settings_and_cache(trace_setup, monkeypatch):
    tracing._model_defaults.clear()
    post = AsyncMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "parameters": "temperature 1\nnum_predict 2048\nthink true\ntop_p 0.95",
                "modified_at": "version-1",
            },
        )
    )

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(tracing.httpx, "AsyncClient", Client)
    trace = tracing.Trace({"messages": []})
    token = tracing.current_trace.set(trace)
    try:
        trace.begin_round(
            1,
            "local",
            [],
            [],
            set(),
            [],
            {"think": None, "temperature": None, "max_output_tokens": None},
        )
        await tracing.observe_provider_request("local", {"model": "local"}, local=True)
        assert trace.data["inference"]["temperature"] == 1
        assert trace.data["inference"]["max_output_tokens"] == 2048
        assert trace.data["inference"]["think"] is True
        assert trace.data["inference"]["sources"]["temperature"] == "model_metadata"
        await tracing.observe_provider_request(
            "local",
            {
                "model": "local",
                "think": False,
                "options": {"temperature": 0, "num_predict": 20},
            },
            local=True,
        )
        assert trace.data["inference"]["temperature"] == 0
        assert trace.data["inference"]["max_output_tokens"] == 20
        assert trace.data["inference"]["think"] is False
        assert post.await_count == 1
    finally:
        tracing.current_trace.reset(token)
        tracing._model_defaults.clear()


@pytest.mark.asyncio
async def test_unavailable_defaults_are_explicit_and_nonfatal(trace_setup, monkeypatch):
    tracing._model_defaults.clear()

    def unavailable(**kwargs):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(tracing.httpx, "AsyncClient", unavailable)
    trace = tracing.Trace({"messages": []})
    token = tracing.current_trace.set(trace)
    try:
        trace.begin_round(1, "local", [], [], set(), [], {})
        await tracing.observe_provider_request("local", {"model": "local"}, local=True)
        assert trace.data["inference"]["temperature"] == "provider_default"
        assert trace.data["inference"]["sources"]["temperature"] == "not_exposed"
        trace.chunk({"thinking": "Reasoning"})
        assert trace.data["inference"]["thinking_observed"] is True
        assert trace.data["inference"]["think"] == "provider_default"
    finally:
        tracing.current_trace.reset(token)
        tracing._model_defaults.clear()


@pytest.mark.asyncio
async def test_groq_settings_match_forwarded_payload(trace_setup):
    trace = tracing.Trace({"messages": []})
    token = tracing.current_trace.set(trace)
    try:
        trace.begin_round(1, "cloud", [], [], set(), [], {"think": False})
        await tracing.observe_provider_request(
            "cloud", {"model": "cloud", "max_tokens": 50, "temperature": 0.2}
        )
        assert trace.data["inference"]["temperature"] == 0.2
        assert trace.data["inference"]["max_output_tokens"] == 50
        assert trace.data["inference"]["think"] == "provider_managed"
        assert trace.data["inference"]["requested"]["think"] is False
    finally:
        tracing.current_trace.reset(token)
