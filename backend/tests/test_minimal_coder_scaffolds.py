"""Small starters stay small; advanced generation remains a separate capability."""

import json
import os
import subprocess
import sys
import socket
import time
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import provider, tooling
from app.agent.coder.scaffold_selection import scaffold_tools
from app.agent.coder.working_state import RunState
from app.config import settings
from app.services import coder_settings
from test_coder_reliability import workspace as workspace_fixture


@pytest.fixture
def workspace(monkeypatch):
    return workspace_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize(
    "window,advanced", [(40000, False), (99999, False), (100000, True), (131072, True)]
)
def test_scaffold_selection_uses_configured_context_not_model_name(
    monkeypatch, window, advanced
):
    monkeypatch.setattr(coder_settings, "_overrides", {"context_window": window})
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {})
    selected = scaffold_tools("anything-200k", tooling.CODER_TOOLS)
    assert (
        "scaffold_crud_app" in [t["function"]["name"] for t in selected]
    ) == advanced
    assert "scaffold_project" in [t["function"]["name"] for t in selected]
    assert len(tooling.CODER_TOOLS) == 17


def test_model_specific_context_override_controls_scaffold_choice(monkeypatch):
    monkeypatch.setattr(coder_settings, "_overrides", {"context_window": 131072})
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {"limited": 40000})
    assert all(
        t["function"]["name"] != "scaffold_crud_app"
        for t in scaffold_tools("limited", tooling.CODER_TOOLS)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,frontend",
    [
        ("fastapi", "react-vite"),
        ("fastapi", "none"),
        ("none", "react-vite"),
        ("fastapi", "vite"),
    ],
)
async def test_minimal_starter_contains_only_runnable_infrastructure(
    workspace, backend, frontend
):
    sandbox, files, _ = workspace
    output, event = await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {
            "name": "minimal",
            "backend": backend,
            "frontend": frontend,
        },
    )
    assert event["exit_code"] == 0
    assert len(files) <= 13
    source = {
        p: c for p, c in files.items() if not p.endswith(".realopen-project.json")
    }
    assert sum(len(c.encode()) for c in source.values()) < 6000
    assert not any(
        "/tests/" in p
        or ".test." in p
        or "/components/" in p
        or "/hooks/" in p
        or p.endswith(("db.py", "api.ts", "theme.css", "services.py", "repository.py"))
        for p in files
    )
    handoff = json.loads(output)["handoff"]
    assert handoff["application_generated"] is False
    assert "Not implemented" in handoff["persistence"]
    if backend != "none":
        assert "FastAPI" in files["/workspace/backend/app.py"]
        assert "/api/health" in files["/workspace/backend/app.py"]
    if frontend != "none":
        assert files["/workspace/frontend/src/style.css"] == '@import "tailwindcss";\n'
        assert '"/api"' in files["/workspace/frontend/vite.config.ts"]
        assert "tailwindcss()" in files["/workspace/frontend/vite.config.ts"]
    state = RunState()
    state.record("scaffold_project", {}, output, event)
    assert state.completion_missing()
    assert not state.validations


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_minimal_tool_selection_is_preserved_in_both_call_protocols(
    monkeypatch, native
):
    monkeypatch.setattr(coder_settings, "_overrides", {"context_window": 40000})
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {})
    monkeypatch.setattr(
        provider.tool_capabilities, "should_try_native", lambda _: native
    )
    selected = scaffold_tools("coder", tooling.CODER_TOOLS)
    chat = AsyncMock(return_value={"message": {"content": '{"final":"Done"}'}})
    monkeypatch.setattr(provider.providers, "chat_once", chat)
    await provider.chat_with_tool_recovery(
        "coder",
        [{"role": "user", "content": "Build an app"}],
        tools=selected,
        compact_context=AsyncMock(
            side_effect=lambda messages, model, **kw: (messages, False)
        ),
    )
    if native:
        assert chat.call_args.kwargs["tools"] == selected
    else:
        protocol = chat.call_args.args[1][-1]["content"]
        assert '"name":"scaffold_project"' in protocol
        assert '"name":"scaffold_crud_app"' not in protocol


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,frontend",
    [("fastapi", "react-vite"), ("none", "react-vite"), ("fastapi", "vite")],
)
async def test_minimal_frontends_typecheck_and_build_locally(
    workspace, tmp_path, backend, frontend
):
    modules = Path(__file__).resolve().parents[2] / "frontend/node_modules"
    if not (modules / ".bin/tsc").exists():
        pytest.skip("Local frontend build dependencies are not installed")
    sandbox, files, _ = workspace
    await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {"name": "minimal", "backend": backend, "frontend": frontend},
    )
    for path, content in files.items():
        target = tmp_path / path.removeprefix("/workspace/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (tmp_path / "frontend/node_modules").symlink_to(modules, target_is_directory=True)
    for executable, args in (("tsc", ["--noEmit"]), ("vite", ["build"])):
        completed = subprocess.run(
            [str(modules / ".bin" / executable), *args],
            cwd=tmp_path / "frontend",
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "NO_COLOR": "1"},
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.asyncio
async def test_minimal_health_endpoint_through_real_frontend_proxy(workspace, tmp_path):
    if os.environ.get("SCAFFOLD_TEST_LIVE_PROXY") != "1":
        pytest.skip("Opt-in local process/network smoke test")
    modules = Path(__file__).resolve().parents[2] / "frontend/node_modules"
    if not (modules / ".bin/vite").exists():
        pytest.skip("Local Vite is not installed")
    sandbox, files, _ = workspace
    await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {
            "name": "proxy-smoke",
            "backend": "fastapi",
            "frontend": "react-vite",
        },
    )

    def free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    backend_port, frontend_port = free_port(), free_port()
    for path, content in files.items():
        target = tmp_path / path.removeprefix("/workspace/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith("vite.config.ts"):
            # Only test port allocation changes; the actual proxy is exercised.
            content = content.replace("6969", str(backend_port))
        target.write_text(content)
    (tmp_path / "frontend/node_modules").symlink_to(modules, target_is_directory=True)
    processes = []
    try:
        for command, cwd in (
            (
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "backend.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(backend_port),
                ],
                tmp_path,
            ),
            (
                [
                    str(modules / ".bin/vite"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(frontend_port),
                ],
                tmp_path / "frontend",
            ),
        ):
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert all(p.poll() is None for p in processes), "A preview process exited"
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{frontend_port}/api/health", timeout=1
                ) as response:
                    assert json.load(response) == {"status": "ok"}
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Minimal frontend proxy did not reach the health endpoint")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
