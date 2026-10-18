"""Compile and exercise generated applications, not just template text snapshots."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.coder import agent, starters, tooling
from app.agent.coder.handoff import openapi_contract
from app.agent.coder.scaffolding.composition import build, FEATURES
from app.agent.coder.scaffolding import composition, crud
from app.agent.coder.scaffolding.contracts import Contribution, Feature
from app.agent.coder.scaffolding.spec import AppSpec
from app.agent.coder.working_state import RunState
from app.services import skills

SPEC = {
    "name": "Inventory & contacts",
    "backend": "fastapi",
    "frontend": "vite",
    "entities": [
        {
            "name": "products",
            "label": "Products",
            "fields": [
                {"name": "title", "type": "string"},
                {"name": "quantity", "type": "integer", "default": 0},
                {"name": "price", "type": "number", "default": 0.0},
                {"name": "available", "type": "boolean", "default": False},
                {"name": "notes", "type": "string", "required": False},
            ],
        },
        {
            "name": "contacts",
            "fields": [
                {"name": "name", "type": "string", "label": "Nom / اسم"},
                {"name": "age", "type": "integer", "required": False},
            ],
        },
    ],
}


def test_new_crud_skill_installs_without_replacing_existing_customized_skill(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    existing = tmp_path / "skills/full-stack-app-builder"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("User-customized disabled skill")
    skills.install_builtin_skills()
    assert (existing / "SKILL.md").read_text() == "User-customized disabled skill"
    instructions = skills.load_for_agent("crud-app-builder", "coder")
    reference = skills.load_for_agent("crud-app-builder", "coder", "references/crud.md")
    assert "scaffold_crud_app" in instructions
    assert "products" in reference


def write_project(root, spec=None, plan=None):
    spec = AppSpec.model_validate(spec or SPEC)
    base = {}
    if spec.backend != "none":
        base.update(starters.read_assets(starters.BACKENDS[spec.backend]["assets"]))
    if spec.frontend != "none":
        base.update(starters.frontend_assets(spec.frontend))
    for path, content in base.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    plan = plan or build(spec)
    for path, content in plan.files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return plan


@pytest.mark.parametrize(
    "backend,frontend",
    [
        ("fastapi", "vite"),
        ("fastapi", "react-vite"),
        ("fastapi", "none"),
        ("none", "vite"),
        ("none", "react-vite"),
    ],
)
def test_plans_are_deterministic_versioned_and_have_only_requested_roles(
    backend, frontend
):
    spec = AppSpec.model_validate({**SPEC, "backend": backend, "frontend": frontend})
    first = build(spec)
    assert first == build(spec)
    data = json.loads(first.files[".realopen-app.json"])
    assert data["features"] == {"crud": 1}
    assert data["spec"]["entities"][0]["fields"][3]["default"] is False
    assert any(p.startswith("backend/") for p in first.files) == (backend != "none")
    assert any(p.startswith("frontend/") for p in first.files) == (frontend != "none")
    if frontend != "none":
        assert (
            '"local": true' in first.files["frontend/src/generated.ts"]
            if backend == "none"
            else '"local": false' in first.files["frontend/src/generated.ts"]
        )
    for path, content in first.files.items():
        if path.endswith(".py"):
            ast.parse(content, path)


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "users;DROP TABLE users",
        "class",
        "id",
        "model_fields",
        "sqlite_sequence",
        "Foo",
    ],
)
def test_invalid_schema_identifiers_are_rejected(name):
    with pytest.raises(ValidationError):
        AppSpec.model_validate(
            {**SPEC, "entities": [{"name": name, "fields": [{"name": "title"}]}]}
        )
    with pytest.raises(ValidationError):
        AppSpec.model_validate(
            {**SPEC, "entities": [{"name": "items", "fields": [{"name": name}]}]}
        )


@pytest.mark.parametrize(
    "field",
    [
        {"name": "amount", "type": "integer", "default": False},
        {"name": "amount", "type": "number", "default": float("nan")},
        {"name": "title", "default": "  "},
        {"name": "title", "default": "x" * 1001},
        {"name": "title", "unexpected": "value"},
    ],
)
def test_invalid_defaults_and_unknown_properties_fail_before_generation(field):
    with pytest.raises(ValidationError):
        AppSpec.model_validate(
            {**SPEC, "entities": [{"name": "items", "fields": [field]}]}
        )


def test_duplicate_entities_fields_and_empty_stacks_rejected():
    for override in (
        {"backend": "none", "frontend": "none"},
        {"entities": [SPEC["entities"][0], SPEC["entities"][0]]},
        {
            "entities": [
                {"name": "items", "fields": [{"name": "title"}, {"name": "title"}]}
            ]
        },
    ):
        with pytest.raises(ValidationError):
            AppSpec.model_validate({**SPEC, **override})


def test_user_strings_cannot_be_interpreted_as_template_markers_or_source():
    value = "ENTITY_NAME ENTITY_FIELDS # PATCH_FIELDS ' \\"  # literal source text
    spec = AppSpec.model_validate(
        {
            **SPEC,
            "name": "APP_TITLE # FEATURE_IMPORTS",
            "entities": [
                {"name": "records", "fields": [{"name": "title", "default": value}]}
            ],
        }
    )
    plan = build(spec)
    source = plan.files["backend/resources/records.py"]
    ast.parse(source)
    assert repr(value) in source
    assert json.loads(plan.files[".realopen-app.json"])["spec"]["name"] == spec.name


def test_feature_dependencies_compose_without_combination_templates():
    def auth(spec):
        return Contribution(
            files={"backend/auth_feature.py": "# auth"},
            backend_dependencies=("auth-example",),
            backend_installers=("backend.auth_feature.install",),
            frontend_plugins=("./auth",),
        )

    def websocket(spec):
        return Contribution(
            files={"backend/events_feature.py": "# events"},
            backend_dependencies=("auth-example",),
            backend_installers=("backend.events_feature.install",),
            frontend_plugins=("./events",),
        )

    registry = {
        **FEATURES,
        "auth": Feature("auth", auth),
        "ws": Feature("ws", websocket),
    }
    for features in (
        ("crud",),
        ("crud", "auth"),
        ("crud", "ws"),
        ("crud", "auth", "ws"),
    ):
        plan = build(AppSpec.model_validate(SPEC), features=features, registry=registry)
        assert "backend/resources/products.py" in plan.files
        if "ws" in features:
            assert list(plan.features) == list(features)
            assert plan.backend["dependencies"] == ["auth-example"]
            source = plan.files["backend/app.py"]
            assert source.index("install_0(app, services)") < source.index(
                "router_0(services)"
            )
            if "auth" in features:
                assert "plugin0, plugin1" in plan.files["frontend/src/main.ts"]
    registry["secure_events"] = Feature(
        "secure_events", lambda spec: Contribution(), requires=("auth", "ws")
    )
    plan = build(
        AppSpec.model_validate(SPEC),
        features=("crud", "secure_events"),
        registry=registry,
    )
    assert list(plan.features) == ["crud", "auth", "ws", "secure_events"]


def test_a_new_stack_is_an_adapter_not_a_rewrite_of_feature_combinations(monkeypatch):
    monkeypatch.setitem(
        starters.BACKENDS, "node", {"assets": "node", "install": "bun install"}
    )
    spec = AppSpec.model_validate({**SPEC, "backend": "node", "frontend": "none"})
    with pytest.raises(ValueError, match="adapter"):
        build(spec)

    def render(spec, result):
        result.files["backend/resource.ts"] = "// stack-specific CRUD adapter"

    def shell(spec, plan):
        plan.files["backend/main.ts"] = "// node composition shell"

    monkeypatch.setitem(crud.BACKEND_ADAPTERS, "node", render)
    monkeypatch.setitem(composition.BACKEND_SHELLS, "node", shell)
    plan = build(spec)
    assert "backend/main.ts" in plan.files
    assert "backend/app.py" not in plan.files


@pytest.mark.parametrize(
    "bad",
    [
        Contribution(files={"../escape": "bad"}),
        Contribution(files={"backend/app.py": "overwrite"}),
        Contribution(files={"backend/resources/products.py": "collision"}),
    ],
)
def test_feature_path_conflicts_are_rejected(bad):
    with pytest.raises(ValueError):
        build(
            AppSpec.model_validate(SPEC),
            features=("crud", "bad"),
            registry={**FEATURES, "bad": Feature("bad", lambda spec: bad)},
        )


def test_feature_cycles_unknown_features_and_dependency_conflicts_rejected():
    spec = AppSpec.model_validate(SPEC)
    with pytest.raises(ValueError, match="cycle"):
        build(
            spec,
            features=("x",),
            registry={"x": Feature("x", lambda s: Contribution(), requires=("x",))},
        )
    with pytest.raises(ValueError, match="Unsupported"):
        build(spec, features=("missing",))
    with pytest.raises(ValueError, match="conflicting"):
        build(
            spec,
            features=("x", "y"),
            registry={
                "x": Feature(
                    "x", lambda s: Contribution(frontend_dependencies={"thing": "1"})
                ),
                "y": Feature(
                    "y", lambda s: Contribution(frontend_dependencies={"thing": "2"})
                ),
            },
        )


@pytest.fixture
def workspace(monkeypatch):
    files, calls = {}, []

    async def call(action, sandbox, payload=None, **kwargs):
        payload = payload or {}
        calls.append((action, payload))
        path = payload.get("path")
        if action == "files/stat":
            return {"exists": path in files}
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/write-checked":
            assert payload["expected_sha256"] == "missing" and path not in files
            files[path] = payload["content"]
            return {"written": len(files[path])}
        if action == "exec":
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}
        raise AssertionError(action)

    monkeypatch.setattr(starters.sandbox_host, "call", call)
    return SimpleNamespace(workspace_quota_bytes=10_000_000), files, calls


@pytest.mark.asyncio
async def test_coder_tool_publishes_with_live_progress_and_preserves_user_edits(
    workspace,
):
    sandbox, files, calls = workspace
    progress = []
    output, event = await tooling.dispatch_tool(
        sandbox, "scaffold_crud_app", SPEC, progress.append
    )
    assert event["exit_code"] == 0
    assert progress[0]["stage"] == "files_written"
    manifest = json.loads(files["/workspace/.realopen-project.json"])
    assert manifest["scaffold"]["features"] == {"crud": 1}
    assert manifest["starter"]["application_generated"] is True
    state = RunState()
    state.record("scaffold_crud_app", SPEC, output, event)
    assert state.application_test_roles_changed == {"backend", "frontend"}
    restored = RunState(project=manifest)
    restored.restore_scaffold_metadata(
        manifest["starter"],
        manifest["starter"]["source_hashes"] | manifest["starter"]["test_hashes"],
    )
    assert restored.application_test_roles_changed == {"backend", "frontend"}
    files["/workspace/backend/resources/products.py"] = "USER EDIT"
    before = dict(files)
    with pytest.raises(ValueError, match="overwrite"):
        await tooling.dispatch_tool(sandbox, "scaffold_crud_app", SPEC)
    assert files == before
    event = agent._event_for_step("scaffold_crud_app", SPEC, "step", None, "sandbox")
    assert (
        event["title"] == "Creating CRUD application"
        and event["toolName"] == "scaffold_crud_app"
    )


@pytest.mark.asyncio
async def test_invalid_or_oversize_scaffolds_never_write(workspace):
    sandbox, files, calls = workspace
    with pytest.raises(ValidationError):
        await tooling.dispatch_tool(
            sandbox, "scaffold_crud_app", {**SPEC, "entities": []}
        )
    assert not calls
    sandbox.workspace_quota_bytes = 1
    with pytest.raises(ValueError, match="quota"):
        await tooling.dispatch_tool(sandbox, "scaffold_crud_app", SPEC)
    assert not files


def run_generated_tests(root):
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "DATABASE_PATH": str(root / "live.sqlite3"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "backend/tests",
            "-q",
            "--confcutdir=backend/tests",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_generated_backend_behavior_and_database_isolation(tmp_path):
    write_project(tmp_path, {**SPEC, "frontend": "none"})
    live = tmp_path / "live.sqlite3"
    live.write_bytes(b"untouched live database sentinel")
    result = run_generated_tests(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert live.read_bytes() == b"untouched live database sentinel"


def test_generated_partial_updates_search_pagination_and_validation(tmp_path):
    write_project(tmp_path, {**SPEC, "frontend": "none"})
    (tmp_path / "backend/tests/test_edge_cases.py").write_text(
        """
from backend.app import create_app
from fastapi.testclient import TestClient

def test_defaults_and_partial_values(client):
    row = client.post("/api/products", json={"title": "  Original  "}).json()
    assert row == {"id": 1, "title": "Original", "quantity": 0, "price": 0.0, "available": False, "notes": None}
    url = "/api/products/1"
    assert client.patch(url, json={"available": True, "quantity": 10}).status_code == 200
    updated = client.patch(url, json={"available": False, "quantity": 0}).json()
    assert updated["available"] is False and updated["quantity"] == 0
    assert updated["title"] == "Original"
    assert client.patch(url, json={}).json() == updated
    assert client.patch(url, json={"notes": ""}).json()["notes"] == ""
    assert client.patch(url, json={"notes": None}).json()["notes"] is None
    for invalid in ({"title": None}, {"title": "  "}, {"quantity": False}, {"quantity": "1"}, {"price": True}, {"available": 1}, {"invented": "x"}):
        assert client.patch(url, json=invalid).status_code == 422
    with TestClient(create_app()) as reopened:
        assert reopened.get(url).json()["title"] == "Original"

def test_literal_search_bound_values_and_sort(client):
    for title, quantity in [("O'Brien %_", 20), ("B", 0), ("A", 10)]:
        assert client.post("/api/products", json={"title": title, "quantity": quantity}).status_code == 201
    assert client.get("/api/products", params={"search": "%_"}).json()["total"] == 1
    assert client.get("/api/products", params={"search": "' OR 1=1 --"}).json()["total"] == 0
    assert [row["quantity"] for row in client.get("/api/products", params={"sort": "quantity", "descending": True}).json()["items"]] == [20, 10, 0]
    assert client.get("/api/products", params={"sort": "id; DROP TABLE products"}).status_code == 422
    page = client.get("/api/products", params={"offset": 1, "limit": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["items"][0]["id"] == 2
    assert client.get("/api/contacts").json()["total"] == 0
"""
    )
    result = run_generated_tests(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_combined_access_rules_and_mutation_hooks_work_in_generated_app(tmp_path):
    # Test-only feature modules exercise composition, not production Auth/WS.
    access = """from fastapi import Depends, Header, HTTPException
def require_access(authorization: str | None = Header(None)):
    if authorization != "test-access":
        raise HTTPException(401, "Access required")
def install(app, services):
    services.route_dependencies.append(Depends(require_access))
"""
    observer = """def install(app, services):
    app.state.observed = []
    async def changed(event):
        app.state.observed.append(event)
    async def broken(event):
        raise RuntimeError("notification unavailable")
    services.mutation_listeners.extend([broken, changed])
"""
    registry = {
        **FEATURES,
        "access": Feature(
            "access",
            lambda spec: Contribution(
                files={"backend/access_feature.py": access},
                backend_installers=("backend.access_feature.install",),
            ),
        ),
        "events": Feature(
            "events",
            lambda spec: Contribution(
                files={"backend/events_feature.py": observer},
                backend_installers=("backend.events_feature.install",),
            ),
        ),
    }
    spec = AppSpec.model_validate({**SPEC, "frontend": "none"})
    plan = build(spec, features=("crud", "access", "events"), registry=registry)
    write_project(tmp_path, spec.model_dump(), plan)
    # Replace generated contract suite, which intentionally assumes anonymous access.
    (tmp_path / "backend/tests/test_crud.py").write_text(
        """
def test_access_and_events(client):
    from backend.app import app
    assert client.post("/api/products", json={"title": "Denied"}).status_code == 401
    assert app.state.observed == []
    headers = {"Authorization": "test-access"}
    assert client.post("/api/products", json={"title": "Allowed"}, headers=headers).status_code == 201
    assert client.patch("/api/products/1", json={"available": False}, headers=headers).status_code == 200
    assert client.delete("/api/products/1", headers=headers).status_code == 204
    assert [event["action"] for event in app.state.observed] == ["created", "updated", "deleted"]
    assert client.get("/api/products", headers=headers).json()["total"] == 0
"""
    )
    result = run_generated_tests(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("styling", ["tailwind"])
@pytest.mark.parametrize("backend", ["fastapi", "none"])
@pytest.mark.parametrize("frontend_stack", ["react-vite", "vite"])
def test_generated_frontend_dom_tests_typecheck_and_build(
    tmp_path, styling, backend, frontend_stack
):
    modules = Path(
        os.environ.get(
            "SCAFFOLD_TEST_NODE_MODULES",
            str(Path(__file__).resolve().parents[2] / "frontend/node_modules"),
        )
    )
    if not (modules / ".bin/vitest").exists():
        pytest.skip(
            "Frontend dependencies must be installed to exercise generated TypeScript"
        )
    if styling == "tailwind" and not (modules / "@tailwindcss/vite").exists():
        pytest.skip("Tailwind test dependencies must be installed")
    write_project(
        tmp_path,
        {**SPEC, "styling": styling, "backend": backend, "frontend": frontend_stack},
    )
    frontend = tmp_path / "frontend"
    if styling == "tailwind":
        (frontend / "src/styling_probe.ts").write_text(
            'export const classes = "text-fuchsia-700 md:grid-cols-3";\n'
        )
    (frontend / "node_modules").symlink_to(modules, target_is_directory=True)
    for command in (
        [str(modules / ".bin/tsc"), "--noEmit"],
        [str(modules / ".bin/vitest"), "run"],
        [str(modules / ".bin/vite"), "build"],
    ):
        result = subprocess.run(
            command, cwd=frontend, capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr

    css = "\n".join(
        path.read_text() for path in (frontend / "dist/assets").glob("*.css")
    )
    if styling == "tailwind":
        assert "tailwindcss" in css and "@apply" not in css
        assert "--app-primary" in css
        assert ".text-fuchsia-700" in css


@pytest.mark.skipif(
    os.environ.get("SCAFFOLD_INTEGRATION_TESTS") != "1",
    reason="Opt-in: bind local backend/frontend ports for real HTTP integration",
)
@pytest.mark.parametrize("styling", ["tailwind"])
@pytest.mark.parametrize("frontend_stack", ["react-vite", "vite"])
def test_generated_full_stack_through_real_vite_proxy(
    tmp_path, styling, frontend_stack
):
    modules = Path(os.environ["SCAFFOLD_TEST_NODE_MODULES"])
    write_project(tmp_path, {**SPEC, "styling": styling, "frontend": frontend_stack})
    (tmp_path / "frontend/node_modules").symlink_to(modules, target_is_directory=True)

    def free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    backend_port, frontend_port = free_port(), free_port()
    # Keep the generated proxy contract; select free local ports only for the test.
    vite_config = tmp_path / "frontend/vite.config.ts"
    vite_config.write_text(
        vite_config.read_text().replace("127.0.0.1:6969", f"127.0.0.1:{backend_port}")
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "DATABASE_PATH": str(tmp_path / "isolated.sqlite3"),
    }
    backend = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    frontend = subprocess.Popen(
        [
            str(modules / ".bin/vite"),
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
        ],
        cwd=tmp_path / "frontend",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(path, data=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        with urlopen(
            Request(
                f"http://127.0.0.1:{frontend_port}" + path,
                body,
                {"Content-Type": "application/json"},
                method=method,
            ),
            timeout=5,
        ) as response:
            return response.status, response.read()

    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                assert request("/api/health")[0] == 200
                break
            except (URLError, TimeoutError):
                if time.monotonic() > deadline:
                    raise AssertionError("Generated app/proxy did not become healthy")
                time.sleep(0.1)
        assert request("/")[0] == 200
        # The running backend supplies the frontend handoff, not invented docs.
        with urlopen(
            f"http://127.0.0.1:{backend_port}/openapi.json", timeout=5
        ) as response:
            contract = openapi_contract(json.load(response), "/api/contacts")
        assert len(contract["operations"]) == 5
        assert {operation["method"] for operation in contract["operations"]} == {
            "GET",
            "POST",
            "PATCH",
            "DELETE",
        }
        if frontend_stack == "react-vite":
            assert request("/src/main.tsx")[0] == 200
            assert request("/src/App.tsx")[0] == 200
        css_status, css = request("/src/theme.css")
        assert css_status == 200 and b"@apply" not in css
        status, body = request(
            "/api/products", {"title": "Through proxy", "available": True}, "POST"
        )
        assert status == 201
        record_id = json.loads(body)["id"]
        status, body = request(
            f"/api/products/{record_id}", {"available": False, "quantity": 0}, "PATCH"
        )
        assert status == 200 and json.loads(body)["available"] is False
        assert json.loads(request("/api/products")[1])["total"] == 1
        assert request(f"/api/products/{record_id}", method="DELETE")[0] == 204
        assert json.loads(request("/api/products")[1])["total"] == 0
    finally:
        for process in (frontend, backend):
            process.terminate()
        for process in (frontend, backend):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
