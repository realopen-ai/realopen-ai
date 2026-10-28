"""Composable, deterministic application scaffolds for the existing coder tools."""

from app.agent.coder.scaffolding.spec import AppSpec
from app.agent.coder.scaffolding.composition import build
from app.agent.coder.starters import scaffold


async def scaffold_crud(sandbox, args, setup_python, progress=None):
    spec = AppSpec.model_validate(args)
    plan = build(spec)
    return await scaffold(
        sandbox,
        spec.model_dump(),
        setup_python,
        progress,
        generated=plan.files,
        composition=plan.manifest(),
    )
