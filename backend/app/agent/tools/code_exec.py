"""
Code execution tool — runs Python in a sandboxed subprocess.
"""

import logging
import time
import subprocess
import tempfile
from pathlib import Path
from typing import List

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry

logger = logging.getLogger(__name__)


class CodeExecTool(BaseTool):
    name = "use_code_exec"
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

        # Basic safety: block dangerous imports
        blocked = {"os.system", "subprocess", "shutil.rmtree", "__import__('os')"}
        code_lower = code.lower()
        for b in blocked:
            if b in code_lower:
                return f"Blocked dangerous operation: {b}", 1

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
                cwd="/tmp",
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]:\n{result.stderr}"
            return output.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "Error: Code execution timed out (30s)", 1
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# Register the tool
tool_registry.register(CodeExecTool())
