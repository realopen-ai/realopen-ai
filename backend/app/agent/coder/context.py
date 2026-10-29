"""Bounded coder continuity using tool receipts and deterministic checkpoints."""

import json
from dataclasses import dataclass, field

from app.services import agent_settings, coder_settings, context_compactor
from app.core.prompt_security import untrusted_context_message
from app.prompts import get_prompt


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
    api_contract_status: dict | None = None
    plan: dict | None = None
    observations: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)

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
            self.observations.clear()
        if event.get("changed"):
            for path in [*event.get("files", []), event.get("file")]:
                self.sources.pop(path, None)
                self.observations.pop(path, None)
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
        self.observations.pop(path, None)
        self.observations[path] = {
            "path": path,
            "sha256": digest,
            "start_line": page.get("start_line", 1),
            "end_line": page.get("end_line"),
            "next_line": page.get("next_line"),
        }
        # This inventory survives source-cache eviction, but is not an unbounded
        # second copy of the project. Omitted source remains in tool history.
        while (
            self.observations
            and tokens(list(self.observations.values())) > self.budgets()["handoff"]
        ):
            self.observations.pop(next(iter(self.observations)))
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
        selected = self._select_sources(ceiling)
        self.sources = {item["path"]: item for item in reversed(selected)}

    def _select_sources(self, ceiling):
        focus = [
            path if path.startswith("/workspace/") else "/workspace/" + path.lstrip("/")
            for path in (self.plan or {}).get("files", [])
        ]
        ordered = list(
            dict.fromkeys(
                [
                    *[p for p in focus if p in self.sources],
                    *reversed(self.sources),
                ]
            )
        )[:6]
        selected = []
        # Share the configured budget across the edit working set: a large
        # repository file must not evict every small component needed next.
        for index, path in enumerate(ordered):
            remaining = max(0, ceiling - tokens(selected) - 3)
            share = remaining // (len(ordered) - index)
            fitted = fit_source(self.sources[path], share)
            if fitted is not None and tokens([*selected, fitted]) <= ceiling:
                selected.append(fitted)
        return selected

    def source_context(self, model=None, limit=None):
        ceiling = self.budgets(model)["sources"]
        if limit is not None:
            ceiling = min(ceiling, limit)
        selected = self._select_sources(ceiling)
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
            - tokens(get_prompt("coder_compaction"))
            - 128
        )
        return build_state(model, source_limit=max(0, available))

    def checkpoint(self):
        return {
            "schema_version": 1,
            "compaction_mode": "deterministic",
            "handoff": self.handoff,
            "compactions": self.compactions,
            "sources": list(self.sources.values()),
            "loaded_resources": self.resources,
            "project_handoff": self.project_handoff,
            "api_contract": self.api_contract,
            "api_contract_status": self.api_contract_status,
            "plan": self.plan,
            "observations": list(self.observations.values()),
            "execution": self.execution,
        }

    def refresh_context(self, messages, model, tools, native_tools, build_state):
        """Keep one current snapshot, not a growing history of large snapshots.

        Static instructions, requests and assistant/tool exchanges stay ordered.
        Only application-owned derived state is replaced at the changing tail.
        """
        current = [
            item
            for item in messages
            if not (item.get("metadata") or {}).get("agent_coder_state")
        ]
        current.append(
            self.working_context(current, model, tools, native_tools, build_state)
        )
        return current

    def prune(self, messages, recent_tokens, output_tokens, *, source_pressure=False):
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
        newest_read = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if ((messages[i].get("metadata") or {}).get("coder_result") or {}).get(
                    "tool"
                )
                == "read_file"
                and ((messages[i].get("metadata") or {}).get("coder_result") or {}).get(
                    "success"
                )
            ),
            None,
        )
        result = []
        # An exact later read is also a safe replacement for an older duplicate,
        # even when its file was evicted from the bounded active-source cache.
        latest_reads = {}
        for index, message in enumerate(messages):
            receipt = (message.get("metadata") or {}).get("coder_result") or {}
            if (
                receipt.get("tool") == "read_file"
                and receipt.get("success")
                and receipt.get("path")
                and receipt.get("sha256")
                and receipt.get("range") is not None
                and not receipt.get("archived")
            ):
                latest_reads[
                    (receipt["path"], receipt["sha256"], tuple(receipt["range"]))
                ] = index
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
                (
                    i >= boundary
                    and not (
                        source_pressure
                        and receipt.get("tool") == "read_file"
                        and i != newest_read
                    )
                )
                or item.get("role") != "tool"
                or not receipt.get("success")
            ):
                result.append(item)
                continue
            content = str(item.get("content") or "")
            cached = retained.get(receipt.get("path"))
            duplicate = latest_reads.get(
                (
                    receipt.get("path"),
                    receipt.get("sha256"),
                    tuple(receipt.get("range") or []),
                )
            )
            repeated_read = (
                receipt.get("tool") == "read_file"
                and cached
                and (
                    cached["sha256"] == receipt.get("sha256") and not cached["partial"]
                )
            )
            if duplicate is not None and duplicate > i:
                reference = untrusted_context_message(
                    "archived coder read",
                    json.dumps(
                        {
                            "path": receipt["path"],
                            "sha256": receipt["sha256"],
                            "source": "Identical exact range is in the later read result: "
                            + str(messages[duplicate].get("tool_call_id")),
                        }
                    ),
                )["content"]
                if tokens(reference) < tokens(content):
                    content = reference
            elif repeated_read:
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
            elif (
                receipt.get("tool") == "read_file"
                and receipt.get("path")
                and receipt.get("sha256")
            ):
                reference = untrusted_context_message(
                    "archived coder read",
                    json.dumps(
                        {
                            "path": receipt["path"],
                            "sha256": receipt["sha256"],
                            "range": receipt.get("range"),
                            "source": "Previously inspected range archived from working context. Full result remains in history; use read_file for a specific missing region, not a project-wide reread.",
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
            replacement = {**item, "content": content}
            if receipt.get("tool") == "read_file" and content != item.get("content"):
                replacement["metadata"] = {
                    **meta,
                    "coder_result": {**receipt, "archived": True},
                }
            result.append(replacement)
        return result

    def discard_redundant(self, messages, model=None):
        """Deterministic cleanup before a paid summary during exploration recovery."""
        budgets = self.budgets(model)
        return self.prune(messages, budgets["recent"], budgets["output"])

    async def compact(
        self, messages, model, *, compactor, tools, native_tools, on_start=None
    ):
        budgets = self.budgets(model)
        limit = context_compactor.context_budget(model).input_limit
        soft_limit = int(limit * agent_settings.effective().compaction_threshold)

        def size(items):
            return context_compactor.estimate_request_tokens(
                context_compactor.model_messages(items, native_tools), tools
            )

        pruned = messages
        if (
            agent_settings.effective().history_compaction
            and size(messages) > soft_limit
        ):
            pruned = self.prune(messages, budgets["recent"], budgets["output"])
            if size(pruned) > limit:
                # A single parallel read batch can exceed the whole input
                # window. Preserve call/result pairing and all failed feedback,
                # but archive older successful read bodies within that batch.
                # The newest successful read remains exact; full outputs remain
                # in worklog/UI/traces and archived ranges can be read on demand.
                pruned = self.prune(
                    pruned, budgets["recent"], budgets["output"], source_pressure=True
                )

        checkpoint = get_prompt("coder_compaction")

        async def archive_history(_text):
            # Current structured state and exact recent exchanges stay protected;
            # never substitute a model-generated account of execution history.
            return checkpoint

        compacted, changed = await compactor(
            pruned,
            model,
            tools=tools,
            native_tools=native_tools,
            max_output_tokens=coder_settings.effective().generation_limit,
            on_start=on_start,
            summarize=archive_history,
            recent_tokens=budgets["recent"],
        )
        if changed and any(
            (item.get("metadata") or {}).get("compaction")
            and checkpoint in str(item.get("content", ""))
            for item in compacted
        ):
            self.handoff = checkpoint
            self.compactions += 1
        pruned_changed = pruned != messages
        if pruned_changed and not changed and on_start:
            on_start()
        return compacted, changed or pruned_changed
