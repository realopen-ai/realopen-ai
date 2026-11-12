"""Task control and checkpoint continuity without real models or Docker."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.coder import agent, resume
from app.agent.coder.working_state import RunState
from app.api import coder_tasks as api
from app.db.session import get_db
from app.services import coder_tasks, sandbox_host
from app.services.chat_streams import start_stream
from test_agent_tracing import coder_setup, trace_setup, coder_responses  # noqa: F401


def record(**changes):
    return SimpleNamespace(
        **dict(
            dict(
                id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                sandbox_id=uuid.uuid4(),
                status="paused",
                request="Build app",
                summary="Paused",
                context_state=RunState().checkpoint(),
                worklog=[],
                files_changed=[],
                tests_run=[],
                created_at=datetime.utcnow() - timedelta(seconds=12),
                completed_at=None,
                progress={"round": 2, "max_rounds": 8},
                resumed_from=None,
                stop_after_round=False,
                cancellation_requested=False,
            ),
            **changes,
        )
    )


def test_checkpoint_roundtrip_retains_plan_but_invalidates_reads():
    state = RunState(original_request="Use SQLite and React")
    state.memory.plan = {
        "steps": [{"task": "Build UI", "status": "pending"}],
        "next_action": "Build UI",
    }
    state.project = {"backend": {"kind": "fastapi"}}
    state.files_changed = ["app.py"]
    state.scaffolded_roles = {"backend", "frontend"}
    state.observation_keys = {("file", "sha", 0, 20), "hash"}
    state.reads = {"file": {"sha256": "old"}}
    state.failures = {"tool": {"error": "Missing module"}}
    state.worklog = [{"tool": "run_tests", "args": {}, "result": "failed"}]
    saved = json.loads(json.dumps(state.checkpoint()))
    restored = RunState.restore(
        saved, worklog=state.worklog, files_changed=state.files_changed, tests_run=[]
    )
    assert restored.original_request == state.original_request
    assert restored.memory.plan == state.memory.plan
    assert restored.project == state.project
    assert restored.scaffolded_roles == state.scaffolded_roles
    assert restored.failures == state.failures
    assert restored.worklog == state.worklog
    assert (
        not restored.reads
        and not restored.memory.sources
        and not restored.available_reads
    )
    assert restored.files_changed == state.files_changed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrong_conversation",
        "running",
        "completed",
        "old_checkpoint",
        "superseded",
        "valid",
    ],
)
async def test_resume_admission(case):
    task = record()
    conv, latest = task.conversation_id, task.id
    if case == "wrong_conversation":
        conv = uuid.uuid4()
    if case in {"running", "completed"}:
        task.status = case
    if case == "old_checkpoint":
        task.context_state = {"schema_version": 1}
    if case == "superseded":
        latest = uuid.uuid4()
    db = SimpleNamespace(
        get=AsyncMock(return_value=task), scalar=AsyncMock(return_value=latest)
    )
    if case == "valid":
        assert await coder_tasks.resumable_task(db, task.id, conv) is task
    else:
        with pytest.raises(ValueError):
            await coder_tasks.resumable_task(db, task.id, conv)


def test_view_keeps_large_worklog_out_of_polling_and_marks_legacy_tasks():
    task = record(worklog=[{"result": "x" * 100000}])
    view = coder_tasks.task_view(task)
    assert "worklog" not in view and view["can_resume"]
    assert view["elapsed_seconds"] >= 12 and view["created_at"].endswith("Z")
    task.context_state = {"schema_version": 1}
    assert not coder_tasks.task_view(task)["can_resume"]
    with pytest.raises(ValueError):
        RunState.restore(task.context_state, worklog=[], files_changed=[], tests_run=[])


@pytest.mark.asyncio
@pytest.mark.usefixtures("coder_setup")
async def test_pause_waits_for_all_calls_in_current_round(monkeypatch):
    coder_responses(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "run_command",
                            "arguments": {"command": "python one.py"},
                        }
                    },
                    {
                        "function": {
                            "name": "run_command",
                            "arguments": {"command": "python two.py"},
                        }
                    },
                ]
            }
        ],
    )
    stops = iter([False, True])
    monkeypatch.setattr(
        agent, "_pause_requested", AsyncMock(side_effect=lambda _: next(stops))
    )
    result = await agent.CoderAgent().execute(
        task="Run two diagnostics", conversation_id="conv"
    )
    assert result.success, result.output
    assert agent.dispatch_tool.await_count == 2
    state = agent.CoderAgent._finish_task.call_args.args[1]
    assert state.stop_reason == "stop_after_round" and state.progress["round"] == 1
    assert json.loads(result.output)["execution_outcome"]["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.usefixtures("coder_setup")
async def test_resume_keeps_original_brief_and_plan(monkeypatch):
    state = RunState(original_request="Use SQLite. Preserve styling.")
    state.memory.plan = {"steps": [{"task": "Build UI", "status": "pending"}]}
    state.validations = {"tests": {"revision": 0}}
    state.last_checks = {"tests": {"revision": 0, "status": "passed"}}
    task = record(context_state=state.checkpoint())
    monkeypatch.setattr(agent, "resumable_task", AsyncMock(return_value=task))
    coder_responses(monkeypatch, [{"content": "Continued"}])
    result = await agent.CoderAgent().execute(
        task="ignored",
        conversation_id=str(task.conversation_id),
        _resume_task_id=str(task.id),
    )
    assert result.success, result.output
    restored = agent.CoderAgent._finish_task.call_args.args[1]
    assert (
        restored.original_request == state.original_request
        and restored.memory.plan == state.memory.plan
    )
    assert not restored.validations and not restored.last_checks
    assert agent._create_task.call_args.kwargs["resume_id"] == task.id


@pytest.mark.asyncio
async def test_resume_stream_cancels_child_and_forwards_nested_events(monkeypatch):
    cancelled = asyncio.Event()

    async def execute(**kwargs):
        kwargs["_event_queue"].put_nowait(
            {"event": "thinking_start", "agent": "coder", "round": 1}
        )
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(resume.CoderAgent, "execute", staticmethod(execute))
    stream = resume.resume_coder_stream("conv", "task")
    assert '"tool_call"' in await anext(stream)
    assert '"thinking_start"' in await anext(stream)
    await stream.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_restart_recovery(monkeypatch):
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(coder_tasks, "async_session_factory", session)
    await coder_tasks.recover_interrupted_tasks()
    assert db.execute.call_args.args[0].compile().params["status"] == "interrupted"
    db.commit.assert_awaited_once()


def test_controls_commit_flags_before_cancellation(monkeypatch):
    task = record(status="running")
    db = SimpleNamespace(scalar=AsyncMock(return_value=task), commit=AsyncMock())
    app = FastAPI()
    app.include_router(api.router, prefix="/api")

    async def database():
        yield db

    app.dependency_overrides[get_db] = database

    async def stop(_):
        db.commit.assert_awaited()

    monkeypatch.setattr(api, "stop_stream", stop)
    with TestClient(app) as client:
        url = f"/api/coder/tasks/{task.id}"
        query = {"conversation_id": str(task.conversation_id)}
        assert client.post(url + "/stop-after-round", params=query).json()[
            "stop_after_round"
        ]
        assert client.post(url + "/cancel", params=query).json()[
            "cancellation_requested"
        ]
        task.status = "paused"
        assert client.post(url + "/cancel", params=query).status_code == 409
        db.scalar.return_value = None
        assert client.post(url + "/cancel", params=query).status_code == 404
        assert client.post(url + "/other", params=query).status_code == 404


@pytest.mark.asyncio
async def test_resume_cannot_replace_an_active_stream():
    release = asyncio.Event()

    async def source():
        yield "data: ready\n\n"
        await release.wait()

    first = await start_stream("resume-admission", source())
    with pytest.raises(ValueError, match="already running"):
        await start_stream("resume-admission", source(), replace_existing=False)
    assert not first.cancel_requested
    release.set()
    await first.task


@pytest.mark.asyncio
async def test_cancelled_host_exec_cancels_matching_command(monkeypatch):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            requests.append((url, json))
            if url.endswith("/exec"):
                raise asyncio.CancelledError
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"cancelled": True}
            )

    monkeypatch.setattr(sandbox_host.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sandbox_host, "payload", lambda _: {"sandbox_id": "sandbox"})
    with pytest.raises(asyncio.CancelledError):
        await sandbox_host.call("exec", None, {"command": "python slow.py"})
    assert requests[1][0].endswith("/exec/cancel")
    assert requests[0][1]["command_id"] == requests[1][1]["command_id"]
