"""edit file tool: schema and execution."""

import difflib
import json
import hashlib
from app.services import sandbox_host
from app.agent.coder.tools import write_file
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "edit_file",
    "Replace one unique exact old_text match with new_text (empty removes it). Prefer inspecting current source, but no exact read-range proof is required. Concurrent changes, missing or ambiguous matches leave the file unchanged.",
    {
        "path": {"type": "string"},
        "old_text": {"type": "string", "minLength": 1},
        "new_text": {"type": "string"},
    },
    ("path", "old_text", "new_text"),
)


def _match_feedback(content: str, old: str) -> str:
    positions, cursor = [], 0
    while old and len(positions) < 4:
        match = content.find(old, cursor)
        if match < 0:
            break
        positions.append(content.count("\n", 0, match) + 1)
        cursor = match + 1
    if positions:
        return (
            f"old_text matches at line(s) {positions}. "
            "Use read_file with start_line/end_line around the intended location. "
            "For multiple matches, include more surrounding text to identify exactly one."
        )
    first = next((line for line in old.splitlines() if line.strip()), "")
    lines = content.splitlines()
    similar = difflib.get_close_matches(first, lines, n=3, cutoff=0.6) if first else []
    # Corrective feedback is application-owned; do not promote source text
    # (which can contain instructions) into that trusted message.
    hints = sorted({lines.index(line) + 1 for line in similar})
    return (
        "old_text was not found verbatim. Read/search the current source and copy exact text. "
        + (
            "Similar lines to inspect with read_file (not replacements): "
            + json.dumps(hints)
            if hints
            else ""
        )
    )


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    content = (await sandbox_host.call("files/read", sandbox, {"path": args["path"]}))[
        "content"
    ]
    current_hash = hashlib.sha256(content.encode()).hexdigest()
    expected = args.get("_expected_sha256")
    if expected and current_hash != expected:
        return (
            f"{args['path']} changed since your last snapshot. No edit was applied. "
            "Read the current file, then retry against that version. "
            + _match_feedback(content, args["old_text"]),
            {"exit_code": 1, "corrective": True},
        )
    old, new = args["old_text"], args["new_text"]
    first = content.find(old) if old else -1
    if first < 0 or content.find(old, first + 1) >= 0:
        return (
            f"No edit was applied to {args['path']}. " + _match_feedback(content, old),
            {"exit_code": 1, "corrective": True},
        )
    return await write_file.execute(
        sandbox,
        {
            **args,
            "content": content.replace(old, new, 1),
            "_expected_sha256": current_hash,
            "_full_read": True,
        },
    )
