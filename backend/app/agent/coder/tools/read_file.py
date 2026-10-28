"""read file tool: schema and execution."""

import json
import hashlib
from app.services import sandbox_host
from app.agent.coder.tools.common import MAX_READ_CHARS
from app.agent.coder.tools.common import MAX_READ_LINES
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "read_file",
    "Read exact source text. Small files are returned whole. Optional start_line/end_line are 1-based inclusive LINE numbers, not characters. Large files return complete lines and next_line; continue from there.",
    {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
    },
    ("path",),
)


def _read_page(args: dict, content: str) -> tuple[dict, int]:
    lines = content.splitlines(keepends=True)
    start = args.get("start_line", 1)
    end = args.get("end_line", max(1, len(lines)))
    if (
        type(start) is not int
        or type(end) is not int
        or start < 1
        or end < start
        or start > max(1, len(lines))
    ):
        raise ValueError(
            f"Use 1-based inclusive start_line/end_line. This file has {len(lines)} lines; "
            "omit both to read from the beginning."
        )
    if "offset" in args or "limit" in args:
        raise ValueError(
            "read_file now uses start_line/end_line (1-based lines), not offset/limit characters."
        )
    offset = sum(len(line) for line in lines[: start - 1])
    selected, size = [], 0
    for line in lines[start - 1 : min(end, start + MAX_READ_LINES - 1)]:
        if size + len(line) > MAX_READ_CHARS:
            if not selected:
                raise ValueError(
                    f"Line {start} exceeds the {MAX_READ_CHARS}-character read budget. "
                    "Use search_files for a focused excerpt or edit_file with a unique exact match; "
                    "no source text was silently truncated."
                )
            break
        selected.append(line)
        size += len(line)
    last = start - 1 + len(selected)
    return {
        "path": args["path"],
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "start_line": start,
        "end_line": last,
        "total_lines": len(lines),
        "total_chars": len(content),
        "content": "".join(selected),
        "next_line": last + 1 if last < len(lines) else None,
    }, offset


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    result = await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
    content = result.get("content", "")
    page, offset = _read_page(args, content)
    return json.dumps(page, ensure_ascii=False), {
        "read": {**page, "offset": offset},
        "file_content": page["content"],
    }
