"""
Code execution tool — runs Python in a sandboxed subprocess.

The execution timeout is configurable (Brain ▸ Tools ▸ Code
Execution); the safety blocklist is deliberately NOT configurable.
"""

import logging
import time
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.services.sandbox_commands import persist_general_code_command

logger = logging.getLogger(__name__)

# Fallback execution timeout (seconds) when the persisted configuration
# predates the setting — the actual value lives in the tool config.
DEFAULT_TIMEOUT_S = 30


class CodeExecTool(BaseTool):
    name = "use_code_exec"
    display_name = "Code Execution"
    description = (
        "Execute Python code and return the output. "
        "Use for calculations, data processing, or scripting. "
        "Only printed output is captured — always print results."
    )
    tool_type = ToolType.CODE_EXEC

    param_aliases = {"code": "code", "script": "code", "program": "code"}

    def get_parameters(self) -> dict:
        return {
            "code": {
                "type": "string",
                "description": "Python code to execute. Print final results.",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["code"]

    async def execute(
        self,
        *,
        code: str,
        language: str = "python",
        conversation_id: str | None = None,
        **kwargs,
    ) -> ToolResult:
        """Execute code and return the output."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-code-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Running code",
            language=language,
            code=code,
            started_at=start,
        )

        try:
            stdout, stderr, exit_code = self._run_code(code, language)
            output = stdout
            if stderr:
                output += f"\n[stderr]:\n{stderr}"
            output = output.strip()
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.output = output
            tool_call.exit_code = exit_code

            try:
                await persist_general_code_command(
                    conversation_id,
                    command=code,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    started_at=datetime.fromtimestamp(start),
                    completed_at=datetime.utcnow(),
                )
            except Exception as exc:
                logger.warning("Could not persist code execution history: %s", exc)

            result_text = f"Code execution output (exit code {exit_code}):\n{output}"
            return ToolResult(
                success=exit_code == 0, output=result_text, tool_call=tool_call
            )

        except Exception as e:
            logger.error("Code execution failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Code execution failed: {e}",
                tool_call=tool_call,
            )

    def _run_code(self, code: str, language: str) -> tuple[str, str, int]:
        """Run code in a subprocess with the configured timeout."""
        if language not in ("python", "python3"):
            return "", f"Unsupported language: {language}", 1

        # Basic safety: block dangerous imports
        blocked = {"os.system", "subprocess", "shutil.rmtree", "__import__('os')"}
        code_lower = code.lower()
        for b in blocked:
            if b in code_lower:
                return "", f"Blocked dangerous operation: {b}", 1

        timeout_s = _configured_timeout_s()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["python3", tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd="/tmp",
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", f"Error: Code execution timed out ({timeout_s}s)", 1
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ── Configuration definition (Brain ▸ Tools ▸ Code Execution) ─────


def _configured_timeout_s() -> int:
    """The persisted execution timeout (custom.timeout_s).

    Read at every execution — UI changes apply immediately.
    """
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_code_exec") or {}
    try:
        value = int((cfg.get("custom") or {}).get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(1, min(600, value))


def _validate_code_exec_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "timeout_s" in custom:
        try:
            v = float(custom["timeout_s"])
        except (TypeError, ValueError):
            raise ValueError("'timeout_s' must be a number")
        if v <= 0 or v > 600:
            raise ValueError("'timeout_s' must be between 1 and 600")
        custom["timeout_s"] = int(v)
    return custom


CODE_EXEC_CONFIG = ToolConfigDefinition(
    tool_name="use_code_exec",
    display_name="Code Execution",
    description=(
        "Execute Python code and return the output. "
        "Use for calculations, data processing, or scripting. "
        "Only printed output is captured — always print results."
    ),
    custom_defaults={"timeout_s": DEFAULT_TIMEOUT_S},
    custom_schema=[
        {
            "key": "execution",
            "label": "Execution",
            "fields": [
                ConfigField(
                    "timeout_s",
                    "Timeout (s)",
                    "int",
                    default=DEFAULT_TIMEOUT_S,
                    help="Maximum seconds a script may run before it is killed",
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_code_exec_custom,
)

register_config(CODE_EXEC_CONFIG)


# Register the tool
tool_registry.register(CodeExecTool())
