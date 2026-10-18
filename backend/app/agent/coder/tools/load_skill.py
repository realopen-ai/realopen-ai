"""load skill tool: schema and execution."""

from app.services import skills as skill_store
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "load_skill",
    "Load the full instructions for one relevant skill from the catalog.",
    {"name": {"type": "string"}, "resource": {"type": "string"}},
    ("name",),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    return (
        skill_store.load_for_agent(
            str(args["name"]),
            "coder",
            str(args["resource"]) if args.get("resource") else None,
        ),
        None,
    )
