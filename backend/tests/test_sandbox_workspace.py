import asyncio
import importlib.machinery
import importlib.util
import json
import sys
import time
import uuid
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

TREE_LOADER = importlib.machinery.SourceFileLoader(
    "roai_tree", str(ROOT / "sandbox" / "helpers" / "roai-tree")
)
TREE_SPEC = importlib.util.spec_from_loader("roai_tree", TREE_LOADER)
TREE_MODULE = importlib.util.module_from_spec(TREE_SPEC)
TREE_LOADER.exec_module(TREE_MODULE)


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


def test_python_indentation_repair_fixes_off_by_one_block():
    malformed = (
        b"def client():\n"
        b'     \"\"\"Create a test client.\"\"\"\n'
        b"    return 1\n"
    )
    repaired = MODULE.repair_python_indentation(malformed)
    compile(repaired.decode(), "<test>", "exec")
    assert b'    \"\"\"Create a test client.\"\"\"' in repaired


def test_python_indentation_repair_leaves_valid_source_unchanged():
    valid = b"def client():\n    return 1\n"
    assert MODULE.repair_python_indentation(valid) == valid


def test_tree_does_not_follow_directory_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)

    tree = TREE_MODULE.walk(str(workspace), 4)

    assert len(tree) == 1
    assert tree[0]["name"] == "outside-link"
    assert tree[0]["type"] == "file"
    assert "children" not in tree[0]


class _ResolvedPathContainer:
    def __init__(self, resolved: str):
        self.status = "running"
        self.resolved = resolved

    def exec_run(self, command, demux=False):
        assert command[:3] == ["realpath", "-m", "--"]
        assert demux is True
        return SimpleNamespace(
            exit_code=0, output=((self.resolved + "\n").encode(), b"")
        )


def test_runtime_accepts_resolved_workspace_path():
    runtime = object.__new__(MODULE.SandboxRuntime)
    container = _ResolvedPathContainer("/workspace/src/main.py")
    runtime._container = lambda _name: container
    spec = SimpleNamespace(container_name="sandbox")

    actual_container, path = runtime._resolved_workspace_path(
        spec, "/workspace/link/main.py"
    )

    assert actual_container is container
    assert path == "/workspace/src/main.py"


def test_runtime_rejects_symlink_resolution_outside_workspace():
    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: _ResolvedPathContainer("/etc/passwd")
    spec = SimpleNamespace(container_name="sandbox")

    with pytest.raises(ValueError, match="Resolved path"):
        runtime._resolved_workspace_path(spec, "/workspace/outside/passwd")


class _BoundedReadContainer:
    status = "running"

    def __init__(self, content: bytes, *, exit_code: int = 0, stderr: bytes = b""):
        self.content = content
        self.exit_code = exit_code
        self.stderr = stderr
        self.commands = []

    def exec_run(self, command, demux=False):
        self.commands.append(command)
        if command[:3] == ["realpath", "-m", "--"]:
            return SimpleNamespace(
                exit_code=0, output=(b"/workspace/file.bin\n", b"")
            )
        assert command[:2] == ["head", "-c"]
        assert demux is True
        return SimpleNamespace(
            exit_code=self.exit_code, output=(self.content, self.stderr)
        )


def test_runtime_file_read_is_bounded_and_binary_safe():
    runtime = object.__new__(MODULE.SandboxRuntime)
    container = _BoundedReadContainer(b"\x00abc")
    runtime._container = lambda _name: container
    spec = SimpleNamespace(container_name="sandbox")

    assert runtime.read_file(spec, "/workspace/file.bin", max_bytes=4) == b"\x00abc"
    assert container.commands[-1] == [
        "head",
        "-c",
        "5",
        "--",
        "/workspace/file.bin",
    ]


def test_runtime_file_read_rejects_content_over_limit():
    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: _BoundedReadContainer(b"12345")
    spec = SimpleNamespace(container_name="sandbox")

    with pytest.raises(ValueError, match="4-byte read limit"):
        runtime.read_file(spec, "/workspace/file.bin", max_bytes=4)


def test_runtime_file_read_surfaces_container_errors():
    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: _BoundedReadContainer(
        b"", exit_code=1, stderr=b"not a regular file"
    )
    spec = SimpleNamespace(container_name="sandbox")

    with pytest.raises(RuntimeError, match="not a regular file"):
        runtime.read_file(spec, "/workspace/file.bin")


def test_runtime_file_size_uses_resolved_path():
    class Container:
        status = "running"

        def exec_run(self, command, demux=False):
            if command[:3] == ["realpath", "-m", "--"]:
                return SimpleNamespace(
                    exit_code=0, output=(b"/workspace/file.bin\n", b"")
                )
            assert command == ["stat", "-c", "%s", "--", "/workspace/file.bin"]
            assert demux is True
            return SimpleNamespace(exit_code=0, output=(b"90\n", b""))

    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: Container()
    spec = SimpleNamespace(container_name="sandbox")

    assert runtime.file_size(spec, "/workspace/link.bin") == 90


@pytest.mark.asyncio
async def test_api_replacement_quota_charges_only_size_delta(monkeypatch):
    from app.api import sandboxes

    item = SimpleNamespace(
        usage_bytes=90,
        workspace_quota_bytes=100,
        last_active_at=None,
    )
    actions = []

    async def fake_get_one(_db, _sandbox_id):
        return item

    async def fake_call(action, _item, payload=None, **_kwargs):
        actions.append((action, payload))
        if action == "usage":
            return {"usage_bytes": 90}
        if action == "files/stat":
            return {"exists": True, "size": 90}
        return {"written": 90}

    class DB:
        async def flush(self):
            return None

    monkeypatch.setattr(sandboxes, "get_one", fake_get_one)
    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)

    result = await sandboxes.write_file(
        uuid.uuid4(),
        sandboxes.FileWrite(path="/workspace/file", content="x" * 90),
        DB(),
    )

    assert result == {"written": 90}
    assert [action for action, _payload in actions] == [
        "usage",
        "files/stat",
        "files/write",
    ]


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


@pytest.mark.asyncio
async def test_preview_query_cannot_override_sandbox_identity(monkeypatch):
    from app.services import sandbox_host

    captured = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params):
            captured.update(params)
            return SimpleNamespace()

    monkeypatch.setattr(
        sandbox_host.httpx, "AsyncClient", lambda **_kwargs: Client()
    )
    sandbox = SimpleNamespace(
        id="trusted-id",
        volume_name="trusted-volume",
        container_name="trusted-container",
        image="trusted-image",
    )

    await sandbox_host.preview_get(
        sandbox,
        6969,
        "health",
        "container_name=attacker&volume_name=attacker&image=attacker&view=full",
    )

    assert captured == {
        "volume_name": "trusted-volume",
        "container_name": "trusted-container",
        "image": "trusted-image",
        "view": "full",
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


@pytest.mark.asyncio
async def test_coder_auto_provisions_default_workspace(monkeypatch):
    from app.agent.coder import agent
    from app.db.models import Conversation

    conversation_id = uuid.uuid4()
    conversation = SimpleNamespace(
        id=conversation_id, title="Automatic", sandbox_id=None
    )
    sandbox = SimpleNamespace(id=uuid.uuid4())
    provisioned = []

    class FakeDb:
        async def get(self, model, _ident):
            return conversation if model is Conversation else None

        async def execute(self, _statement, _params=None):
            return None

        async def refresh(self, _record):
            return None

        async def scalar(self, _statement):
            return None

        def add(self, record):
            self.record = record

        async def commit(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            self.db = FakeDb()
            return self.db

        async def __aexit__(self, *_args):
            return None

    async def fake_provision(_db, *, name, conversation):
        provisioned.append(name)
        conversation.sandbox_id = sandbox.id
        return sandbox

    monkeypatch.setattr(agent, "async_session_factory", SessionContext)
    monkeypatch.setattr(agent, "provision_sandbox", fake_provision)
    result_conversation, result_sandbox = await agent._create_task(
        str(conversation_id), uuid.uuid4(), "build it"
    )

    assert provisioned == ["Automatic workspace"]
    assert result_conversation is conversation
    assert result_sandbox is sandbox


def test_coder_contract_includes_toolchains_and_preview():
    from app.agent.coder.tooling import CODER_TOOLS
    from app.prompts import get_prompt

    names = {item["function"]["name"] for item in CODER_TOOLS}
    assert "start_preview" in names
    assert "setup_python_project" in names
    prompt = get_prompt("coder_system")
    assert "uv sync" in prompt
    assert "never try pip" in prompt
    assert "written at most twice" in prompt
    assert "0.0.0.0:6767" in prompt
    assert "0.0.0.0:6969" in prompt
    preview_tool = next(
        item for item in CODER_TOOLS if item["function"]["name"] == "start_preview"
    )
    ports = preview_tool["function"]["parameters"]["properties"]["port"]["enum"]
    assert ports == [6767, 6969]


def test_sandbox_uses_fixed_loopback_application_ports():
    assert MODULE.PREVIEW_PORTS == (6767, 6969)


def test_workspace_ready_events_link_live_client_to_sandbox():
    from app.agent.coder.agent import _workspace_events

    sandbox_id = uuid.uuid4()
    sandbox = SimpleNamespace(
        id=sandbox_id,
        name="Project workspace",
        status="running",
        desired_running=True,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        workspace_quota_bytes=2 * 1024**3,
        usage_bytes=0,
        idle_timeout_seconds=1800,
        error=None,
    )
    running, completed = _workspace_events(sandbox, "parent", uuid.uuid4())

    assert running["type"] == "sandbox"
    assert running["status"] == "running"
    assert running["sandboxId"] == str(sandbox_id)
    assert running["sandbox"]["name"] == "Project workspace"
    assert completed["id"] == running["id"]
    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_coder_recovers_from_ollama_native_tool_parser_500(monkeypatch):
    from app.agent.coder import provider
    from app.agent.coder.tooling import CODER_TOOLS

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

    monkeypatch.setattr(provider.providers, "chat_once", fake_chat_once)
    response = await provider.chat_with_tool_recovery(
        "qwen", [{"role": "user", "content": "continue"}]
    )
    function = response["message"]["tool_calls"][0]["function"]
    assert function == {
        "name": "read_file",
        "arguments": {"path": "/workspace/main.py"},
    }
    assert calls[0]["tools"] == CODER_TOOLS
    assert calls[1]["tools"] is None
    assert calls[1]["format"] == "json"


@pytest.mark.asyncio
async def test_python_project_setup_writes_valid_pep621_and_syncs(monkeypatch):
    from app.agent.coder import tooling

    calls = []

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        calls.append((action, payload))
        return {"exit_code": 0, "stdout": "synced", "stderr": ""}

    monkeypatch.setattr(tooling.sandbox_host, "call", fake_call)
    progress = []
    _, event = await tooling.dispatch_tool(
        SimpleNamespace(),
        "setup_python_project",
        {
            "name": "notes-api",
            "dependencies": ["fastapi", "uvicorn"],
            "dev_dependencies": ["pytest", "httpx"],
        },
        progress.append,
    )
    written = calls[0][1]["content"]
    assert 'dependencies = ["fastapi", "uvicorn"]' in written
    assert 'dev = ["pytest", "httpx", "httpx2"]' in written
    assert "[project.dependencies]" not in written
    assert calls[1][1]["command"] == "uv sync --all-groups"
    assert event["exit_code"] == 0
    assert [item["stage"] for item in progress] == [
        "project_written",
        "sync_completed",
    ]


@pytest.mark.asyncio
async def test_coder_refuses_manual_python_toolchain_install(monkeypatch):
    from app.agent.coder import tooling

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("prohibited command must not reach sandbox")

    monkeypatch.setattr(tooling.sandbox_host, "call", unexpected_call)
    output, event = await tooling.dispatch_tool(
        SimpleNamespace(),
        "run_command",
        {"command": "pip install fastapi"},
    )
    assert "setup_python_project" in output
    assert "uv add --dev httpx2" in output
    assert event["exit_code"] == 2
    assert event["corrective"] is True


@pytest.mark.asyncio
async def test_coder_refuses_web_server_outside_preview_tool(monkeypatch):
    from app.agent.coder import tooling

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("server command must not reach sandbox")

    monkeypatch.setattr(tooling.sandbox_host, "call", unexpected_call)
    output, event = await tooling.dispatch_tool(
        SimpleNamespace(),
        "run_command",
        {"command": "uvicorn app:app --host 0.0.0.0 --port 8000"},
    )
    assert "start_preview" in output
    assert "6969" in output
    assert event["exit_code"] == 2


def test_python_project_setup_starts_as_visible_terminal_command():
    from app.agent.coder.agent import _event_for_step

    event = _event_for_step(
        "setup_python_project", {"name": "notes-api"}, "step", None
    )
    assert event["type"] == "code_exec"
    assert event["language"] == "shell"
    assert event["code"] == "uv sync --all-groups"


@pytest.mark.parametrize(
    ("provided", "expected"),
    [
        ("app.py", "/workspace/app.py"),
        ("/app.py", "/workspace/app.py"),
        ("/tests/test_app.py", "/workspace/tests/test_app.py"),
        ("/workspace/main.py", "/workspace/main.py"),
        ("/workspace/../app.py", "/workspace/app.py"),
    ],
)
def test_coder_paths_are_canonicalized_into_workspace(provided, expected):
    from app.agent.coder.tooling import workspace_path

    assert workspace_path(provided) == expected


@pytest.mark.asyncio
async def test_python_setup_streams_file_write_before_sync_and_refreshes(monkeypatch):
    from app.agent.coder import agent

    async def fake_dispatch(_sandbox, name, _args, progress=None):
        assert name == "setup_python_project"
        progress(
            {
                "stage": "project_written",
                "file_content": "[project]\nname = 'demo'\n",
            }
        )
        progress({"stage": "sync_completed"})
        return "synced", {"command": "uv sync --all-groups", "exit_code": 0}

    async def fake_record(**_kwargs):
        return None

    monkeypatch.setattr(agent, "dispatch_tool", fake_dispatch)
    monkeypatch.setattr(agent, "_record_command", fake_record)
    queue = asyncio.Queue()
    await agent._run_step(
        sandbox=SimpleNamespace(id="sandbox-id"),
        conversation=SimpleNamespace(id="conversation-id"),
        task_id=uuid.uuid4(),
        name="setup_python_project",
        args={"name": "demo", "dependencies": [], "dev_dependencies": []},
        state=agent.RunState(),
        parent_id="parent",
        event_queue=queue,
    )
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert [(event["type"], event["status"]) for event in events] == [
        ("file_write", "running"),
        ("file_write", "completed"),
        ("code_exec", "running"),
        ("code_exec", "completed"),
    ]
    assert events[1]["filePath"] == "/workspace/pyproject.toml"
    assert events[-1]["refreshFiles"] is True


@pytest.mark.asyncio
async def test_coder_tool_error_is_returned_for_model_recovery(monkeypatch):
    from app.agent.coder import agent

    async def failed_dispatch(*_args, **_kwargs):
        raise RuntimeError("temporary file service failure")

    monkeypatch.setattr(agent, "dispatch_tool", failed_dispatch)
    queue = asyncio.Queue()
    output, event = await agent._run_step(
        sandbox=SimpleNamespace(id="sandbox-id"),
        conversation=SimpleNamespace(id="conversation-id"),
        task_id=uuid.uuid4(),
        name="write_file",
        args={"path": "/app.py", "content": "value = 1\n"},
        state=agent.RunState(),
        parent_id="parent",
        event_queue=queue,
    )

    assert "temporary file service failure" in output
    assert event["exit_code"] == 1
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[0]["filePath"] == "/workspace/app.py"
    assert events[-1]["status"] == "error"


@pytest.mark.asyncio
async def test_write_tool_returns_five_line_diff(monkeypatch):
    from app.agent.coder import tooling

    calls = []

    read_count = 0

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        nonlocal read_count
        calls.append((action, payload))
        if action == "files/read":
            read_count += 1
            return {
                "content": (
                    "old\nvalue\n"
                    if read_count == 1
                    else "new\nvalue\nextra\n"
                )
            }
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"exists": True, "size": len("old\nvalue\n".encode())}
        if action == "exec":
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {"written": 10}

    monkeypatch.setattr(tooling.sandbox_host, "call", fake_call)
    sandbox = SimpleNamespace(workspace_quota_bytes=1024)
    _, event = await tooling.dispatch_tool(
        sandbox,
        "write_file",
        {"path": "main.py", "content": "new\nvalue\nextra\n"},
    )
    assert any(action == "files/write" for action, _payload in calls)
    assert len(event["diff"].splitlines()) <= 5
    assert event["file_content"] == "new\nvalue\nextra\n"


@pytest.mark.asyncio
async def test_write_tool_repairs_python_indentation_before_upload(monkeypatch):
    from app.agent.coder import tooling

    written = None
    reads = 0

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        nonlocal written, reads
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"exists": False, "size": 0}
        if action == "files/write":
            written = payload["content"]
            return {"written": len(written)}
        if action == "files/read":
            reads += 1
            if reads == 1:
                raise FileNotFoundError
            return {"content": written}
        if action == "exec":
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        raise AssertionError(action)

    monkeypatch.setattr(tooling.sandbox_host, "call", fake_call)
    malformed = 'def client():\n     """Client."""\n    return 1\n'
    await tooling.dispatch_tool(
        SimpleNamespace(workspace_quota_bytes=1024),
        "write_file",
        {"path": "/workspace/test_app.py", "content": malformed},
    )
    assert '    """Client."""' in written
    compile(written, "<test>", "exec")


@pytest.mark.asyncio
async def test_write_tool_repairs_multiple_off_grid_indent_widths(monkeypatch):
    from app.agent.coder import tooling

    written = None
    reads = 0

    async def fake_call(action, _sandbox, payload=None, **_kwargs):
        nonlocal written, reads
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"exists": False, "size": 0}
        if action == "files/write":
            written = payload["content"]
            return {"written": len(written)}
        if action == "files/read":
            reads += 1
            if reads == 1:
                raise FileNotFoundError
            return {"content": written}
        if action == "exec":
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        raise AssertionError(action)

    monkeypatch.setattr(tooling.sandbox_host, "call", fake_call)
    malformed = (
        'def initialize():\n'
        '      """Initialize."""\n'
        "     global engine\n"
        "      # Build the engine.\n"
        "     engine = object()\n"
    )
    await tooling.dispatch_tool(
        SimpleNamespace(workspace_quota_bytes=1024),
        "write_file",
        {"path": "/workspace/main.py", "content": malformed},
    )
    compile(written, "<test>", "exec")
    assert '    """Initialize."""' in written
    assert "    global engine" in written


@pytest.mark.asyncio
async def test_write_tool_rejects_invalid_python_before_replacing_file(monkeypatch):
    from app.agent.coder import tooling

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("invalid Python must not reach the file service")

    monkeypatch.setattr(tooling.sandbox_host, "call", unexpected_call)
    output, event = await tooling.dispatch_tool(
        SimpleNamespace(workspace_quota_bytes=1024),
        "write_file",
        {"path": "/workspace/main.py", "content": "def broken(:\n    pass\n"},
    )
    assert "rejected before replacing" in output
    assert event["exit_code"] == 1
    assert event["corrective"] is True


def test_general_code_exec_rejects_workspace_application_builds():
    from app.agent.tools.code_exec import CodeExecTool

    _stdout, stderr, exit_code = CodeExecTool()._run_code(
        "from fastapi import FastAPI\nopen('/workspace/main.py', 'w')", "python"
    )
    assert exit_code == 1
    assert "delegate_to_coder" in stderr


def test_write_file_stops_when_parent_directory_cannot_be_created():
    class Container:
        status = "running"

        def exec_run(self, command, **_kwargs):
            if command[:3] == ["realpath", "-m", "--"]:
                return SimpleNamespace(
                    exit_code=0, output=(b"/workspace/new/file.txt\n", b"")
                )
            return SimpleNamespace(exit_code=1, output=(b"", b"permission denied"))

        def put_archive(self, *_args, **_kwargs):
            raise AssertionError("archive write must not run after mkdir failure")

    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: Container()
    spec = MODULE.SandboxSpec("id", "volume", "container", "image", 1, 256)
    with pytest.raises(RuntimeError, match="Could not create parent directory"):
        runtime.write_file(spec, "/workspace/new/file.txt", b"hello")


def test_python_write_is_owned_by_workspace_user_and_formatted():
    class Container:
        status = "running"

        def __init__(self):
            self.archive = None
            self.commands = []

        def exec_run(self, command, **_kwargs):
            self.commands.append(command)
            if command[:3] == ["realpath", "-m", "--"]:
                return SimpleNamespace(
                    exit_code=0, output=(b"/workspace/app.py\n", b"")
                )
            return SimpleNamespace(exit_code=0, output=(b"", b""))

        def put_archive(self, _parent, archive):
            self.archive = archive
            return True

    container = Container()
    runtime = object.__new__(MODULE.SandboxRuntime)
    runtime._container = lambda _name: container
    spec = MODULE.SandboxSpec("id", "volume", "container", "image", 1, 256)

    runtime.write_file(spec, "/workspace/app.py", b"x=1\n")

    with MODULE.tarfile.open(fileobj=MODULE.io.BytesIO(container.archive)) as archive:
        member = archive.getmember("app.py")
        assert (member.uid, member.gid) == (1000, 1000)
    assert [
        "autopep8",
        "--in-place",
        "--aggressive",
        "--",
        "/workspace/app.py",
    ] in container.commands


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
