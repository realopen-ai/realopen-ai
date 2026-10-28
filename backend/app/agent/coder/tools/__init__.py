"""Explicit coder registry with stable schema ordering."""

from collections.abc import Callable

from app.agent.coder.tools import (
    scaffold_crud_app,
    scaffold_project,
    check_preview,
    load_skill,
    setup_python_project,
    list_files,
    read_file,
    edit_file,
    write_file,
    search_files,
    run_command,
    run_tests,
    install_dependencies,
    run_checks,
    run_browser_tests,
    start_preview,
)

TOOL_MODULES = (
    scaffold_crud_app,
    scaffold_project,
    check_preview,
    load_skill,
    setup_python_project,
    list_files,
    read_file,
    edit_file,
    write_file,
    search_files,
    run_command,
    run_tests,
    install_dependencies,
    run_checks,
    run_browser_tests,
    start_preview,
)
CODER_TOOLS = [module.SCHEMA for module in TOOL_MODULES]
TOOL_REGISTRY = {module.SCHEMA["function"]["name"]: module for module in TOOL_MODULES}


async def dispatch_tool(
    sandbox, name: str, args: dict, progress: Callable[[dict], None] | None = None
) -> tuple[str, dict | None]:
    module = TOOL_REGISTRY.get(name)
    if module is None:
        return f"Unknown coder tool: {name}", None
    return await module.execute(sandbox, args, progress)
