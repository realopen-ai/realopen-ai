"""Render editable application source, not an LLM-generated packaging recipe."""

import json
import re
from pathlib import Path

from app.agent.coder.scaffolding.contracts import Contribution
from app.agent.coder.starters import frontend_assets

TEMPLATES = Path(__file__).with_name("templates")


def asset(path):
    return (TEMPLATES / path).read_text()


def render_asset(path, replacements):
    # One pass over template text: inserted user strings are never interpreted
    # as another placeholder (or as Python/TypeScript template expressions).
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match[0]], asset(path))


def render_crud(spec):
    result = Contribution()
    for selected, adapters in (
        (spec.backend, BACKEND_ADAPTERS),
        (spec.frontend, FRONTEND_ADAPTERS),
    ):
        if selected == "none":
            continue
        if selected not in adapters:
            raise ValueError(f"CRUD needs an adapter for stack {selected}")
        adapters[selected](spec, result)
    return result


def fastapi(spec, result):
    result.files["backend/repository.py"] = asset("backend/repository.py.tpl")
    result.files["backend/tests/test_crud.py"] = asset(
        "backend/test_crud.py.tpl"
    ).replace("ENTITY_CASES", repr([entity_case(e) for e in spec.entities]))
    routers = []
    for entity in spec.entities:
        path = f"backend/resources/{entity.name}.py"
        result.files[path] = render_entity(entity)
        routers.append(f"backend.resources.{entity.name}.build_router")
    result.files["backend/resources/__init__.py"] = ""
    result.backend_routers = tuple(routers)


def vite(spec, result):
    for name in ("crud.ts", "crud.test.ts", "style.css"):
        result.files[f"frontend/src/{name}"] = asset(f"frontend/{name}.tpl")
    shared_frontend(spec, result)
    result.files["frontend/index.html"] = asset("frontend/index.html.tpl")


def shared_frontend(spec, result):
    for name in ("data.ts", "types.ts"):
        result.files[f"frontend/src/{name}"] = asset(f"frontend/{name}.tpl")
    result.files["frontend/src/generated.ts"] = (
        'import type { AppConfig } from "./types";\n'
        "export const config: AppConfig = "
        + json.dumps(
            {
                "name": spec.name,
                "local": spec.backend == "none",
                "entities": [e.model_dump() for e in spec.entities],
            },
            ensure_ascii=False,
        )
        + ";\n"
    )


def react_vite(spec, result):
    shared_frontend(spec, result)
    for source, target in {
        "App.tsx": "App.tsx",
        "App.test.tsx": "App.test.tsx",
        "useCrud.ts": "hooks/useCrud.ts",
        "RecordForm.tsx": "components/RecordForm.tsx",
        "RecordList.tsx": "components/RecordList.tsx",
        "Filters.tsx": "components/Filters.tsx",
    }.items():
        result.files[f"frontend/src/{target}"] = asset(f"frontend/react/{source}.tpl")
    result.files["frontend/src/style.css"] = asset("frontend/style.css.tpl")
    result.files["frontend/index.html"] = frontend_assets(spec.frontend)[
        "frontend/index.html"
    ]


def entity_case(entity):
    examples = {"string": "Example value", "integer": 7, "number": 3.5, "boolean": True}
    payload = {f.name: examples[f.type] for f in entity.fields}
    field = entity.fields[0]
    changed = {
        "string": "Updated value",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
    }[field.type]
    return (
        entity.name,
        payload,
        {field.name: changed},
        [f.name for f in entity.fields if f.required and f.default is None],
    )


def render_entity(entity):
    create, patch = [], []
    types = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
    for field in entity.fields:
        annotation = types[field.type]
        nullable = not field.required
        if nullable:
            annotation += " | None"
        rules = "max_length=1000, " if field.type == "string" else ""
        if field.type == "string" and field.required:
            rules += "min_length=1, "
        if field.type == "integer":
            rules += "ge=-9007199254740991, le=9007199254740991, "
        rules += (
            "strict=True"
            if field.type != "number"
            else "strict=True, allow_inf_nan=False"
        )
        default = (
            repr(field.default) if field.default is not None or nullable else "..."
        )
        create.append(
            f"    {field.name}: {annotation} = Field(default={default}, {rules})"
        )
        patch.append(f"    {field.name}: {annotation} = Field(default=None, {rules})")
    return render_asset(
        "backend/entity.py.tpl",
        {
            "    # CREATE_FIELDS": "\n".join(create),
            "    # PATCH_FIELDS": "\n".join(patch),
            "ENTITY_NAME": repr(entity.name),
            "ENTITY_FIELDS": repr([f.model_dump() for f in entity.fields]),
        },
    )


BACKEND_ADAPTERS = {"fastapi": fastapi}
FRONTEND_ADAPTERS = {"vite": vite, "react-vite": react_vite}
