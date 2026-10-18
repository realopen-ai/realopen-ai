"""Bounded coder continuity: exact source excerpts and semantic task handoffs."""

import json
from dataclasses import dataclass, field

from app.services import agent_settings, coder_settings, context_compactor
from app.core.prompt_security import untrusted_context_message


def tokens(value):
    return context_compactor._estimate_tokens(
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    )


def prefix(text, limit):
    """Return an exact prefix within the same conservative estimate as budgeting."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if tokens(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def bounded_output(text, limit):
    if tokens(text) <= limit:
        return text
    marker = "\n[…older output shortened; full output remains in tool history…]\n"
    # The estimate rounds each fragment down independently; leave room for the join.
    available = max(0, limit - tokens(marker) - 3)
    return (
        prefix(text, available // 2) + marker + prefix(text[::-1], available // 2)[::-1]
    )


def fit_source(entry, limit):
    """Budget the serialized receipt, including escaped source and path metadata."""
    if tokens([entry]) <= limit:
        return entry
    content = entry["content"]
    entry = {**entry, "content": "", "partial": True}
    if tokens([entry]) > limit:
        return None
    low, high = 0, len(content)
    while low < high:
        middle = (low + high + 1) // 2
        if tokens([{**entry, "content": content[:middle]}]) <= limit:
            low = middle
        else:
            high = middle - 1
    excerpt = content[:low]
    if "\n" in excerpt:
        excerpt = excerpt[: excerpt.rfind("\n") + 1]
    entry["content"] = excerpt
    return entry


@dataclass
class CoderMemory:
    # Dict insertion order is recency order. Entries come only from tool receipts.
    sources: dict = field(default_factory=dict)
    resources: list = field(default_factory=list)
    handoff: str = ""
    compactions: int = 0
    active_sources: list | None = None
    project_handoff: dict | None = None
    api_contract: dict | None = None

    def budgets(self, model=None):
        preferences = coder_settings.effective()
        budget = context_compactor.context_budget(
            model or "", preferences.generation_limit
        )
        return {
            "handoff": min(
                preferences.handoff_tokens, max(128, budget.input_limit // 8)
            ),
            "sources": min(preferences.source_context_tokens, budget.input_limit // 6),
            "recent": min(preferences.recent_context_tokens, budget.input_limit // 3),
            "output": min(
                preferences.tool_output_tokens, max(128, budget.input_limit // 8)
            ),
        }

    def record(self, name, args, event):
        """Invalidate stale observations before retaining a new read/write receipt."""
        self.active_sources = None
        if event.get("changes_unknown"):
            self.sources.clear()
        if event.get("changed"):
            for path in [*event.get("files", []), event.get("file")]:
                self.sources.pop(path, None)
        if event.get("exit_code") not in (None, 0):
            return
        if name == "load_skill":
            resource = {
                "name": args.get("name"),
                "resource": args.get("resource") or "SKILL.md",
            }
            if resource not in self.resources:
                self.resources.append(resource)
        page = event.get("read") or {}
        path = page.get("path") or event.get("file")
        content = page.get("content", event.get("file_content"))
        digest = page.get("sha256") or event.get("sha256")
        if not path or not digest or not isinstance(content, str):
            return
        ceiling = self.budgets()["sources"]
        self.sources.pop(path, None)
        if not ceiling:
            self.sources.clear()
            return
        # A targeted edit keeps the modified area instead of always the file header.
        offset = int(page.get("offset", 0))
        line = int(page.get("start_line", 1))
        focus = args.get("new_text") if name == "edit_file" else None
        start = content.find(focus) if isinstance(focus, str) and focus else 0
        if start > 0 and tokens(content) > ceiling:
            start = content.rfind("\n", 0, max(0, start - 400)) + 1
        else:
            start = 0
        line += content[:start].count("\n")
        excerpt = prefix(content[start:], max(0, ceiling - 128))
        if len(excerpt) < len(content[start:]) and "\n" in excerpt:
            excerpt = excerpt[: excerpt.rfind("\n") + 1]
        self.sources[path] = {
            "path": path,
            "sha256": digest,
            "offset": offset + start,
            "start_line": line,
            "content": excerpt,
            "partial": bool(
                start or len(excerpt) < len(content) or page.get("next_line") or offset
            ),
        }
        fitted = fit_source(self.sources[path], max(128, ceiling // 2))
        if fitted is None:
            self.sources.pop(path)
        else:
            self.sources[path] = fitted
        while self.sources and tokens(list(self.sources.values())) > ceiling:
            self.sources.pop(next(iter(self.sources)))

    def source_context(self, model=None, limit=None):
        ceiling = self.budgets(model)["sources"]
        if limit is not None:
            ceiling = min(ceiling, limit)
        selected = []
        for item in reversed(list(self.sources.values())):
            if tokens([*selected, item]) <= ceiling:
                selected.append(item)
            elif not selected and ceiling > 128:
                shortened = fit_source(item, ceiling)
                if shortened is not None:
                    selected.append(shortened)
        self.active_sources = selected
        return selected

    def working_context(self, messages, model, tools, native_tools, build_state):
        """Source excerpts yield space to instructions, schemas and exact tool feedback."""
        bare = build_state(model, source_limit=0)
        latest_exchange = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if messages[i].get("tool_calls")
            ),
            len(messages),
        )
        fixed = [
            item
            for i, item in enumerate(messages)
            if (
                item.get("role") == "system"
                or i >= latest_exchange
                or (
                    (item.get("metadata") or {}).get("agent_context")
                    and not (item.get("metadata") or {}).get("historical_state")
                )
                or (item.get("metadata") or {}).get("agent_request")
            )
        ]
        cost = context_compactor.estimate_request_tokens(
            context_compactor.model_messages([*fixed, bare], native_tools), tools
        )
        available = (
            context_compactor.context_budget(model).input_limit
            - cost
            - self.budgets(model)["handoff"]
            - 128
        )
        return build_state(model, source_limit=max(0, available))

    def checkpoint(self):
        return {
            "schema_version": 1,
            "handoff": self.handoff,
            "compactions": self.compactions,
            "sources": list(self.sources.values()),
            "loaded_resources": self.resources,
            "project_handoff": self.project_handoff,
            "api_contract": self.api_contract,
        }

    def prune(self, messages, recent_tokens, output_tokens):
        """Prune expendable data without removing calls or their matching results."""
        current = any(
            (item.get("metadata") or {}).get("agent_coder_state")
            and not (item.get("metadata") or {}).get("historical_state")
            for item in messages
        )
        messages = [
            item
            for item in messages
            if not (
                current
                and (item.get("metadata") or {}).get("agent_coder_state")
                and (item.get("metadata") or {}).get("historical_state")
            )
        ]
        # Keep at least the latest complete exchange exact, even with a zero tail budget.
        last_exchange = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if messages[i].get("tool_calls")
            ),
            len(messages),
        )
        boundary, used = len(messages), 0
        for i in range(len(messages) - 1, -1, -1):
            used += context_compactor.estimate_request_tokens([messages[i]])
            if used > recent_tokens:
                break
            boundary = i
        boundary = min(boundary, last_exchange)
        result = []
        retained = {
            source["path"]: source
            for source in (
                self.active_sources
                if self.active_sources is not None
                else self.source_context()
            )
            if (self.sources.get(source["path"]) or {}).get("sha256")
            == source["sha256"]
        }
        for i, item in enumerate(messages):
            meta = item.get("metadata") or {}
            receipt = meta.get("coder_result") or {}
            if (
                i >= boundary
                or item.get("role") != "tool"
                or not receipt.get("success")
            ):
                result.append(item)
                continue
            content = str(item.get("content") or "")
            cached = retained.get(receipt.get("path"))
            repeated_read = (
                receipt.get("tool") == "read_file"
                and cached
                and (
                    cached["sha256"] == receipt.get("sha256") and not cached["partial"]
                )
            )
            if repeated_read:
                reference = untrusted_context_message(
                    "archived coder read",
                    json.dumps(
                        {
                            "path": cached["path"],
                            "sha256": cached["sha256"],
                            "source": "Exact content is in retained active_sources.",
                        }
                    ),
                )["content"]
                if tokens(reference) < tokens(content):
                    content = reference
            elif receipt.get("tool") not in {"load_skill", "read_file"}:
                if tokens(content) > output_tokens:
                    archived = untrusted_context_message(
                        "archived coder tool output",
                        bounded_output(content, output_tokens),
                    )["content"]
                    if tokens(archived) < tokens(content):
                        content = archived
            result.append({**item, "content": content})
        return result

    async def compact(
        self, messages, model, *, compactor, tools, native_tools, on_start=None
    ):
        budgets = self.budgets(model)
        limit = context_compactor.context_budget(model).input_limit
        soft_limit = int(limit * agent_settings.effective().compaction_threshold)
        size = lambda items: context_compactor.estimate_request_tokens(
            context_compactor.model_messages(items, native_tools), tools
        )
        pruned = messages
        if (
            agent_settings.effective().history_compaction
            and size(messages) > soft_limit
        ):
            pruned = self.prune(messages, budgets["recent"], budgets["output"])

        summaries = []

        async def summarize(text):
            summary = await context_compactor._summarize_bounded(
                model,
                text,
                max_tokens=budgets["handoff"],
                prompt_name="coder_compaction",
            )
            if summary:
                summaries.append(summary)
            return summary

        compacted, changed = await compactor(
            pruned,
            model,
            tools=tools,
            native_tools=native_tools,
            max_output_tokens=coder_settings.effective().generation_limit,
            on_start=on_start,
            summarize=summarize,
            recent_tokens=budgets["recent"],
        )
        if changed and summaries:
            # Only install notes from the accepted context, never a rejected sizing attempt.
            accepted = [
                summary
                for summary in summaries
                if any(
                    (item.get("metadata") or {}).get("compaction")
                    and summary in str(item.get("content", ""))
                    for item in compacted
                )
            ]
            if accepted:
                self.handoff = "\n\n".join(dict.fromkeys(accepted))
                self.compactions += 1
        pruned_changed = pruned != messages
        if pruned_changed and not changed and on_start:
            on_start()
        return compacted, changed or pruned_changed
