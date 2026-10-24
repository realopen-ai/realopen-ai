"""Focused coding-agent orchestration backed by a persistent workspace."""

from __future__ import annotations

import json
import asyncio
import logging
import time
import uuid
from datetime import datetime

from sqlalchemy import select, text

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType
from app.agent.coder.provider import chat_with_tool_recovery, parse_calls
from app.agent.coder.working_state import RunState
from app.agent.coder.project import configuration_changed, project_snapshot
from app.agent.coder.context import bounded_output, tokens
from app.agent.coder.checkpoints import (
    PlanCheckpoints,
    PlanCheckpointRequired,
    PLAN_TOOLS,
    fit_checkpoint,
)
from app.agent.coder.handoff import agent_scaffold_result, bounded_contract
from app.agent.coder.execution_policy import (
    explicit_choices,
    scaffold_constraint_error,
    verification_allowed,
    baseline_commands,
)
from app.agent.validation import argument_errors, normalize_structured_arguments
from app.agent.coder.tooling import (
    CODER_TOOLS,
    dispatch_tool,
    infer_preview,
    normalize_tool_args,
    source_fingerprint,
)
from app.agent.tracing import observe, traced_coder_execution
from app.agent.protocol import (
    assistant_call_message,
    tool_result_message,
    normalize_native_calls,
)
from app.services.context_compactor import compact_conversation
from app.services.agent_activity import activity_events
from app.db.models import Conversation, Sandbox, SandboxTask
from app.db.session import async_session_factory
from app.prompts import get_prompt, format_prompt
from app.core.prompt_security import untrusted_context_message
from app.services import sandbox_host, tool_capabilities, coder_settings, agent_settings
from app.services import skills as skill_store
from app.services.model_prefs import resolve_task_model
from app.services.sandbox_commands import persist_command, split_command_output
from app.services.sandbox_manager import provision_sandbox

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    """Wall-clock timestamp for persisted/SSE tool events (epoch ms)."""
    return int(time.time() * 1000)


CODER_SYSTEM_PROMPT = get_prompt("coder_system")
COMMAND_TOOLS = {
    "run_command",
    "run_tests",
    "install_dependencies",
    "setup_python_project",
    "scaffold_project",
    "scaffold_crud_app",
    "check_preview",
    "run_checks",
    "run_browser_tests",
}
READ_TOOLS = {"list_files", "read_file", "search_files"}


async def _apply_plan_checkpoint(
    response, calls, *, state, sandbox, conversation, task_id, parent_id, event_queue
):
    """Validate one restricted turn; never execute siblings or repair in a loop."""
    normalized = normalize_native_calls(calls)
    truncated = response.get("done_reason", response.get("finish_reason")) in {
        "length",
        "max_tokens",
    }
    if not normalized:
        normalized = [{"tool": "update_plan", "args": {}, "raw_arguments": {}}]
    for item in normalized:
        name, args = item["tool"], item["args"]
        observe("begin_tool", name, item["raw_arguments"])
        if len(normalized) != 1 or name != "update_plan":
            errors = [
                "Only one update_plan call is allowed in this checkpoint; no implementation tool was executed."
            ]
        elif truncated:
            errors = [
                "Checkpoint response was truncated; the previous plan is unchanged."
            ]
        elif item.get("parse_error"):
            errors = [item["parse_error"]]
        else:
            errors = argument_errors(args, PLAN_TOOLS[0]["function"]["parameters"])
        if errors:
            error = "Plan checkpoint not saved: " + "; ".join(errors)
            observe("end_tool", "failed", error=error, validation_error=True)
            _rejected_step(
                state, name, args, error, event_queue, parent_id, task_id, sandbox.id
            )
            continue
        output, event = await _run_step(
            sandbox=sandbox,
            conversation=conversation,
            task_id=task_id,
            name=name,
            args=args,
            state=state,
            parent_id=parent_id,
            event_queue=event_queue,
        )
        _track_step(state, name, args, output, event)


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
            started_at=datetime.utcfromtimestamp(started_at / 1000),
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
    now = _now_ms()
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
    return event, {
        **event,
        "status": "completed",
        "completedAt": now,
        "durationMs": 0,
    }


def _event_for_step(
    name: str,
    args: dict,
    step_id: str,
    parent_id: str | None,
    sandbox_id=None,
) -> dict:
    event_type = (
        "skill"
        if name == "load_skill"
        else (
            "file_read"
            if name in READ_TOOLS
            else (
                "file_write"
                if name in {"write_file", "edit_file"}
                else "preview" if name == "start_preview" else "code_exec"
            )
        )
    )
    skill_resource = str(args.get("resource") or "SKILL.md")
    title = {
        "list_files": "Listing workspace files",
        "read_file": f"Reading {args.get('path', 'file')}",
        "write_file": f"Writing {args.get('path', 'file')}",
        "edit_file": f"Editing {args.get('path', 'file')}",
        "scaffold_project": "Creating project starter",
        "scaffold_crud_app": "Creating CRUD application",
        "check_preview": "Checking frontend/API connection",
        "search_files": f"Searching for {args.get('query', '')}",
        "run_tests": "Running tests",
        "run_checks": "Running verification checks",
        "run_browser_tests": "Running optional browser tests",
        "update_plan": "Saving coding continuation plan",
        "install_dependencies": "Installing dependencies",
        "run_command": "Running command",
        "setup_python_project": "Setting up Python project",
        "start_preview": "Starting app preview",
        "load_skill": (f"Loading skill {args.get('name', '')} · {skill_resource}"),
    }.get(name, name)
    event = {
        "id": step_id,
        "parentId": parent_id,
        "agent": "coder",
        "toolName": name,
        "type": event_type,
        "status": "running",
        "title": title,
        "startedAt": _now_ms(),
    }
    if sandbox_id is not None:
        event["sandboxId"] = str(sandbox_id)
    if name == "load_skill":
        event.update(
            {
                "skillName": str(args.get("name", "")),
                "skillResource": skill_resource,
            }
        )
    if name in COMMAND_TOOLS | {"start_preview"}:
        if name == "setup_python_project":
            command = "uv sync --all-groups"
        elif name in {"run_checks", "run_browser_tests"}:
            # This also renders schema-rejected calls: arguments need not
            # contain a valid string array yet. Display, never execute them.
            entries = args.get("commands" if name == "run_checks" else "paths", [])
            separator = "\n" if name == "run_checks" else " "
            command = (
                separator.join(map(str, entries))
                if isinstance(entries, list)
                else str(entries)
            )
            if name == "run_browser_tests":
                command = "roai-browser-test " + command
        else:
            command = args.get("command", "")
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
    origin=None,
) -> tuple[str, dict | None]:
    state.step_index += 1
    args = normalize_tool_args(name, args)
    args = {key: value for key, value in args.items() if not key.startswith("_")}
    if name in {"write_file", "edit_file"}:
        args.update(state.read_arguments(args["path"]))
    start_event = _event_for_step(
        name,
        args,
        f"{parent_id or task_id}-step-{state.step_index}",
        parent_id,
        sandbox.id,
    )
    if origin:
        start_event["origin"] = origin
    project_event = None
    observe("tool_identity", start_event["id"])
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
    started_monotonic = time.monotonic()

    def setup_progress(update: dict) -> None:
        nonlocal command_started
        if event_queue is None:
            return
        if update.get("stage") == "check_completed":
            check = update["check"]
            event_queue.put_nowait(
                {
                    **start_event,
                    "title": f"Verification check {update['index'] + 1}/{len(args['commands'])} finished",
                    "output": json.dumps(
                        {
                            "command": check["command"],
                            "exit_code": check.get("exit_code"),
                            "result": check["output"],
                        }
                    )[:20_000],
                }
            )
        if update.get("stage") in {"files_written", "project_written"}:
            event_queue.put_nowait(
                {
                    "id": start_event["id"],
                    "sandboxId": str(sandbox.id),
                    "refreshFiles": True,
                }
            )
        if update.get("stage") == "project_written" and project_event:
            event_queue.put_nowait(
                {
                    **project_event,
                    "status": "completed",
                    "completedAt": _now_ms(),
                    "durationMs": round(
                        (time.monotonic() - started_monotonic) * 1000, 1
                    ),
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
            (
                setup_progress
                if name
                in {
                    "setup_python_project",
                    "scaffold_project",
                    "scaffold_crud_app",
                    "run_checks",
                }
                else None
            ),
        )
        if event is not None and (
            name in {"scaffold_project", "scaffold_crud_app", "setup_python_project"}
            or configuration_changed(event)
            and "project_facts" not in event
        ):
            event["project_facts"] = await project_snapshot(sandbox)
        if name in COMMAND_TOOLS:
            await _record_command(
                sandbox=sandbox,
                conversation_id=conversation.id,
                task_id=task_id,
                tool_name=name,
                sequence=state.step_index,
                command=str(
                    (event or {}).get("command") or start_event.get("code", "")
                ),
                output=output,
                exit_code=event.get("exit_code") if event else None,
                started_at=started_at,
            )
        completed = {
            **start_event,
            "status": "completed",
            "completedAt": _now_ms(),
            "durationMs": round((time.monotonic() - started_monotonic) * 1000, 1),
            "output": output[:20_000],
        }
        if event and event.get("command"):
            completed["code"] = event["command"]
        if (
            name in {"setup_python_project", "scaffold_project", "scaffold_crud_app"}
            or event
            and event.get("changed")
        ):
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
            if event.get("exit_code") not in (None, 0):
                completed["status"] = "error"
                completed["error"] = output
        if event_queue is not None:
            event_queue.put_nowait(completed)
        failed = event and event.get("exit_code") not in (None, 0)
        observe(
            "end_tool",
            "failed" if failed else "completed",
            output=output,
            error=output if failed else None,
            execution_args=args,
        )
        return output, event
    except Exception as exc:
        observe("end_tool", "failed", error=str(exc), execution_args=args)
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
                    "completedAt": _now_ms(),
                    "durationMs": round(
                        (time.monotonic() - started_monotonic) * 1000, 1
                    ),
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
    return state.record(name, normalize_tool_args(name, args), output, event)


def _rejected_step(state, name, args, error, queue, parent_id, task_id, sandbox_id):
    """Expose validation failures without pretending a sandbox action ran."""
    state.step_index += 1
    if queue is not None:
        event = _event_for_step(
            name,
            args,
            f"{parent_id or task_id}-step-{state.step_index}",
            parent_id,
            sandbox_id,
        )
        queue.put_nowait(
            {
                **event,
                "status": "error",
                "error": error,
                "output": error,
                "completedAt": _now_ms(),
                "durationMs": 0,
            }
        )
    return state.record(name, args, error, {"exit_code": 1})


async def _save_progress(task_id: uuid.UUID, state: RunState) -> None:
    async with async_session_factory() as db:
        record = await db.get(SandboxTask, task_id)
        record.worklog = state.worklog
        record.files_changed = state.files_changed
        record.tests_run = state.tests_run
        record.context_state = state.checkpoint()
        await db.commit()


async def _check_cancellation(task_id):
    async with async_session_factory() as db:
        current = await db.get(SandboxTask, task_id)
        if current and current.cancellation_requested:
            raise RuntimeError("Coding task cancelled")


async def _baseline_verification(
    *,
    generated,
    allowed,
    sandbox,
    conversation,
    task_id,
    state,
    parent_id,
    event_queue,
    messages,
):
    commands = baseline_commands(generated) if allowed else []
    if not commands:
        return ""
    args = {"commands": commands, "timeout": 300}
    item = {"id": "baseline-" + uuid.uuid4().hex, "tool": "run_checks", "args": args}
    # All preceding model-requested calls have matching results before this
    # separate application-owned exchange is appended (native and fallback).
    messages.append(
        assistant_call_message(get_prompt("coder_baseline_verification"), [item])
    )
    observe("begin_tool", "run_checks", args, origin="application")
    await _check_cancellation(task_id)
    output, event = await _run_step(
        sandbox=sandbox,
        conversation=conversation,
        task_id=task_id,
        name="run_checks",
        args=args,
        state=state,
        parent_id=parent_id,
        event_queue=event_queue,
        origin="application",
    )
    reason = _track_step(state, "run_checks", args, output, event)
    messages.append(
        tool_result_message(
            item,
            bounded_output(output, state.memory.budgets()["output"]),
            application_feedback=bool(event and event.get("corrective")),
        )
    )
    return reason


class CoderAgent(BaseTool):
    name = "delegate_to_coder"
    display_name = "Workspace coding agent"
    description = (
        "Delegate a substantial coding or file task to a focused agent in "
        "the conversation's persistent sandbox workspace. Report its verification "
        "levels accurately: builds, API checks and simulated DOM tests are not real-browser verification."
    )
    tool_type = ToolType.SANDBOX

    def get_parameters(self) -> dict:
        return {
            "task": {
                "type": "string",
                "description": "Faithful coding brief: preserve explicit user requirements. Label suggestions as optional; do not invent required ports, storage, frameworks or other constraints.",
            }
        }

    def get_required_params(self) -> list[str]:
        return ["task"]

    @coder_settings.with_snapshot
    @traced_coder_execution
    async def execute(
        self,
        *,
        task: str,
        conversation_id: str | None = None,
        _event_queue=None,
        _parent_tool_call_id: str | None = None,
        _user_request: str | None = None,
        **kwargs,
    ) -> ToolResult:
        if not task or not task.strip():
            return self.invalid_arguments("Provide a concrete coding task.")
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
        state = None
        try:
            conversation, sandbox = await _create_task(conversation_id, task_id, task)
            observe("coder_identity", task_id, sandbox.id)
            runtime = await sandbox_host.call("start", sandbox)
            sandbox.status = runtime.get("status", "running")
            sandbox.desired_running = True
            sandbox.error = None
            if _event_queue is not None:
                for event in _workspace_events(sandbox, _parent_tool_call_id, task_id):
                    _event_queue.put_nowait(event)
            model = await resolve_task_model("coder")
            observe("resolved_model", model)
            coder_prompt = CODER_SYSTEM_PROMPT
            # Refresh unchanged bundled defaults before taking the skill catalog.
            # A failed update is diagnostic, not a reason to lose the task.
            try:
                await asyncio.to_thread(skill_store.install_builtin_skills)
            except (OSError, ValueError) as error:
                logger.warning("Could not refresh bundled skills: %s", error)
            skill_catalog = skill_store.routing_catalog("coder")
            if skill_catalog:
                skill_context = untrusted_context_message(
                    "available coder skill metadata", skill_catalog
                )
                skill_context["metadata"]["agent_context"] = True
            messages = [
                {"role": "system", "content": coder_prompt},
                *([skill_context] if skill_catalog else []),
                {
                    "role": "user",
                    "content": (
                        format_prompt(
                            "coder_delegation", user_request=_user_request, task=task
                        )
                        if _user_request
                        else task
                    ),
                    "metadata": {"agent_request": True},
                },
            ]
            authoritative_request = _user_request or task
            state = RunState(
                explicit_constraints=explicit_choices(authoritative_request)
            )
            automatic_verification = verification_allowed(authoritative_request)
            try:
                manifest = await sandbox_host.call(
                    "files/read", sandbox, {"path": "/workspace/.realopen-project.json"}
                )
                project = json.loads(manifest.get("content", ""))
                if (
                    isinstance(project, dict)
                    and {"backend", "frontend"} <= project.keys()
                    and any(project.get(role) for role in ("backend", "frontend"))
                    and all(
                        project.get(role) is None or isinstance(project.get(role), dict)
                        for role in ("backend", "frontend")
                    )
                ):
                    state.project = {
                        "backend": project.get("backend"),
                        "frontend": project.get("frontend"),
                    }
                    saved_handoff = project.get("handoff")
                    state.memory.project_handoff = (
                        saved_handoff
                        if isinstance(saved_handoff, dict)
                        and saved_handoff.get("schema_version") == 1
                        else None
                    )
                    state.restore_scaffold_metadata(
                        project.get("starter") or {},
                        await source_fingerprint(sandbox) or {},
                    )
            except Exception:
                # Existing non-starter repositories remain supported.
                pass
            state.project_facts = await project_snapshot(sandbox)
            transient_text_models = set()
            final = ""
            failed = False
            premature_finals = 0
            incomplete_generations = 0
            preferences = coder_settings.effective()
            plan_checkpoints = PlanCheckpoints()
            thinking_started = None

            async def compact_recovery_context(current, model, **kwargs):
                # Text-tool recovery adds its schema protocol to the real request.
                # Apply the same coder handoff and persist the resulting working
                # history, instead of falling back to the general chat summary.
                kwargs.pop("max_output_tokens", None)
                current = state.memory.refresh_context(
                    current,
                    model,
                    kwargs.get("tools"),
                    False,
                    state.context,
                )
                if await plan_checkpoints.phase(
                    state.memory,
                    current,
                    model,
                    kwargs.get("tools"),
                    False,
                    compact_conversation,
                ):
                    # Text fallback adds schema instructions that can cause new
                    # budget pressure. Pause recovery rather than archive first.
                    plan_checkpoints.requested = True
                    raise PlanCheckpointRequired()
                compacted, changed = await state.memory.compact(
                    current,
                    model,
                    compactor=compact_conversation,
                    **kwargs,
                )
                if changed:
                    messages[:] = [
                        item
                        for item in compacted
                        if not (item.get("metadata") or {}).get("coder_protocol")
                    ]
                    await _save_progress(task_id, state)
                return compacted, changed

            def on_thinking(token):
                nonlocal thinking_started
                if _event_queue is None:
                    return
                if token is None:
                    if thinking_started is not None:
                        _event_queue.put_nowait(
                            {
                                "event": "thinking_done",
                                "thinkingDuration": round(
                                    time.monotonic() - thinking_started, 3
                                ),
                            }
                        )
                        thinking_started = None
                    return
                if thinking_started is None:
                    thinking_started = time.monotonic()
                    _event_queue.put_nowait(
                        {
                            "event": "thinking_start",
                            "agent": "coder",
                            "model": model,
                            "round": _round + 1,
                            "parentId": _parent_tool_call_id,
                        }
                    )
                _event_queue.put_nowait({"event": "thinking", "thinking": token})

            def checkpoint_round(round_number: int) -> None:
                """Ask the parent chat stream to durably save this coder round.

                The event is application-owned and never enters the model
                transcript. Queue ordering guarantees that every thinking and
                tool event from the round is consumed before the checkpoint.
                """
                if _event_queue is not None and round_number > 0:
                    _event_queue.put_nowait(
                        {
                            "event": "persistence_checkpoint",
                            "agent": "coder",
                            "round": round_number,
                            "parentId": _parent_tool_call_id,
                        }
                    )

            observe("limits", preferences.max_rounds)
            last_round = 0
            for _round in range(preferences.max_rounds):
                # The previous iteration has fully completed, including all
                # of its tool calls. Persist it before beginning new work.
                if last_round:
                    checkpoint_round(last_round)
                last_round = _round + 1
                round_marker = state.round_marker()
                generated_event = None
                native_tools = (
                    model not in transient_text_models
                    and tool_capabilities.should_try_native(model)
                )
                messages = state.memory.refresh_context(
                    messages,
                    model,
                    CODER_TOOLS if native_tools else None,
                    native_tools,
                    state.context,
                )
                if state.read_only_rounds >= 2 and (
                    state.files_changed or state.project
                ):
                    # Recovery is a trusted application instruction, not an
                    # instruction hidden inside an untrusted source envelope.
                    messages = [
                        message
                        for message in messages
                        if not (message.get("metadata") or {}).get(
                            "coder_exploration_recovery"
                        )
                    ]
                    messages.append(
                        {
                            "role": "user",
                            "content": format_prompt(
                                "coder_exploration_recovery",
                                missing=", ".join(state.completion_missing())
                                or "Review remaining explicit requirements",
                                files=", ".join(list(state.reads)[-8:])
                                or "See project map",
                            ),
                            "metadata": {
                                "trusted": True,
                                "agent_context": True,
                                "coder_exploration_recovery": True,
                            },
                        }
                    )
                    state.recovery_sent = True
                    if agent_settings.effective().history_compaction:
                        messages = state.memory.discard_redundant(messages, model)
                await _check_cancellation(task_id)
                checkpoint_phase = await plan_checkpoints.phase(
                    state.memory,
                    messages,
                    model,
                    CODER_TOOLS if native_tools else None,
                    native_tools,
                    compact_conversation,
                )
                active_tools = PLAN_TOOLS if checkpoint_phase else CODER_TOOLS
                output_limit = (
                    min(
                        preferences.generation_limit,
                        state.memory.budgets(model)["handoff"],
                    )
                    if checkpoint_phase
                    else preferences.generation_limit
                )
                inference_messages = messages
                if checkpoint_phase:
                    inference_messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": get_prompt("coder_plan_checkpoint")
                            + "\nCheckpoint phase: "
                            + checkpoint_phase,
                            "metadata": {
                                "trusted": True,
                                "agent_context": True,
                                "coder_plan_checkpoint": checkpoint_phase,
                            },
                        },
                    ]

                async def compact_round(on_start):
                    if checkpoint_phase:
                        return messages, False
                    return await state.memory.compact(
                        messages,
                        model,
                        compactor=compact_conversation,
                        tools=CODER_TOOLS if native_tools else None,
                        native_tools=native_tools,
                        on_start=on_start,
                    )

                async for activity in activity_events(
                    compact_round,
                    "context_compaction",
                ):
                    if activity.get("event"):
                        if _event_queue is not None:
                            _event_queue.put_nowait(activity)
                    else:
                        messages, compacted = activity["result"]
                        if compacted:
                            # Save the accepted handoff before the next inference.
                            await _save_progress(task_id, state)
                observe(
                    "begin_round",
                    _round + 1,
                    model,
                    inference_messages if checkpoint_phase else messages,
                    active_tools if native_tools else None,
                    {t["function"]["name"] for t in active_tools},
                    [],
                    {
                        "think": False if checkpoint_phase else preferences.thinking,
                        "temperature": preferences.temperature,
                        "max_output_tokens": output_limit,
                    },
                )
                try:
                    if checkpoint_phase:
                        await fit_checkpoint(
                            inference_messages,
                            model,
                            tools=active_tools if native_tools else None,
                            native_tools=native_tools,
                            max_output_tokens=output_limit,
                        )
                    response = await chat_with_tool_recovery(
                        model,
                        inference_messages if checkpoint_phase else messages,
                        compact_context=(
                            fit_checkpoint
                            if checkpoint_phase
                            else compact_recovery_context
                        ),
                        transient_text_models=transient_text_models,
                        **(
                            {
                                "tools": active_tools,
                                "output_limit": output_limit,
                                "thinking": False,
                            }
                            if checkpoint_phase
                            else {}
                        ),
                        **(
                            {
                                "on_activity": _event_queue.put_nowait,
                                "on_thinking": on_thinking,
                            }
                            if _event_queue is not None
                            else {}
                        ),
                    )
                except Exception as error:
                    if isinstance(error, PlanCheckpointRequired):
                        # No fallback inference or implementation tool ran.
                        # The next bounded turn saves the plan before retrying.
                        continue
                    if not checkpoint_phase:
                        raise
                    await _check_cancellation(task_id)
                    plan_checkpoints.attempted()
                    observe("begin_tool", "update_plan", {}, origin="application")
                    observe("end_tool", "failed", error=str(error))
                    _rejected_step(
                        state,
                        "update_plan",
                        {},
                        "Plan checkpoint unavailable; keeping the last valid plan and execution facts. "
                        + str(error),
                        _event_queue,
                        _parent_tool_call_id,
                        task_id,
                        sandbox.id,
                    )
                    await _save_progress(task_id, state)
                    continue
                message = response.get("message", {})
                await _check_cancellation(task_id)
                observe(
                    "chunk",
                    {
                        "content": message.get("content", ""),
                        "thinking": message.get("thinking", ""),
                        "tool_calls": message.get("tool_calls", []),
                    },
                )
                calls = parse_calls(message)
                observe("parsed", calls, calls, message.get("content", ""))
                if checkpoint_phase:
                    plan_checkpoints.attempted()
                    await _apply_plan_checkpoint(
                        response,
                        calls,
                        state=state,
                        sandbox=sandbox,
                        conversation=conversation,
                        task_id=task_id,
                        parent_id=_parent_tool_call_id,
                        event_queue=_event_queue,
                    )
                    await _save_progress(task_id, state)
                    # The temporary checkpoint request/transcript is in the
                    # trace/UI. Only its validated plan enters working state;
                    # it must not displace the latest implementation feedback.
                    continue
                plan_checkpoints.compacted()
                if response.get("done_reason", response.get("finish_reason")) in {
                    "length",
                    "max_tokens",
                }:
                    incomplete_generations += 1
                    error = "Generation reached its output limit; no calls from this incomplete response were executed. Use smaller files or targeted edits and split work across rounds."
                    rejected = normalize_native_calls(calls)
                    if rejected:
                        messages.append(
                            assistant_call_message(
                                message.get("content", ""),
                                rejected,
                            )
                        )
                        for item in rejected:
                            observe("begin_tool", item["tool"], item["raw_arguments"])
                            observe(
                                "end_tool", "failed", error=error, validation_error=True
                            )
                            messages.append(
                                tool_result_message(
                                    item, error, application_feedback=True
                                )
                            )
                            _rejected_step(
                                state,
                                item["tool"],
                                item["args"],
                                error,
                                _event_queue,
                                _parent_tool_call_id,
                                task_id,
                                sandbox.id,
                            )
                    else:
                        messages.append({"role": "user", "content": error})
                    if incomplete_generations >= 2:
                        failed, final = True, error
                        break
                    continue
                incomplete_generations = 0
                if not calls:
                    final = message.get("content", "")
                    missing = state.completion_missing()
                    if not missing:
                        break
                    premature_finals += 1
                    if premature_finals >= 2:
                        failed, final = (
                            True,
                            "Coder output remains unverified: " + ", ".join(missing),
                        )
                        break
                    messages.extend(
                        [
                            {"role": "assistant", "content": final},
                            {
                                "role": "user",
                                "content": get_prompt("coder_validate_completion")
                                + "\nMissing: "
                                + ", ".join(missing),
                            },
                        ]
                    )
                    if _round + 1 == preferences.max_rounds:
                        failed, final = (
                            True,
                            "Coder output remains unverified: " + ", ".join(missing),
                        )
                    continue
                normalized_calls = normalize_native_calls(calls)
                premature_finals = 0
                messages.append(
                    assistant_call_message(
                        message.get("content", ""),
                        normalized_calls,
                    )
                )
                abort_reason = ""
                for call_index, item in enumerate(normalized_calls):
                    name, args = item["tool"], item["args"]
                    observe("begin_tool", name, item["raw_arguments"])
                    if item.get("parse_error"):
                        observe("end_tool", "failed", error=item["parse_error"])
                        messages.append(
                            tool_result_message(
                                item, item["parse_error"], application_feedback=True
                            )
                        )
                        abort_reason = _rejected_step(
                            state,
                            name,
                            {"raw": item["raw_arguments"]},
                            item["parse_error"],
                            _event_queue,
                            _parent_tool_call_id,
                            task_id,
                            sandbox.id,
                        )
                        if abort_reason:
                            break
                        continue
                    schema = next(
                        (
                            tool["function"]["parameters"]
                            for tool in CODER_TOOLS
                            if tool["function"]["name"] == name
                        ),
                        None,
                    )
                    errors = (
                        ["Unknown coder tool"]
                        if schema is None
                        else argument_errors(
                            normalize_structured_arguments(args, schema), schema
                        )
                    )
                    if errors:
                        output = (
                            "Invalid tool arguments; nothing was executed: "
                            + "; ".join(errors)
                        )
                        observe(
                            "end_tool", "failed", error=output, validation_error=True
                        )
                        messages.append(
                            tool_result_message(item, output, application_feedback=True)
                        )
                        abort_reason = _rejected_step(
                            state,
                            name,
                            args,
                            output,
                            _event_queue,
                            _parent_tool_call_id,
                            task_id,
                            sandbox.id,
                        )
                        if abort_reason:
                            break
                        continue
                    args = normalize_structured_arguments(args, schema)
                    constraint_error = scaffold_constraint_error(
                        name, args, state.explicit_constraints
                    )
                    if constraint_error:
                        observe(
                            "end_tool",
                            "failed",
                            error=constraint_error,
                            validation_error=True,
                        )
                        messages.append(
                            tool_result_message(
                                item, constraint_error, application_feedback=True
                            )
                        )
                        abort_reason = _rejected_step(
                            state,
                            name,
                            args,
                            constraint_error,
                            _event_queue,
                            _parent_tool_call_id,
                            task_id,
                            sandbox.id,
                        )
                        if abort_reason:
                            break
                        continue
                    await _check_cancellation(task_id)
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
                    if name == "scaffold_crud_app" and baseline_commands(event):
                        generated_event = event
                    agent_output = (
                        json.dumps(
                            bounded_contract(
                                event["api_contract"],
                                state.memory.budgets(model)["output"],
                                tokens,
                            ),
                            ensure_ascii=False,
                        )
                        if event and event.get("api_contract")
                        else (
                            agent_scaffold_result(output, event)
                            if name in {"scaffold_project", "scaffold_crud_app"}
                            else output
                        )
                    )
                    result_message = tool_result_message(
                        item,
                        (
                            agent_output
                            if name in {"load_skill", "read_file"}
                            else bounded_output(
                                agent_output,
                                state.memory.budgets(model)["output"],
                            )
                        ),
                        skill_guidance=name == "load_skill" and not event,
                        application_feedback=bool(event and event.get("corrective")),
                    )
                    result_message.setdefault("metadata", {})["coder_result"] = {
                        "tool": name,
                        "success": not bool(
                            event
                            and (
                                event.get("exit_code") not in (None, 0)
                                or event.get("corrective")
                            )
                        ),
                        "path": ((event or {}).get("read") or {}).get("path"),
                        "sha256": ((event or {}).get("read") or {}).get("sha256"),
                        "range": (
                            [event["read"]["offset"], len(event["read"]["content"])]
                            if event and event.get("read")
                            else None
                        ),
                    }
                    messages.append(result_message)
                    if abort_reason:
                        break
                if abort_reason:
                    # Every attempted native call needs a matching result,
                    # including siblings skipped after a validation/parsing
                    # failure exhausts the no-progress budget.
                    for skipped in normalized_calls[call_index + 1 :]:
                        observe(
                            "begin_tool",
                            skipped["tool"],
                            skipped["raw_arguments"],
                        )
                        observe(
                            "end_tool",
                            "skipped",
                            error="coder no-progress budget exhausted",
                        )
                        messages.append(
                            tool_result_message(
                                skipped,
                                "Skipped because the coder no-progress budget was exhausted.",
                            )
                        )
                if not abort_reason:
                    abort_reason = await _baseline_verification(
                        generated=generated_event,
                        allowed=automatic_verification,
                        sandbox=sandbox,
                        conversation=conversation,
                        task_id=task_id,
                        state=state,
                        parent_id=_parent_tool_call_id,
                        event_queue=_event_queue,
                        messages=messages,
                    )
                abort_reason = abort_reason or state.finish_round(round_marker)
                await _save_progress(task_id, state)
                if abort_reason:
                    failed, final = True, abort_reason
                    break
            else:
                failed, final = (
                    True,
                    f"Coder stopped after reaching its {preferences.max_rounds}-round safety limit.",
                )
                observe(
                    "finish",
                    "max_rounds_reached",
                    "coder round budget exhausted",
                    response=final,
                )

            # A terminal round has no following iteration to checkpoint it.
            checkpoint_round(last_round)

            if not failed and state.successful_validation and not state.preview_started:
                inferred = await infer_preview(sandbox)
                if inferred:
                    command, port = inferred
                    observe("end_round", "completed")
                    observe(
                        "begin_tool",
                        "start_preview",
                        {"command": command, "port": port},
                        origin="application",
                    )
                    output, event = await dispatch_tool(
                        sandbox, "start_preview", {"command": command, "port": port}
                    )
                    observe(
                        "end_tool",
                        "completed",
                        output=output,
                        execution_args={"command": command, "port": port},
                    )
                    if event and _event_queue is not None:
                        now = _now_ms()
                        _event_queue.put_nowait(
                            {
                                "id": f"{_parent_tool_call_id or task_id}-preview",
                                "parentId": _parent_tool_call_id,
                                "agent": "coder",
                                "toolName": "start_preview",
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
                        checkpoint_round(last_round)

            final = final + "\n\n" + state.verification_summary()
            await self._finish_task(task_id, state, final, failed)
            observe(
                "finish",
                "failed" if failed else "completed",
                "coder task finished",
                response=final if failed else None,
            )
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
                        "verification": state.verification_report(),
                    }
                ),
                call,
            )
        except asyncio.CancelledError:
            observe("finish", "cancelled", "coder execution cancelled")
            try:
                if state is not None:
                    await _save_progress(task_id, state)
                await self._fail_task(task_id, RuntimeError("Coding task cancelled"))
            except Exception:
                logger.warning("Could not save cancelled coder task", exc_info=False)
            raise
        except Exception as exc:
            if state is not None:
                try:
                    await _save_progress(task_id, state)
                except Exception:
                    logger.warning(
                        "Could not save interrupted coder progress", exc_info=False
                    )
            observe(
                "finish",
                "cancelled" if str(exc) == "Coding task cancelled" else "failed",
                str(exc),
            )
            try:
                await self._fail_task(task_id, exc)
            except Exception:
                logger.warning("Could not persist failed coder task", exc_info=False)
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
            record.context_state = state.checkpoint()
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
