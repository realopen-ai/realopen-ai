"""write file tool: schema and execution."""

import difflib
import json
import hashlib
from app.services import sandbox_host
from app.agent.coder.tools.common import MAX_INLINE_WRITE_RECEIPT_CHARS
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "write_file",
    "Create a file, or replace a complete file whose current contents you have read or just written. Prefer edit_file for targeted corrections.",
    {"path": {"type": "string"}, "content": {"type": "string"}},
    ("path", "content"),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    if str(args.get("path", "")).endswith("pyproject.toml"):
        return (
            "Direct pyproject.toml writes are disabled. Use setup_python_project "
            "for a new project or `uv add`/`uv remove` for an existing project.",
            {"exit_code": 1, "corrective": True},
        )
    content = str(args.get("content", ""))
    if str(args.get("path", "")).endswith(".py"):
        try:
            compile(content, str(args["path"]), "exec")
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return (
                f"Python write rejected before replacing the file: "
                f"{exc.__class__.__name__} at {location}: {exc.msg}. "
                "Correct the source and call write_file again.",
                {
                    "file": args["path"],
                    "exit_code": 1,
                    "corrective": True,
                },
            )
    previous = ""
    current = await sandbox_host.call("files/stat", sandbox, {"path": args["path"]})
    if current.get("exists"):
        if not args.get("_expected_sha256") or not args.get("_full_read"):
            return (
                "Read the complete current file before replacing it, or use edit_file after reading the relevant section. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        previous = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", "")
        if hashlib.sha256(previous.encode()).hexdigest() != args["_expected_sha256"]:
            return (
                "File changed; read_file again before editing. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        if previous == content:
            return "File already contains this content; nothing changed.", {
                "changed": False
            }
    usage = await sandbox_host.call("usage", sandbox)
    projected_usage = max(0, usage["usage_bytes"] - current["size"]) + len(
        content.encode()
    )
    if projected_usage > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded; stop and report this.", {"exit_code": 1}
    await sandbox_host.call(
        "files/write-checked",
        sandbox,
        {
            "path": args["path"],
            "content": content,
            "expected_sha256": (
                args.get("_expected_sha256") if current.get("exists") else "missing"
            ),
        },
    )
    written_content = content
    if str(args["path"]).endswith(".py"):
        # Checked host writes format/compile staging content before publication.
        written_content = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", content)
    diff = difflib.unified_diff(
        previous.splitlines(),
        written_content.splitlines(),
        fromfile=f"a/{args['path']}",
        tofile=f"b/{args['path']}",
        lineterm="",
        n=1,
    )
    sha256 = hashlib.sha256(written_content.encode()).hexdigest()
    receipt = {
        "status": f"Wrote {len(written_content.encode())} bytes to {args['path']}",
        "path": args["path"],
        "bytes": len(written_content.encode()),
        "sha256": sha256,
        "host_formatting_changed_content": written_content != content,
        "message": (
            "The snapshot below is the exact persisted file after host formatting."
            if len(written_content) <= MAX_INLINE_WRITE_RECEIPT_CHARS
            else "The exact persisted snapshot is recorded internally; use read_file for source text."
        ),
    }
    if len(written_content) <= MAX_INLINE_WRITE_RECEIPT_CHARS:
        receipt["persisted_content"] = written_content
    return json.dumps(receipt, ensure_ascii=False), {
        "file": args["path"],
        "diff": "\n".join(list(diff)[:5]),
        "file_content": written_content,
        "sha256": sha256,
        "changed": previous != written_content,
    }
