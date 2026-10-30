"""Small scaffold maps and observed API contracts, not copies of source files."""

import copy
import json


def project_handoff(project, metadata=None):
    """Describe only registered scaffold capabilities; never claim checks passed."""
    metadata = metadata or {}
    backend, frontend = project.get("backend"), project.get("frontend")
    generated = metadata.get("generator") == "crud"
    features = metadata.get("features", {})
    if not generated and any(
        (part or {}).get("scaffold") == "minimal" for part in project.values()
    ):
        result = {
            "schema_version": 1,
            "application_generated": False,
            "scope": "Minimal runnable starter only; no requested application behavior has been implemented.",
            "implemented": [
                (
                    "Packaging, local styling and API proxy"
                    if frontend
                    else "Packaging and health endpoint"
                )
            ],
            "persistence": "Not implemented; add it only when required by the user.",
            "checks": {
                role: {
                    key: config[key]
                    for key in ("test", "typecheck", "build")
                    if key in config
                }
                for role, config in project.items()
                if config
            },
            "edit_targets": {
                **({"api": ["backend/app.py"]} if backend else {}),
                **(
                    {
                        "ui": [
                            "frontend/"
                            + frontend["entrypoint"].replace("main.tsx", "App.tsx")
                        ],
                        "styling": ["frontend/src/style.css"],
                        "connection": ["frontend/vite.config.ts"],
                    }
                    if frontend
                    else {}
                ),
            },
            "next_step": "Implement the requested application and its isolated behavior tests. No tests are generated; health/build checks only verify starter setup.",
        }
        if frontend:
            result["frontend_entrypoint"] = "frontend/" + frontend["entrypoint"]
        probe = (backend or frontend or {}).get("readiness_path")
        if probe:
            result["integration_probe"] = {
                "port": 6767 if frontend else 6969,
                "path": probe,
                "scope": "Readiness/transport only, not application behavior.",
            }
        return result
    result = {
        "schema_version": 1,
        "scope": "Initial generated architecture, not proof of current source or correctness.",
        "application_generated": generated,
        "features": features,
        "implemented": (
            [
                (
                    "CRUD forms, routes and behavior tests"
                    if backend and frontend
                    else "CRUD behavior and tests"
                )
            ]
            if generated
            else [
                "Packaging and setup smoke tests; requested application still needs implementation"
            ]
        ),
        "resources": metadata.get("resources", []),
        "checks": {
            role: {
                key: config[key]
                for key in ("test", "typecheck", "build")
                if key in config
            }
            for role, config in project.items()
            if config
        },
        "edit_targets": {},
        "next_step": (
            "Run generated checks first; inspect only a failure or a remaining explicit requirement."
            if generated
            else "Define the API contract, implement and test backend behavior, then frontend and integration; establish an early vertical slice for uncertain interfaces."
        ),
    }
    targets = result["edit_targets"]
    probe = (backend or frontend or {}).get("readiness_path")
    if probe:
        result["integration_probe"] = {
            "port": 6767 if frontend else 6969,
            "path": probe,
            "scope": "Readiness and transport only, not authorization or UI behavior coverage. For full-stack apps this requests the backend through the frontend proxy.",
        }
    if backend:
        targets["api"] = [
            "backend/resources/<resource>.py" if generated else "backend/app.py"
        ]
        targets["persistence"] = (
            ["backend/repository.py", "backend/db.py"]
            if generated
            else ["backend/db.py"]
        )
        result["persistence"] = (
            "SQLite; backend/db.py supplies DATABASE_PATH override and transactions; tests use isolated databases."
        )
        result["api_contract"] = {
            "source": "check_preview(port=6969, path='/openapi.json') after starting backend; api_path optionally filters endpoints",
            "resources": [
                {"path": "/api/" + name, "source": f"backend/resources/{name}.py"}
                for name in result["resources"]
            ],
            "status": "Not inspected at runtime",
        }
    elif frontend:
        result["persistence"] = (
            "Browser localStorage" if generated else "No persistence implemented"
        )
    if frontend:
        react = frontend.get("framework") == "react"
        targets["ui"] = (
            [
                "frontend/src/App.tsx",
                "frontend/src/components/",
                "frontend/src/hooks/useCrud.ts",
            ]
            if generated and react
            else [
                (
                    "frontend/src/App.tsx"
                    if react
                    else "frontend/src/crud.ts" if generated else "frontend/src/main.ts"
                )
            ]
        )
        targets["styling"] = ["frontend/src/style.css", "frontend/src/theme.css"]
        targets["connection"] = [
            "frontend/src/data.ts" if generated else "frontend/src/api.ts",
            "frontend/vite.config.ts",
        ]
        result["frontend_entrypoint"] = "frontend/" + frontend["entrypoint"]
    if generated:
        result["extension_points"] = {
            **(
                {
                    "backend": "backend/services.py: route dependencies and post-commit mutation listeners"
                }
                if backend
                else {}
            ),
            **(
                {
                    "frontend": "frontend/src/extensions.ts: shared transport, invalidation and cleanup"
                }
                if frontend
                else {}
            ),
        }
        if "auth" in features:
            result["implemented"].append(
                "Local registration/login/logout, revocable cookie sessions and CSRF protection"
            )
            targets["authentication"] = ["backend/auth.py"]
            if frontend:
                targets["authentication"].append("frontend/src/auth/")
            result["session_configuration"] = (
                "AUTH_COOKIE_SECURE=1 for HTTPS deployment; preview HTTP is supported. Accounts are local; there are no seeded credentials."
            )
            result["data_access"] = (
                "private per-user records"
                if "ownership" in features
                else "records shared by signed-in accounts"
            )
        if "ownership" in features:
            result["implemented"].append(
                "Server-assigned row ownership enforced on every CRUD operation"
            )
            targets["ownership"] = ["backend/ownership.py", "backend/repository.py"]
        result["not_generated"] = [
            "Relationships",
            *([] if "auth" in features else ["Auth"]),
            *(
                ["Password reset, email verification, SSO and role-based permissions"]
                if "auth" in features
                else []
            ),
            "WebSockets",
            "Additional business rules",
        ]
    return result


def openapi_contract(document, path_prefix=None):
    """Keep wire schemas and references verbatim, omitting prose/examples, not types.

    Select referenced schemas transitively. A self-reference is retained as a
    reference rather than expanded. Supports OpenAPI 3.x JSON schemas.
    """
    if (
        not isinstance(document, dict)
        or not str(document.get("openapi", "")).startswith("3.")
        or not isinstance(document.get("paths"), dict)
    ):
        raise ValueError("Response is not an OpenAPI 3.x document")

    refs = set()

    def wire(value):
        if isinstance(value, list):
            return [wire(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str):
            refs.add(reference)
        # Property/status/media names and literal defaults are data, not prose.
        return {
            key: (
                {name: wire(schema) for name, schema in item.items()}
                if key
                in {
                    "properties",
                    "patternProperties",
                    "content",
                    "responses",
                    "headers",
                    "$defs",
                }
                and isinstance(item, dict)
                else (
                    copy.deepcopy(item)
                    if key in {"default", "const", "enum"}
                    else wire(item)
                )
            )
            for key, item in value.items()
            if key
            not in {"description", "summary", "example", "examples", "externalDocs"}
        }

    operations = []
    for path, path_item in document["paths"].items():
        if path_prefix and not (
            path == path_prefix.rstrip("/")
            or path.startswith(path_prefix.rstrip("/") + "/")
        ):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "trace",
            } or not isinstance(operation, dict):
                continue
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    **wire(
                        {
                            key: operation[key]
                            for key in ("requestBody", "responses", "security")
                            if key in operation
                        }
                    ),
                    "parameters": wire(
                        [
                            *path_item.get("parameters", []),
                            *operation.get("parameters", []),
                        ]
                    ),
                }
            )
    # Resolve internal references, including parameters, responses and requestBodies.
    definitions = {}
    while refs - definitions.keys():
        reference = sorted(refs - definitions.keys())[0]
        value = document
        if not reference.startswith("#/"):
            definitions[reference] = {"unresolved": True}
            continue
        try:
            for part in reference[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            value = {"unresolved": True}
        definitions[reference] = wire(value)
    return {
        "source": "Runtime OpenAPI",
        "openapi": document["openapi"],
        "path_prefix": path_prefix,
        "operations": operations,
        "definitions": definitions,
        "security": document.get("security", []),
        "security_schemes": document.get("components", {}).get("securitySchemes", {}),
    }


def bounded_contract(contract, token_limit, estimate):
    """Keep valid JSON and explicitly report omissions under the existing budget."""
    result = copy.deepcopy(contract)
    while estimate(result) > token_limit and result.get("definitions"):
        result["definitions"].pop(next(reversed(result["definitions"])))
        result["omitted_definitions"] = result.get("omitted_definitions", 0) + 1
    while estimate(result) > token_limit and result.get("operations"):
        result["operations"].pop()
        result["omitted_operations"] = result.get("omitted_operations", 0) + 1
    if result.get("omitted_definitions") or result.get("omitted_operations"):
        result["notice"] = (
            "Contract abbreviated; use check_preview /openapi.json with api_path for the relevant resource. Never infer omitted schemas."
        )
    return (
        result
        if estimate(result) <= token_limit
        else {
            "notice": "API contract available; request the relevant api_path to inspect it within budget."
        }
    )


def agent_scaffold_result(output, event):
    """UI/history/traces retain full output; inference gets the actionable handoff."""
    if not event or not event.get("project_handoff"):
        return output
    data = json.loads(output)
    return json.dumps(
        {
            "handoff": event["project_handoff"],
            "created_file_count": len(data.get("created_files", [])),
            "setup": [
                {
                    key: item[key]
                    for key in ("action", "exit_code", "error")
                    if key in item
                }
                | ({"failure_details": item} if item.get("exit_code") != 0 else {})
                for item in data.get("setup", [])
            ],
            "note": "Dependency setup is not application verification. Full file list/output remains in tool history.",
        },
        ensure_ascii=False,
    )
