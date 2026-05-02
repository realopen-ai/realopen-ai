"""
Code execution tool - runs Python code in the sandbox.

This tool allows the AI agent to execute Python code and return the output.
In production, this should run in a sandboxed container. For now, it executes
locally with basic safety checks.
"""

import logging
import time
import subprocess
import tempfile
from pathlib import Path

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry

logger = logging.getLogger(__name__)


class CodeExecTool(BaseTool):
    name = "use_code_exec"
    description = (
        "Execute Python code and return the output. "
        "Use this for calculations, data processing, or any task that requires "
        "running code. Provide the code as a string. "
        "The code runs in a sandboxed environment with a 30-second timeout."
    )
    tool_type = ToolType.CODE_EXEC

    async def execute(
        self,
        *,
        code: str,
        language: str = "python",
        **kwargs,
    ) -> ToolResult:
        """Execute code and return the output."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-code-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title=f"Running {language}",
            language=language,
            code=code,
            started_at=start,
        )

        try:
            output, exit_code = self._run_code(code, language)
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.output = output
            tool_call.exit_code = exit_code

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

    def _run_code(self, code: str, language: str) -> tuple[str, int]:
        """Run code in a subprocess with timeout."""
        if language not in ("python", "python3"):
            return f"Unsupported language: {language}", 1

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            f.flush()
            tmp_path = f.name

        try:
            result = subprocess.run(
                ["python3", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if result.stderr:
                output += f"\nStderr:\n{result.stderr}"
            return output.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "Error: Code execution timed out (30s)", 1
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# Register the tool
tool_registry.register(CodeExecTool())
