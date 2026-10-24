"""One bounded plan turn before work or deterministic dialogue archival."""

from dataclasses import dataclass, replace

from app.agent.coder.tools.update_plan import SCHEMA
from app.services import agent_settings, context_compactor

PLAN_TOOLS = [SCHEMA]


class PlanCheckpointRequired(Exception):
    """Pause protocol recovery before it archives previously unseen context."""


@dataclass
class PlanCheckpoints:
    initial_attempted: bool = False
    ready_for_compaction: bool = False
    requested: bool = False

    async def phase(self, memory, messages, model, tools, native_tools, compactor):
        if not self.initial_attempted:
            return "initial"
        if self.requested:
            return "before_compaction"
        if (
            self.ready_for_compaction
            or not agent_settings.effective().history_compaction
        ):
            return None
        # Probe the deterministic operation without modifying live memory or
        # emitting activity. Source/output pruning alone does not need an LLM
        # checkpoint; request one only when older dialogue would be archived.
        probe = replace(memory)
        try:
            await probe.compact(
                messages,
                model,
                compactor=compactor,
                tools=tools,
                native_tools=native_tools,
            )
        except context_compactor.ContextBudgetExceeded:
            # A small-schema/low-output checkpoint may still fit, even when the
            # full coding request cannot. Never truncate context just to run it.
            return "before_compaction"
        return "before_compaction" if probe.compactions > memory.compactions else None

    def attempted(self):
        # One attempt, successful or not. No checkpoint retry loop.
        self.initial_attempted = True
        self.ready_for_compaction = True
        self.requested = False

    def compacted(self):
        self.ready_for_compaction = False


async def fit_checkpoint(
    messages, model, *, tools=None, native_tools=True, max_output_tokens=None, **_kwargs
):
    """Fit the checkpoint's actual budget without archiving its input first."""
    size = context_compactor.estimate_request_tokens(
        context_compactor.model_messages(messages, native_tools), tools
    )
    if size > context_compactor.context_budget(model, max_output_tokens).input_limit:
        raise context_compactor.ContextBudgetExceeded(
            "Plan checkpoint does not fit; keeping the last valid plan and execution facts."
        )
    return messages, False
