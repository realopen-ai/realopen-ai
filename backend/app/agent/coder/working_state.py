"""Deterministic coder working facts and progress, separate from LLM summaries."""

import hashlib
import json
import posixpath
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from app.core.prompt_security import untrusted_context_message
from app.agent.coder.context import CoderMemory


def validation_kind(name, command):
    """Compatibility grouping for callers that do not need check granularity."""
    kinds = validation_categories(command)
    if kinds & {
        "backend_tests",
        "backend_behavior_tests",
        "frontend_tests",
        "frontend_behavior_tests",
        "tests",
    }:
        return "tests"
    if kinds & {"typecheck", "build"}:
        return "build"
    if "syntax" in kinds:
        return "syntax"
    return None


def validation_categories(command, project_facts=None):
    """Evidence describes commands that ran, not proof of application correctness.

    Ignore shell sequences that could hide an earlier failure. run_checks is
    the way to batch independent checks while retaining each outcome.
    """
    if "\n" in command:
        return set()
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return set()
    if any(word in {";", "||", "&"} for word in words):
        return set()
    parts, current = [], []
    for word in words:
        if word in {"&&", "|"}:
            parts.append(current)
            current = []
        else:
            current.append(word)
    parts.append(current)
    kinds = set()
    directory = ""
    for part in parts:
        if not part:
            continue
        executable = part[0].rsplit("/", 1)[-1]
        if executable == "cd" and len(part) == 2:
            directory = posixpath.normpath(posixpath.join(directory, part[1]))
            continue
        if executable in {"uv", "poetry"} and len(part) > 2 and part[1] == "run":
            part = part[2:]
            executable = part[0].rsplit("/", 1)[-1]
        if executable in {"python", "python3"}:
            if part[1:3] == ["-m", "pytest"] or part[1:3] == ["-m", "unittest"]:
                kinds.add("backend_tests")
            elif part[1:3] in (["-m", "py_compile"], ["-m", "compileall"]):
                kinds.add("syntax")
        elif executable == "pytest":
            kinds.add("backend_tests")
        elif executable in {"vitest", "jest"}:
            kinds.add("frontend_tests")
        elif executable == "tsc":
            kinds.add("typecheck")
        elif executable in {"npm", "bun", "pnpm", "yarn"}:
            # Read script bodies from current manifests, not starter defaults.
            script_words = part[1:]
            script_directory = directory
            if "--cwd" in script_words or "--prefix" in script_words:
                flag = "--cwd" if "--cwd" in script_words else "--prefix"
                index = script_words.index(flag)
                if index + 1 >= len(script_words):
                    continue
                script_directory = script_words[index + 1]
                script_words = script_words[:index] + script_words[index + 2 :]
            if script_words[:1] in (["run"], ["exec"], ["x"]):
                script_words = script_words[1:]
            if not script_words:
                continue
            script = script_words[0]
            if script in {"vitest", "jest"}:
                kinds.add("frontend_tests")
            elif script == "tsc":
                kinds.add("typecheck")
            else:
                normalized_directory = posixpath.normpath(script_directory)
                if normalized_directory in {".", "/workspace"}:
                    normalized_directory = ""
                manifest = (
                    normalized_directory.removeprefix("/workspace/").strip("/")
                    + "/package.json"
                ).lstrip("/")
                scripts = (
                    (project_facts or {})
                    .get("manifests", {})
                    .get(manifest, {})
                    .get("scripts", {})
                )
                body = scripts.get(script) if isinstance(scripts, dict) else None
                if isinstance(body, str) and body:
                    # Do not recurse into arbitrary package-script chains.
                    try:
                        head = shlex.split(body)[0]
                    except (ValueError, IndexError):
                        continue
                    if head in {"vitest", "jest", "tsc", "vite"}:
                        kinds.update(validation_categories(body))
                elif script == "build":
                    kinds.add("build")
        elif executable == "vite" and part[1:2] == ["build"]:
            kinds.add("build")
        elif executable == "node" and "--test" in part[1:]:
            kinds.add("tests")
        elif executable == "node" and "--check" in part[1:]:
            kinds.add("syntax")
        elif executable in {"go", "cargo"} and part[1:2] == ["test"]:
            kinds.add("tests")
        elif executable in {"go", "cargo"} and part[1:2] == ["build"]:
            kinds.add("build")
        elif executable == "make" and part[1:2] in (["test"], ["check"]):
            kinds.add("tests")
    return kinds


def _workspace_relative(path: object) -> str:
    return str(path).removeprefix("/workspace/").lstrip("/")


def _test_role(path: object) -> str | None:
    relative = _workspace_relative(path)
    name = posixpath.basename(relative)
    if relative.startswith("frontend/") and (
        "/tests/" in f"/{relative}" or ".test." in name or ".spec." in name
    ):
        return "frontend"
    if relative.startswith("backend/") and (
        "/tests/" in f"/{relative}"
        or name.startswith("test_")
        or name.endswith("_test.py")
    ):
        return "backend"
    return None


def _application_role(path: object) -> str | None:
    relative = _workspace_relative(path)
    if _test_role(path):
        return None
    if relative.startswith("frontend/src/"):
        return "frontend"
    if relative.startswith("backend/") and relative.endswith(".py"):
        return "backend"
    return None


@dataclass
class RunState:
    memory: CoderMemory = field(default_factory=CoderMemory)
    worklog: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    tests_run: list = field(default_factory=list)
    reads: dict = field(default_factory=dict)
    validations: dict = field(default_factory=dict)
    last_checks: dict = field(default_factory=dict)
    repeat_counts: dict = field(default_factory=dict)
    previews: set = field(default_factory=set)
    project: dict | None = None
    project_facts: dict | None = None
    revision: int = 0
    step_index: int = 0
    preview_started: bool = False
    successful_validation: bool = False
    unverified_changes: bool = False
    scaffolded_roles: set = field(default_factory=set)
    starter_source_hashes: dict = field(default_factory=dict)
    starter_test_hashes: dict = field(default_factory=dict)
    application_roles_changed: set = field(default_factory=set)
    application_test_roles_changed: set = field(default_factory=set)

    def restore_scaffold_metadata(self, starter: dict, current_hashes: dict) -> None:
        """Rebuild generated-app evidence after a later coder execution starts."""
        if not isinstance(starter, dict) or starter.get("schema_version") != 1:
            return
        source_hashes = starter.get("source_hashes")
        test_hashes = starter.get("test_hashes")
        if not isinstance(source_hashes, dict) or not isinstance(test_hashes, dict):
            return
        self.scaffolded_roles = {
            role
            for role in ("backend", "frontend")
            if self.project and self.project.get(role)
        }
        self.starter_source_hashes = dict(source_hashes)
        self.starter_test_hashes = dict(test_hashes)
        current_hashes = current_hashes or {}
        for path in self.starter_source_hashes.keys() | current_hashes.keys():
            role = _application_role(path)
            if role and current_hashes.get(path) != self.starter_source_hashes.get(
                path
            ):
                self.application_roles_changed.add(role)
        for path in self.starter_test_hashes.keys() | current_hashes.keys():
            role = _test_role(path)
            if role and current_hashes.get(path) != self.starter_test_hashes.get(path):
                self.application_test_roles_changed.add(role)

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
        self.memory.record(name, args, event)
        if "checks" in event:
            reason = ""
            for check in event["checks"]:
                reason = (
                    self.record(
                        "run_command",
                        {"command": check["command"]},
                        check["output"],
                        check,
                    )
                    or reason
                )
            return reason
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
        if name == "scaffold_project" and event.get("project"):
            self.scaffolded_roles = {
                role for role in ("backend", "frontend") if event["project"].get(role)
            }
            self.starter_test_hashes = dict(event.get("starter_test_hashes") or {})
            self.starter_source_hashes = dict(event.get("starter_source_hashes") or {})
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
            changed_paths = event.get("files", []) + (
                [event["file"]] if event.get("file") else []
            )
            for path in changed_paths:
                if path not in self.files_changed:
                    self.files_changed.append(path)
                self.reads.pop(path, None)
                if name != "scaffold_project":
                    test_role = _test_role(path)
                    application_role = _application_role(path)
                    if test_role:
                        self.application_test_roles_changed.add(test_role)
                    if application_role:
                        self.application_roles_changed.add(application_role)
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
        if "project_facts" in event:
            self.project_facts = event["project_facts"]
        if event.get("preview_url"):
            self.preview_started = True
            self.previews.add(event.get("port"))
        if event.get("command"):
            self.tests_run.append(event)
            categories = validation_categories(event["command"], self.project_facts)
            if name == "run_browser_tests":
                categories.add("browser")
            # A failed rerun supersedes earlier success even without a source edit.
            for category in categories:
                self.validations.pop(category, None)
                if category == "backend_tests":
                    self.validations.pop("backend_behavior_tests", None)
                if category == "frontend_tests":
                    self.validations.pop("frontend_behavior_tests", None)
                self.last_checks[category] = {
                    "command": event["command"],
                    "revision": self.revision,
                    "status": (
                        "failed" if event.get("exit_code") != 0 else "not_verified"
                    ),
                }
            if (
                event.get("exit_code") == 0
                and not event.get("changes_unknown")
                and not changed
            ):
                for category in categories:
                    if category == "browser" and not event.get("browser_result"):
                        continue
                    self.validations[category] = {
                        "command": event["command"],
                        "revision": self.revision,
                    }
                    self.last_checks[category].update(status="passed")
                if "backend_tests" in categories and (
                    "backend" not in self.scaffolded_roles
                    or "backend" in self.application_test_roles_changed
                ):
                    self.validations["backend_behavior_tests"] = {
                        "command": event["command"],
                        "revision": self.revision,
                    }
                if "frontend_tests" in categories and (
                    "frontend" not in self.scaffolded_roles
                    or "frontend" in self.application_test_roles_changed
                ):
                    self.validations["frontend_behavior_tests"] = {
                        "command": event["command"],
                        "revision": self.revision,
                    }
            self.successful_validation = bool(self.validations)
        if name == "run_browser_tests" and not event.get("browser_result"):
            self.validations.pop("browser", None)
        if name == "check_preview" and event.get("exit_code") != 0:
            self.validations.pop("integration", None)
            self.last_checks["integration"] = {
                "revision": self.revision,
                "status": "failed",
                "path": args.get("path"),
                "port": args.get("port"),
            }
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
                self.last_checks["integration"] = {
                    **self.validations["integration"],
                    "status": "passed",
                }
        self.successful_validation = bool(self.validations)
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
            unimplemented = self.scaffolded_roles - self.application_roles_changed
            if unimplemented:
                missing.append(
                    "application implementation beyond the starter ("
                    + ", ".join(sorted(unimplemented))
                    + ")"
                )
            if self.project.get("backend"):
                if "backend_tests" not in self.validations:
                    missing.append("backend tests")
                elif (
                    "backend" in self.scaffolded_roles
                    and "backend" in self.application_roles_changed
                    and "backend_behavior_tests" not in self.validations
                ):
                    missing.append("backend application behavior tests")
            if self.project.get("frontend"):
                for kind, label in [
                    ("frontend_tests", "frontend test runner"),
                    ("typecheck", "frontend typecheck"),
                    ("build", "frontend build"),
                ]:
                    if kind not in self.validations:
                        missing.append(label)
                if (
                    "frontend" in self.scaffolded_roles
                    and "frontend" in self.application_roles_changed
                    and "frontend_behavior_tests" not in self.validations
                ):
                    missing.append("frontend application DOM tests")
            if "integration" not in self.validations:
                missing.append("live preview smoke check")
            return missing
        return (
            []
            if self.successful_validation
            else ["relevant tests or syntax/build validation"]
        )

    def verification_report(self):
        return {
            "revision": self.revision,
            "checks": {
                kind: {
                    **self.last_checks.get(kind, {}),
                    "status": (
                        "passed"
                        if kind in self.validations
                        else (
                            self.last_checks[kind]["status"]
                            if self.last_checks.get(kind, {}).get("revision")
                            == self.revision
                            else "not_verified"
                        )
                    ),
                    **self.validations.get(kind, {}),
                }
                for kind in (
                    "backend_tests",
                    "backend_behavior_tests",
                    "frontend_tests",
                    "frontend_behavior_tests",
                    "typecheck",
                    "build",
                    "integration",
                    "browser",
                    "tests",
                    "syntax",
                )
            },
            "scope": "Command outcomes only; passing checks do not establish exhaustive coverage or correctness.",
            "generated_app_test_scope": {
                "starter_test_files": sorted(self.starter_test_hashes),
                "application_code_changed": sorted(self.application_roles_changed),
                "application_tests_changed": sorted(
                    self.application_test_roles_changed
                ),
            },
        }

    def verification_summary(self):
        labels = {
            "backend_tests": "backend test runner",
            "backend_behavior_tests": "backend application behavior tests",
            "frontend_tests": "frontend test runner",
            "frontend_behavior_tests": "frontend application DOM tests (not real-browser evidence)",
            "tests": "tests (runner scope unspecified)",
            "typecheck": "typecheck",
            "build": "build",
            "integration": "HTTP/API smoke check",
            "browser": "real-browser tests",
        }
        passed = [label for kind, label in labels.items() if kind in self.validations]
        return (
            "Verification of current files: "
            + (", ".join(passed) if passed else "none")
            + "."
            + (
                " Browser behavior was not verified."
                if "browser" not in self.validations
                else " Browser coverage is limited to the executed scripts."
            )
        )

    def context(self, model=None, *, source_limit=None):
        message = untrusted_context_message(
            "application coder working state",
            json.dumps(
                {
                    "revision": self.revision,
                    "continuation": {
                        "compactions": self.memory.compactions,
                        "loaded_resources": self.memory.resources,
                        "active_sources": self.memory.source_context(
                            model, source_limit
                        ),
                    },
                    "starter_commands_not_current_dependencies": {
                        role: {
                            key: value
                            for key, value in (
                                config if isinstance(config, dict) else {}
                            ).items()
                            if key in {"test", "typecheck", "build", "preview"}
                        }
                        for role, config in (self.project or {}).items()
                    },
                    "observed_project": self.project_facts,
                    "changed_files": self.files_changed,
                    "read_files": {
                        path: {
                            "sha256": item.get("sha256"),
                            "total_chars": item.get("total"),
                        }
                        for path, item in self.reads.items()
                    },
                    "current_validation": self.validations,
                    "generated_app_evidence": {
                        "scaffolded_roles": sorted(self.scaffolded_roles),
                        "application_code_changed": sorted(
                            self.application_roles_changed
                        ),
                        "application_tests_changed": sorted(
                            self.application_test_roles_changed
                        ),
                        "starter_tests_are_not_application_tests": sorted(
                            self.starter_test_hashes
                        ),
                    },
                    "source_tracking_unavailable": self.unverified_changes,
                    "previews": sorted(p for p in self.previews if p),
                    "recent_steps": [
                        {"tool": item["tool"], "result": item["result"][:600]}
                        for item in self.worklog[-3:]
                    ],
                },
                ensure_ascii=False,
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
