"""Native file serving and dependency streams keep filesystem details private."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps as deps_api
from app.api.frontend import install_frontend
from app.services import deps_manager


@pytest.fixture
def frontend(tmp_path):
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text("Application shell")
    (root / "assets").mkdir()
    (root / "assets/app.js").write_text("Application script")
    outside = tmp_path / "frontend-private"
    outside.mkdir()
    (outside / "secret.txt").write_text("Private file")
    app = FastAPI()
    install_frontend(app, root)
    return app, root, outside


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/frontend-private/secret.txt",
        "/assets/%2e%2e/%2e%2e/frontend-private/secret.txt",
        "/%2fetc/passwd",
        "/%2e%2e%2ffrontend-private%2fsecret.txt",
    ],
)
def test_native_frontend_rejects_unsafe_paths(frontend, path):
    app, _, _ = frontend
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert "Private file" not in response.text


@pytest.mark.parametrize("directory_link", [False, True])
def test_native_frontend_rejects_symlink_assets(frontend, directory_link):
    app, root, outside = frontend
    link = root / "assets" / "linked"
    try:
        link.symlink_to(
            outside if directory_link else outside / "secret.txt",
            target_is_directory=directory_link,
        )
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    path = "/assets/linked/secret.txt" if directory_link else "/assets/linked"
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert "Private file" not in response.text


def test_native_frontend_keeps_assets_and_spa_routes(frontend):
    app, _, _ = frontend
    with TestClient(app) as client:
        assert client.get("/").text == "Application shell"
        assert client.get("/learn/notes/123").text == "Application shell"
        assert client.get("/assets/app.js").text == "Application script"
        assert client.get("/assets/missing.js").status_code == 404


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_dependency_planning_errors_stay_in_server_logs(monkeypatch, caplog, operation):
    detail = "Internal failure at /private/config with secret-token-value"

    def invalid_plan(*args, **kwargs):
        raise ValueError(detail)

    monkeypatch.setattr(deps_manager, "_system_commands", invalid_plan)
    worker = getattr(deps_manager, f"_{operation}_system_direct")
    monkeypatch.setattr(
        deps_api, f"{operation}_dependency", lambda dep: worker(dep, "macos")
    )
    app = FastAPI()
    app.include_router(deps_api.router, prefix="/api")
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        url = f"/api/deps/libreoffice/{operation}"
        response = client.post(url) if operation == "install" else client.delete(url)
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["stage"] == "error"
    assert "Check server logs" in events[-1]["error"]
    assert detail not in response.text
    assert "secret-token-value" not in response.text
    assert detail in caplog.text
