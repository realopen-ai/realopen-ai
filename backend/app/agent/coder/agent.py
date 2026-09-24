"""Focused coding-agent orchestration backed by a persistent workspace."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType
from app.agent.coder.provider import chat_with_tool_recovery, parse_calls
from app.agent.coder.tooling import dispatch_tool, infer_preview, normalize_tool_args
from app.db.models import Conversation, Sandbox, SandboxTask
from app.db.session import async_session_factory
from app.prompts import get_prompt
from app.services import sandbox_host
from app.services.model_prefs import resolve_task_model
from app.services.sandbox_commands import persist_command, split_command_output
from app.services.sandbox_manager import provision_sandbox

logger = logging.getLogger(__name__)
CODER_SYSTEM_PROMPT = "\n\n".join(
    (get_prompt("coder_system"), get_prompt("coder_fastapi_reference"))
)
COMMAND_TOOLS = {
    "run_command",
    "run_tests",
    "install_dependencies",
    "setup_python_project",
}
READ_TOOLS = {"list_files", "read_file", "search_files"}


@dataclass
class RunState:
    worklog: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    tests_run: list[dict] = field(default_factory=list)
    writes_by_path: dict[str, int] = field(default_factory=dict)
    step_index: int = 0
    command_failures: int = 0
    preview_started: bool = False
    successful_validation: bool = False


async def _record_command(
    *,
    sandbox,
    conversation_id,
    task_id,
    tool_name,
    sequence,
    command,
    started_at: float,
    output="",
    exit_code=None,
    error: str = "",
) -> None:
    """Persist command history without making telemetry break the agent."""
    try:
        stdout, stderr = split_command_output(output, exit_code)
        if error:
            stderr = error
        await persist_command(
            sandbox_id=sandbox.id,
            conversation_id=conversation_id,
            task_id=task_id,
            source="coder_agent",
            tool_name=tool_name,
            sequence=sequence,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            started_at=datetime.utcfromtimestamp(started_at),
            completed_at=datetime.utcnow(),
        )
    except Exception as exc:
        logger.warning("Could not persist coder command history: %s", exc)


async def _create_task(conversation_id: str, task_id: uuid.UUID, request: str):
    async with async_session_factory() as db:
        conversation_uuid = uuid.UUID(conversation_id)
        conversation = await db.get(Conversation, conversation_uuid)
        if not conversation:
            raise ValueError("Conversation not found")
        # Serialize the check-and-create path so concurrent delegations cannot
        # provision two default workspaces for one conversation.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"conversation-sandbox:{conversation_uuid}"},
        )
        await db.refresh(conversation)
        if not conversation.sandbox_id:
            workspace_name = f"{conversation.title or 'Coder'} workspace"
            sandbox = await provision_sandbox(
                db, name=workspace_name, conversation=conversation
            )
        else:
            sandbox = await db.get(Sandbox, conversation.sandbox_id)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": str(sandbox.id)},
        )
        active = await db.scalar(
            select(SandboxTask).where(
                SandboxTask.sandbox_id == sandbox.id,
                SandboxTask.status.in_(["queued", "running"]),
            )
        )
        if active:
            raise ValueError("Another coding task is already active in this sandbox.")
        db.add(
            SandboxTask(
                id=task_id,
                sandbox_id=sandbox.id,
                conversation_id=conversation.id,
                request=request,
                status="running",
                worklog=[],
            )
        )
        await db.commit()
        return conversation, sandbox


def _workspace_events(sandbox, parent_id: str | None, task_id) -> tuple[dict, dict]:
    now = time.time()
    event = {
        "id": f"{parent_id or task_id}-workspace",
        "parentId": parent_id,
        "type": "sandbox",
        "status": "running",
        "title": "Workspace ready",
        "startedAt": now,
        "sandboxId": str(sandbox.id),
        "sandbox": {
            "id": str(sandbox.id),
            "name": sandbox.name,
            "status": sandbox.status,
            "desired_running": sandbox.desired_running,
            "cpu_limit": sandbox.cpu_limit,
            "memory_limit_mb": sandbox.memory_limit_mb,
            "workspace_quota_bytes": sandbox.workspace_quota_bytes,
            "usage_bytes": sandbox.usage_bytes,
            "idle_timeout_seconds": sandbox.idle_timeout_seconds,
            "error": sandbox.error,
        },
    }
    return event, {**event, "status": "completed", "completedAt": now}


def _event_for_step(
    name: str,
    args: dict,
    step_id: str,
    parent_id: str | None,
    sandbox_id=None,
) -> dict:
    event_type = (
        "file_read"
        if name in READ_TOOLS
        else (
            "file_write"
            if name == "write_file"
            else "preview" if name == "start_preview" else "code_exec"
        )
    )
    title = {
        "list_files": "Listing workspace files",
        "read_file": f"Reading {args.get('path', 'file')}",
        "write_file": f"Writing {args.get('path', 'file')}",
        "search_files": f"Searching for {args.get('query', '')}",
        "run_tests": "Running tests",
        "install_dependencies": "Installing dependencies",
        "run_command": "Running command",
        "setup_python_project": "Setting up Python project",
        "start_preview": "Starting app preview",
    }.get(name, name)
    event = {
        "id": step_id,
        "parentId": parent_id,
        "type": event_type,
        "status": "running",
        "title": title,
        "startedAt": time.time(),
    }
    if sandbox_id is not None:
        event["sandboxId"] = str(sandbox_id)
    if name in COMMAND_TOOLS | {"start_preview"}:
        command = (
            "uv sync --all-groups"
            if name == "setup_python_project"
            else args.get("command", "")
        )
        event.update({"language": "shell", "code": command})
    else:
        event["filePath"] = (
            "/workspace/pyproject.toml"
            if name == "setup_python_project"
            else args.get("path", "/workspace")
        )
    return event


async def _run_step(
    *,
    sandbox,
    conversation,
    task_id,
    name: str,
    args: dict,
    state: RunState,
    parent_id: str | None,
    event_queue,
) -> tuple[str, dict | None]:
    state.step_index += 1
    args = normalize_tool_args(name, args)
    start_event = _event_for_step(
        name,
        args,
        f"{parent_id or task_id}-step-{state.step_index}",
        parent_id,
        sandbox.id,
    )
    project_event = None
    command_started = name != "setup_python_project"
    if name == "setup_python_project":
        project_event = {
            **start_event,
            "id": f"{start_event['id']}-project",
            "type": "file_write",
            "title": "Writing /workspace/pyproject.toml",
            "filePath": "/workspace/pyproject.toml",
        }
        project_event.pop("language", None)
        project_event.pop("code", None)
        if event_queue is not None:
            event_queue.put_nowait(project_event)
    elif event_queue is not None:
        event_queue.put_nowait(start_event)
    started_at = start_event["startedAt"]

    def setup_progress(update: dict) -> None:
        nonlocal command_started
        if event_queue is None:
            return
        if update.get("stage") == "project_written" and project_event:
            event_queue.put_nowait(
                {
                    **project_event,
                    "status": "completed",
                    "completedAt": time.time(),
                    "output": "Created /workspace/pyproject.toml",
                    "fileContent": update.get("file_content", ""),
                }
            )
            event_queue.put_nowait(start_event)
            command_started = True

    try:
        output, event = await dispatch_tool(
            sandbox,
            name,
            args,
            setup_progress if name == "setup_python_project" else None,
        )
        if name in COMMAND_TOOLS:
            await _record_command(
                sandbox=sandbox,
                conversation_id=conversation.id,
                task_id=task_id,
                tool_name=name,
                sequence=state.step_index,
                command=str(start_event.get("code", "")),
                output=output,
                exit_code=event.get("exit_code") if event else None,
                started_at=started_at,
            )
        completed = {
            **start_event,
            "status": "completed",
            "completedAt": time.time(),
            "output": output[:20_000],
        }
        if name == "setup_python_project":
            completed["refreshFiles"] = True
        if name in READ_TOOLS:
            completed["fileContent"] = output[:20_000]
        if event:
            for source, target in (
                ("exit_code", "exitCode"),
                ("diff", "diff"),
                ("file_content", "fileContent"),
                ("preview_url", "previewUrl"),
                ("port", "previewPort"),
            ):
                if event.get(source) is not None:
                    completed[target] = event[source]
        if event_queue is not None:
            event_queue.put_nowait(completed)
        return output, event
    except Exception as exc:
        if name in COMMAND_TOOLS:
            await _record_command(
                sandbox=sandbox,
                conversation_id=conversation.id,
                task_id=task_id,
                tool_name=name,
                sequence=state.step_index,
                command=str(start_event.get("code", "")),
                started_at=started_at,
                error=str(exc),
            )
        if event_queue is not None:
            failed_event = start_event if command_started else project_event
            event_queue.put_nowait(
                {
                    **(failed_event or start_event),
                    "status": "error",
                    "completedAt": time.time(),
                    "error": str(exc),
                    "output": str(exc),
                }
            )
        command = str(start_event.get("code", ""))
        return f"Tool error: {exc}", {
            **({"command": command} if command else {}),
            "exit_code": 1,
        }


def _track_step(
    state: RunState, name: str, args: dict, output: str, event: dict | None
) -> str:
    state.worklog.append(
        {
            "tool": name,
            "args": args,
            "result": output[:2000],
            "at": datetime.utcnow().isoformat(),
        }
    )
    if event and event.get("file") and event["file"] not in state.files_changed:
        state.files_changed.append(event["file"])
    if event and event.get("preview_url"):
        state.preview_started = True
    if event and event.get("command"):
        state.tests_run.append(event)
        command = str(event["command"]).lower()
        if event.get("exit_code") == 0 and (
            name == "run_tests"
            or "pytest" in command
            or "bun test" in command
            or "npm test" in command
        ):
            state.successful_validation = True
    if (
        event
        and event.get("exit_code") not in (None, 0)
        and not event.get("corrective")
    ):
        state.command_failures += 1
        if state.command_failures >= 4:
            return (
                "Coder stopped after four failed commands to avoid a retry loop. "
                "The last failure was: " + output[:500]
            )
    return ""


async def _save_progress(task_id: uuid.UUID, state: RunState) -> None:
    async with async_session_factory() as db:
        record = await db.get(SandboxTask, task_id)
        record.worklog = state.worklog
        record.files_changed = state.files_changed
        record.tests_run = state.tests_run
        await db.commit()


class CoderAgent(BaseTool):
    name = "delegate_to_coder"
    display_name = "Workspace coding agent"
    description = (
        "Delegate a substantial coding or file task to a focused agent in "
        "the conversation's persistent sandbox workspace."
    )
    tool_type = ToolType.SANDBOX

    def get_parameters(self) -> dict:
        return {
            "task": {
                "type": "string",
                "description": "Concrete coding task and acceptance criteria.",
            }
        }

    def get_required_params(self) -> list[str]:
        return ["task"]

    async def execute(
        self,
        *,
        task: str,
        conversation_id: str | None = None,
        _event_queue=None,
        _parent_tool_call_id: str | None = None,
        **kwargs,
    ) -> ToolResult:
        started = time.time()
        call = ToolCall(
            id=f"tc-coder-{int(started * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Coding in workspace",
            started_at=started,
        )
        if not conversation_id:
            return ToolResult(
                False,
                "A conversation must be linked to a sandbox before coding can be delegated.",
                call,
            )
        task_id = uuid.uuid4()
        try:
            conversation, sandbox = await _create_task(conversation_id, task_id, task)
            runtime = await sandbox_host.call("start", sandbox)
            sandbox.status = runtime.get("status", "running")
            sandbox.desired_running = True
            sandbox.error = None
            if _event_queue is not None:
                for event in _workspace_events(sandbox, _parent_tool_call_id, task_id):
                    _event_queue.put_nowait(event)
            model = await resolve_task_model("coder")
            messages = [
                {"role": "system", "content": CODER_SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ]
            state = RunState()
            final = ""
            failed = False
            for _round in range(24):
                async with async_session_factory() as db:
                    current = await db.get(SandboxTask, task_id)
                    if current and current.cancellation_requested:
                        raise RuntimeError("Coding task cancelled")
                message = (await chat_with_tool_recovery(model, messages)).get(
                    "message", {}
                )
                calls = parse_calls(message)
                if not calls:
                    final = message.get("content", "")
                    break
                messages.append(message)
                abort_reason = ""
                for item in calls:
                    fn = item.get("function", item)
                    name, args = fn.get("name", ""), fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if name == "write_file":
                        path = str(args.get("path", ""))
                        state.writes_by_path[path] = (
                            state.writes_by_path.get(path, 0) + 1
                        )
                        if state.writes_by_path[path] > 2:
                            messages.append(
                                {
                                    "role": "tool",
                                    "content": (
                                        f"Refused a third rewrite of {path}. Read the current "
                                        "file, run the narrowest test, and report a blocker."
                                    ),
                                }
                            )
                            continue
                    output, event = await _run_step(
                        sandbox=sandbox,
                        conversation=conversation,
                        task_id=task_id,
                        name=name,
                        args=args,
                        state=state,
                        parent_id=_parent_tool_call_id,
                        event_queue=_event_queue,
                    )
                    abort_reason = _track_step(state, name, args, output, event)
                    messages.append({"role": "tool", "content": output})
                    if abort_reason:
                        break
                await _save_progress(task_id, state)
                if abort_reason:
                    failed, final = True, abort_reason
                    break
            else:
                failed, final = (
                    True,
                    "Coder stopped after reaching its 24-round safety limit.",
                )

            if not failed and state.successful_validation and not state.preview_started:
                inferred = await infer_preview(sandbox)
                if inferred:
                    command, port = inferred
                    output, event = await dispatch_tool(
                        sandbox, "start_preview", {"command": command, "port": port}
                    )
                    if event and _event_queue is not None:
                        now = time.time()
                        _event_queue.put_nowait(
                            {
                                "id": f"{_parent_tool_call_id or task_id}-preview",
                                "parentId": _parent_tool_call_id,
                                "type": "preview",
                                "status": "completed",
                                "title": "Started app preview",
                                "startedAt": now,
                                "completedAt": now,
                                "language": "shell",
                                "code": command,
                                "output": output,
                                "previewUrl": event["preview_url"],
                                "previewPort": port,
                            }
                        )

            await self._finish_task(task_id, state, final, failed)
            call.status = "error" if failed else "completed"
            call.completed_at, call.output = time.time(), final
            return ToolResult(
                not failed,
                json.dumps(
                    {
                        "task_id": str(task_id),
                        "summary": final,
                        "files_changed": state.files_changed,
                        "commands": state.tests_run,
                    }
                ),
                call,
            )
        except Exception as exc:
            await self._fail_task(task_id, exc)
            call.status, call.completed_at, call.error = "error", time.time(), str(exc)
            return ToolResult(
                False,
                f"Coding task failed: {exc}. Do not call delegate_to_coder again "
                f"in this turn; report the failure and task id {task_id}.",
                call,
            )

    @staticmethod
    async def _finish_task(
        task_id: uuid.UUID, state: RunState, final: str, failed: bool
    ) -> None:
        async with async_session_factory() as db:
            record = await db.get(SandboxTask, task_id)
            record.status = "failed" if failed else "completed"
            record.summary, record.completed_at = final, datetime.utcnow()
            record.worklog = state.worklog
            record.files_changed = state.files_changed
            record.tests_run = state.tests_run
            await db.commit()

    @staticmethod
    async def _fail_task(task_id: uuid.UUID, exc: Exception) -> None:
        async with async_session_factory() as db:
            record = await db.get(SandboxTask, task_id)
            if record:
                record.status = (
                    "cancelled" if "cancel" in str(exc).lower() else "failed"
                )
                record.summary, record.completed_at = str(exc), datetime.utcnow()
                await db.commit()
