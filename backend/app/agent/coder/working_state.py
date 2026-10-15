"""Deterministic coder working facts and progress, separate from LLM summaries."""

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from app.core.prompt_security import untrusted_context_message


def validation_kind(name, command):
    """Classify checks, not a tool label or a word echoed by a shell."""
    try:
        words = shlex.split(command.lower())
    except ValueError:
        return None
    if not words or "||" in words or words[0] in {"echo", "printf", "true"}:
        return None
    text = " ".join(words)
    if re.search(r"(?:^|\s)(?:pytest|vitest|jest|unittest)(?:\s|$)", text) or re.search(
        r"(?:bun|npm|pnpm|yarn|go|cargo|make)\s+(?:run\s+)?test(?:\s|$)", text
    ):
        return "tests"
    if re.search(r"(?:^|\s)tsc(?:\s|$)", text) or re.search(
        r"(?:bun|npm|pnpm|yarn|make)\s+(?:run\s+)?(?:--cwd\s+\S+\s+)?build(?:\s|$)",
        text,
    ):
        return "build"
    if re.search(r"(?:py_compile|compileall|node --check)(?:\s|$)", text):
        return "syntax"
    return "tests" if name == "run_tests" else None


@dataclass
class RunState:
    worklog: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    tests_run: list = field(default_factory=list)
    reads: dict = field(default_factory=dict)
    validations: dict = field(default_factory=dict)
    repeat_counts: dict = field(default_factory=dict)
    previews: set = field(default_factory=set)
    project: dict | None = None
    revision: int = 0
    step_index: int = 0
    preview_started: bool = False
    successful_validation: bool = False
    unverified_changes: bool = False

    def read_arguments(self, path):
        receipt = self.reads.get(path, {})
        covered = 0
        for start, end in sorted(receipt.get("ranges", [])):
            if start > covered:
                break
            covered = max(covered, end)
        return {
            "_expected_sha256": receipt.get("sha256"),
            "_full_read": "total" in receipt and covered >= receipt["total"],
            "_read_ranges": receipt.get("ranges", []),
        }

    def record(self, name, args, output, event):
        event = event or {}
        self.worklog.append(
            {
                "tool": name,
                "args": {k: v for k, v in args.items() if not k.startswith("_")},
                "result": output[:2000],
                "at": datetime.utcnow().isoformat(),
            }
        )
        if "read" in event:
            page = event["read"]
            receipt = self.reads.setdefault(page["path"], {})
            if receipt.get("sha256") != page["sha256"]:
                receipt.clear()
                receipt.update(
                    sha256=page["sha256"], total=page["total_chars"], ranges=[]
                )
            receipt["ranges"].append(
                (page["offset"], page["offset"] + len(page["content"]))
            )
        changed = event.get(
            "changed", bool(event.get("file") and event.get("exit_code") in (None, 0))
        )
        if changed or event.get("changes_unknown"):
            if changed:
                self.revision += 1
            self.validations.clear()
            self.successful_validation = False
            if event.get("changes_unknown"):
                self.unverified_changes = True
                self.reads.clear()
            for path in event.get("files", []) + (
                [event["file"]] if event.get("file") else []
            ):
                if path not in self.files_changed:
                    self.files_changed.append(path)
                self.reads.pop(path, None)
        # A successful write establishes an exact snapshot too, including any
        # host-side formatting. Do not make the model reread its own output.
        if (
            event.get("file")
            and "file_content" in event
            and event.get("sha256")
            and event.get("exit_code") in (None, 0)
        ):
            content = event["file_content"]
            self.reads[event["file"]] = {
                "sha256": event["sha256"],
                "total": len(content),
                "ranges": [(0, len(content))],
            }
        if event.get("project"):
            self.project = event["project"]
        if event.get("preview_url"):
            self.preview_started = True
            self.previews.add(event.get("port"))
        if event.get("command"):
            self.tests_run.append(event)
            if event.get("exit_code") == 0 and not event.get("changes_unknown"):
                category = validation_kind(name, event["command"])
                if category:
                    self.validations[category] = {
                        "command": event["command"],
                        "revision": self.revision,
                    }
                    self.successful_validation = True
        if name == "check_preview" and event.get("exit_code") == 0:
            full_stack = (
                self.project
                and self.project.get("backend")
                and self.project.get("frontend")
            )
            path = args.get("path", "")
            if not full_stack or (
                args.get("port") == 6767
                and path.startswith("/api/")
                and {6767, 6969} <= self.previews
            ):
                self.validations["integration"] = {
                    "revision": self.revision,
                    "path": path,
                    "port": args.get("port"),
                }
        # Repeating the same action/output against unchanged files is no
        # progress. Different corrections and failures get their own budget.
        signature = hashlib.sha256(
            json.dumps(
                [self.revision, name, args, output], sort_keys=True, default=str
            ).encode()
        ).hexdigest()
        self.repeat_counts[signature] = self.repeat_counts.get(signature, 0) + 1
        if self.repeat_counts[signature] >= 3:
            return "Coder stopped after repeating the same action/result three times without file progress. Inspect a different source of evidence or report the concrete blocker."
        return ""

    def completion_missing(self):
        if not self.files_changed and not self.unverified_changes:
            return []
        if self.project:
            missing = []
            if self.project.get("backend") and "tests" not in self.validations:
                missing.append("backend tests")
            if self.project.get("frontend") and "build" not in self.validations:
                missing.append("frontend build")
            if "integration" not in self.validations:
                missing.append("live preview smoke check")
            return missing
        return (
            []
            if self.successful_validation
            else ["relevant tests or syntax/build validation"]
        )

    def context(self):
        message = untrusted_context_message(
            "application coder working state",
            json.dumps(
                {
                    "revision": self.revision,
                    "project": self.project,
                    "changed_files": self.files_changed,
                    "read_files": {
                        path: {
                            "sha256": item.get("sha256"),
                            "total_chars": item.get("total"),
                        }
                        for path, item in self.reads.items()
                    },
                    "current_validation": self.validations,
                    "source_tracking_unavailable": self.unverified_changes,
                    "previews": sorted(p for p in self.previews if p),
                    "recent_steps": [
                        {"tool": item["tool"], "result": item["result"][:600]}
                        for item in self.worklog[-3:]
                    ],
                }
            ),
        )
        message["metadata"].update(agent_context=True, agent_coder_state=True)
        return message


def compact_output(output, limit=6000):
    if len(output) <= limit:
        return output
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        data = None

    def excerpt(value):
        if not isinstance(value, str) or len(value) <= limit // 3:
            return value
        return (
            value[: limit // 6]
            + "\n[…output shortened; full output is in command history…]\n"
            + value[-limit // 6 :]
        )

    if isinstance(data, dict):
        compact = json.dumps(
            {key: excerpt(value) for key, value in data.items()}, ensure_ascii=False
        )
        if len(compact) <= limit:
            return compact
    return (
        output[: limit // 2]
        + "\n[…shortened; use read_file/search_files for exact content…]\n"
        + output[-limit // 3 :]
    )
