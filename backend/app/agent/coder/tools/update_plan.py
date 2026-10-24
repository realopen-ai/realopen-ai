"""Small, model-authored continuation notes; never verification evidence."""

import json

from app.agent.coder.tools.schema import _function
from app.agent.validation import argument_errors

SCHEMA = _function(
    "update_plan",
    "Save a short continuation plan for this task. Replace it as work advances; it survives compaction. Record the concrete next action and unresolved questions, not source code. Plan claims never count as executed checks or file changes.",
    {
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "minLength": 1, "maxLength": 200},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["task", "status"],
                "additionalProperties": False,
            },
        },
        "next_action": {"type": "string", "minLength": 1, "maxLength": 300},
        "notes": {"type": "string", "maxLength": 600},
    },
    ("steps", "next_action"),
)


async def execute(sandbox, args, progress=None):
    errors = argument_errors(args, SCHEMA["function"]["parameters"])
    if errors:
        raise ValueError("Invalid continuation plan: " + "; ".join(errors))
    # The shared validator checks lengths/types but intentionally supports only
    # a subset of JSON Schema. Never retain undeclared nested payloads here.
    plan = {
        "steps": [
            {key: step[key] for key in ("task", "status")} for step in args["steps"]
        ],
        "next_action": args["next_action"],
        **({"notes": args["notes"]} if isinstance(args.get("notes"), str) else {}),
    }
    return json.dumps({"saved_plan": plan}), {"working_plan": plan}
