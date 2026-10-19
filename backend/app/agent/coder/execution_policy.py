"""Conservative request constraints and application-owned coder recovery actions."""

import re

from app.agent.coder.starters import BACKENDS, FRONTENDS
from app.agent.coder.styling import STYLING
from app.prompts import format_prompt


def explicit_choices(request):
    """Only unambiguous registered names in original prose become constraints.

    This is not a task router: ambiguous alternatives and negated mentions do
    not select a stack. Aliases live with each stack, not a task-specific regex.
    """
    prose = re.sub(r"```.*?(?:```|\Z)", "", request or "", flags=re.S)
    selected = {}
    for role, registry in (
        ("backend", BACKENDS),
        ("frontend", FRONTENDS),
        ("styling", STYLING),
    ):
        candidates = {}
        for name, config in registry.items():
            for alias in config.get("request_names", []):
                for match in re.finditer(
                    r"(?<!\w)" + re.escape(alias) + r"(?!\w|\.[a-z]|\s+native\b)",
                    prose,
                    re.I,
                ):
                    start = (
                        max(
                            prose.rfind("\n", 0, match.start()),
                            prose.rfind(";", 0, match.start()),
                        )
                        + 1
                    )
                    prefix = prose[start : match.start()].lower()
                    # Do not promote prohibitions, historical references or suggestions.
                    if re.search(
                        r"\b(?:no|not|don't|without|avoid|instead of|rather than|previously|formerly|for example)\b[^,;\n]{0,45}$",
                        prefix,
                    ):
                        continue
                    line = prose[
                        start : (
                            prose.find("\n", match.end())
                            if "\n" in prose[match.end() :]
                            else len(prose)
                        )
                    ]
                    if re.search(
                        r"\b(?:or|either|maybe|perhaps|optional|preferably)\b",
                        line,
                        re.I,
                    ):
                        continue
                    candidates[name] = line.strip()[:300]
        if len(candidates) == 1:
            value, evidence = next(iter(candidates.items()))
            selected[role] = {"value": value, "evidence": evidence}
    return selected


def scaffold_constraint_error(name, args, constraints):
    if name not in {"scaffold_project", "scaffold_crud_app"}:
        return ""
    conflicts = []
    for role, constraint in constraints.items():
        actual = args.get(
            role,
            (
                "react-vite"
                if role == "frontend"
                else "tailwind" if role == "styling" else None
            ),
        )
        if actual != constraint["value"]:
            conflicts.append(
                f"{role} must be {constraint['value']!r}, not {actual!r}. Original request: {constraint['evidence']}"
            )
    return (
        format_prompt("coder_stack_mismatch", conflicts="\n".join(conflicts))
        if conflicts
        else ""
    )


def verification_allowed(request):
    # Honor explicit opt-outs; ordinary build tasks may run isolated checks.
    for match in re.finditer(
        r"\b(?:do not|don't|never|skip|without running|run no)\s+(?:(?:run|execute|perform)\s+)?(?:(?:any|the|all|automated|baseline|backend|frontend|unit|integration|browser|e2e)\s+)*(?:tests?|checks?|verification|builds?|typechecks?)\b",
        request or "",
        re.I,
    ):
        if match.group().lower().startswith("skip") and re.search(
            r"\b(?:do not|don't|never)\s+$", request[: match.start()], re.I
        ):
            continue  # "Never skip tests" is not an opt-out.
        return False
    return True


def baseline_commands(event):
    """Only freshly generated stacks get automatic checks; never arbitrary manifests."""
    if (
        not event
        or event.get("exit_code") != 0
        or not (event.get("project_handoff") or {}).get("application_generated")
    ):
        return []
    return [
        config[key]
        for role, config in (event.get("project") or {}).items()
        if role in {"backend", "frontend"} and config
        for key in ("test", "typecheck", "build")
        if key in config
    ]
