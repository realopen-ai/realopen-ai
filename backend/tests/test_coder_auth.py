"""Exercise generated authentication and ownership, not only template strings."""

import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time

import httpx

import pytest

from app.agent.coder.scaffolding.composition import build
from app.agent.coder.scaffolding.spec import AppSpec
from app.agent.coder.scaffolding.contracts import Contribution, Feature
from app.agent.coder.scaffolding.composition import FEATURES
from app.agent.coder.handoff import project_handoff
from app.agent.coder.starters import BACKENDS, FRONTENDS
from app.agent.coder.tooling import CODER_TOOLS
from test_crud_scaffolding import SPEC, run_generated_tests, write_project


def spec(**values):
    return AppSpec.model_validate({**SPEC, "features": ["crud", "ownership"], **values})


@pytest.mark.parametrize("frontend", ["none", "vite", "react-vite"])
def test_ownership_composes_auth_and_records_requested_capabilities(frontend):
    plan = build(spec(frontend=frontend))
    assert list(plan.features) == ["crud", "auth", "ownership"]
    assert plan.installers == ["backend.auth.install", "backend.ownership.install"]
    assert plan.backend["dependencies"] == []  # Standard-library password hashing.
    assert "backend/auth.py" in plan.files and "backend/ownership.py" in plan.files
    assert "FEATURE_" not in plan.files["backend/app.py"]
    if frontend != "none":
        entry = "main.tsx" if frontend == "react-vite" else "main.ts"
        assert "FEATURE_" not in plan.files["frontend/src/" + entry]
        assert "frontend/src/auth/session.ts" in plan.files


@pytest.mark.parametrize("frontend", ["vite", "react-vite"])
def test_browser_only_auth_is_rejected(frontend):
    with pytest.raises(ValueError, match="backend|server"):
        build(spec(backend="none", frontend=frontend))


def test_client_owned_identity_and_reserved_auth_routes_are_rejected():
    with pytest.raises(ValueError, match="application-managed"):
        build(spec(entities=[{"name": "posts", "fields": [{"name": "owner_id"}]}]))
    with pytest.raises(ValueError, match="reserved"):
        build(spec(entities=[{"name": "auth", "fields": [{"name": "title"}]}]))
    plan = build(spec(features=["crud"]))
    assert "backend/auth.py" not in plan.files
    assert "frontend/src/auth/session.ts" not in plan.files


def test_schema_and_durable_map_advertise_only_selected_features():
    schema = next(
        tool["function"]
        for tool in CODER_TOOLS
        if tool["function"]["name"] == "scaffold_crud_app"
    )
    assert schema["parameters"]["properties"]["features"]["items"]["enum"] == [
        "crud",
        "auth",
        "ownership",
    ]
    for features in (
        {"crud": 1},
        {"crud": 1, "auth": 1},
        {"crud": 1, "auth": 1, "ownership": 1},
    ):
        handoff = project_handoff(
            {"backend": BACKENDS["fastapi"], "frontend": FRONTENDS["react-vite"]},
            {"generator": "crud", "features": features},
        )
        assert ("Auth" in handoff["not_generated"]) == ("auth" not in features)
        if "auth" in features:
            assert "backend/auth.py" in handoff["edit_targets"]["authentication"]
            assert (handoff["data_access"] == "private per-user records") == (
                "ownership" in features
            )


def test_authentication_boundaries_compose_without_combination_templates():
    registry = {
        **FEATURES,
        "additional": Feature(
            "additional",
            lambda spec: Contribution(
                frontend_boundaries=("./additional",), frontend_mounts=("./additional",)
            ),
            requires=("auth",),
        ),
    }
    react = build(
        spec(frontend="react-vite", features=["crud", "ownership", "additional"]),
        registry=registry,
    )
    assert (
        "<Boundary0 context={context}><Boundary1 context={context}>"
        in react.files["frontend/src/main.tsx"]
    )
    vanilla = build(
        spec(frontend="vite", features=["crud", "ownership", "additional"]),
        registry=registry,
    )
    assert (
        "mount0(target, context, ((target: HTMLElement) => mount1(target, context, mountApp)))"
        in vanilla.files["frontend/src/main.ts"]
    )


@pytest.mark.parametrize("features", [["crud", "auth"], ["crud", "ownership"]])
def test_generated_authentication_and_ownership_suites(tmp_path, features):
    write_project(tmp_path, spec(frontend="none", features=features).model_dump())
    sentinel = tmp_path / "live.sqlite3"
    sentinel.write_bytes(b"not the test database")
    result = run_generated_tests(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_bytes() == b"not the test database"


@pytest.mark.parametrize("frontend_stack", ["vite", "react-vite"])
def test_generated_authenticated_frontend_typecheck_dom_tests_and_build(
    tmp_path, frontend_stack
):
    modules = Path(
        os.environ.get(
            "SCAFFOLD_TEST_NODE_MODULES",
            str(Path(__file__).resolve().parents[2] / "frontend/node_modules"),
        )
    )
    if not (modules / "@tailwindcss/vite").exists() or any(
        not (modules / ".bin" / command).exists()
        for command in ("tsc", "vitest", "vite")
    ):
        pytest.skip("Generated frontend dependencies must be available")
    write_project(tmp_path, spec(frontend=frontend_stack).model_dump())
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").symlink_to(modules, target_is_directory=True)
    for command in ("tsc", "vitest", "vite"):
        args = {"tsc": ["--noEmit"], "vitest": ["run"], "vite": ["build"]}[command]
        result = subprocess.run(
            [str(modules / ".bin" / command), *args],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((tmp_path / ".realopen-app.json").read_text())
    assert "ownership" in manifest["features"]


@pytest.mark.skipif(
    os.environ.get("SCAFFOLD_INTEGRATION_TESTS") != "1",
    reason="Opt-in local backend/frontend servers",
)
@pytest.mark.parametrize("frontend_stack", ["react-vite", "vite"])
def test_cookie_sessions_and_ownership_through_real_vite_proxy(
    tmp_path, frontend_stack
):
    modules = Path(
        os.environ.get(
            "SCAFFOLD_TEST_NODE_MODULES",
            str(Path(__file__).resolve().parents[2] / "frontend/node_modules"),
        )
    )
    if not (modules / "@tailwindcss/vite").exists():
        pytest.skip("Generated frontend dependencies must be available")
    write_project(tmp_path, spec(frontend=frontend_stack).model_dump())
    (tmp_path / "frontend/node_modules").symlink_to(modules, target_is_directory=True)

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    backend_port, frontend_port = free_port(), free_port()
    config = tmp_path / "frontend/vite.config.ts"
    config.write_text(
        config.read_text().replace("127.0.0.1:6969", f"127.0.0.1:{backend_port}")
    )
    env = {
        **os.environ,
        "DATABASE_PATH": str(tmp_path / "isolated.sqlite3"),
        "PYTHONPATH": str(tmp_path),
    }
    processes = []
    try:
        processes.append(
            subprocess.Popen(
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
        )
        processes.append(
            subprocess.Popen(
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
        )
        base = f"http://127.0.0.1:{frontend_port}"
        with httpx.Client(base_url=base, timeout=3) as alice, httpx.Client(
            base_url=base, timeout=3
        ) as bob:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if alice.get("/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(
                        "Generated auth app/proxy did not become healthy"
                    )
                time.sleep(0.1)
            assert alice.get("/").status_code == 200
            for client, name in ((alice, "alice"), (bob, "bob")):
                client.headers["X-App-Request"] = "1"
                response = client.post(
                    "/api/auth/register",
                    json={"username": name, "password": "proxy-test-password"},
                )
                assert response.status_code == 201, response.text
                assert client.cookies
                client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            created = alice.post("/api/contacts", json={"name": "Private", "age": 1})
            assert created.status_code == 201, created.text
            identity = created.json()["id"]
            assert bob.get("/api/contacts").json()["total"] == 0
            assert (
                bob.patch(f"/api/contacts/{identity}", json={"age": 2}).status_code
                == 404
            )
            assert bob.delete(f"/api/contacts/{identity}").status_code == 404
            assert alice.get(f"/api/contacts/{identity}").json()["age"] == 1
            assert alice.post("/api/auth/logout").status_code == 204
            assert alice.get("/api/contacts").status_code == 401
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
