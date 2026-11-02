"""Measured coder outcomes, separate from model-authored completion prose."""


def execution_outcome(state, sandbox_id, *, failed, reason=None):
    reason = reason or (
        (state.stop_reason or "verification_incomplete") if failed else "completed"
    )
    guard = reason in {
        "repeated_available_evidence",
        "sustained_exploration",
        "repeated_action_without_progress",
        "max_rounds",
    }
    return {
        "status": "stopped" if guard else ("failed" if failed else "completed"),
        "stop_reason": reason,
        "sandbox_id": str(sandbox_id),
        "workspace_retained": True,
        "files_preserved": bool(state.files_changed),
        "files_changed": state.files_changed,
        "verification": state.verification_report(),
        "unfinished_plan_steps": [
            s
            for s in (state.memory.plan or {}).get("steps", [])
            if s.get("status") != "completed"
        ],
        "planned_next_action_not_executed": (state.memory.plan or {}).get(
            "next_action"
        ),
        "guidance": (
            "Execution stopped by an agent control guard, not a filesystem permission denial. "
            "Generated files remain in the sandbox; they are not document artifacts. "
            "Report preserved work, measured checks and unfinished requirements. "
            "Do not invent sandbox restrictions or repeat delegation in this turn."
            if guard
            else "Report measured verification separately from the model-authored plan and summary. "
            "Workspace files are in the sandbox, not the document artifact store."
        ),
    }
