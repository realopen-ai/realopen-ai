import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.skills import router
from app.agent.base import ToolType
from app.agent.tools.load_skill import LoadSkillTool
from app.services import skills


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")


def _skill_markdown(name="FastAPI helper", roles=None, body="# Secret instructions"):
    role_lines = "\n".join(f"  - {role}" for role in (roles or []))
    roles_yaml = f"\n{role_lines}" if role_lines else " []"
    return (
        "---\n"
        f"name: {name}\n"
        "description: Build small FastAPI services\n"
        f"roles:{roles_yaml}\n"
        "enabled: true\n"
        "---\n\n"
        f"{body}\n"
    )


def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_routing_exposes_metadata_and_loads_full_content_on_demand():
    skills.save_skill(
        name="FastAPI helper",
        description="Build small FastAPI services",
        roles=["coder"],
        enabled=True,
        content="# Secret instructions\nUse the bundled template.",
    )

    catalog = skills.routing_catalog("coder")
    assert "fastapi-helper: Build small FastAPI services" in catalog
    assert "Secret instructions" not in catalog
    assert skills.routing_catalog("general") == ""

    loaded = skills.load_for_agent("fastapi-helper", "coder")
    assert "# Secret instructions" in loaded
    with pytest.raises(ValueError, match="unavailable"):
        skills.load_for_agent("fastapi-helper", "general")


def test_global_skill_is_available_to_every_role():
    skills.save_skill(
        name="Writing",
        description="Improve prose",
        roles=[],
        enabled=True,
        content="Be direct.",
    )
    assert "writing" in skills.routing_catalog("general")
    assert "writing" in skills.routing_catalog("coder")
    assert "writing" in skills.routing_catalog("voice")


def test_zip_import_keeps_relative_resources():
    imported = skills.import_zip(
        _zip(
            {
                "my-skill/SKILL.md": _skill_markdown(),
                "my-skill/scripts/check.py": "print('ok')",
                "my-skill/references/guide.md": "Guide",
            }
        )
    )
    assert imported["id"] == "fastapi-helper"
    assert imported["files"] == [
        "SKILL.md",
        "references/guide.md",
        "scripts/check.py",
    ]
    assert (
        skills.load_for_agent("fastapi-helper", "general", "references/guide.md")
        == "Guide"
    )
    with pytest.raises(ValueError, match="Invalid"):
        skills.load_for_agent("fastapi-helper", "general", "../secret")


def test_zip_import_rejects_traversal():
    archive = _zip(
        {
            "my-skill/SKILL.md": _skill_markdown(),
            "my-skill/../../escaped.txt": "nope",
        }
    )
    with pytest.raises(ValueError, match="unsafe path"):
        skills.import_zip(archive)
    assert not (skills.skills_root().parent / "escaped.txt").exists()


def test_folder_import_accepts_browser_relative_paths():
    imported = skills.import_files(
        [
            ("downloaded/SKILL.md", _skill_markdown(name="Downloaded").encode()),
            ("downloaded/assets/example.txt", b"example"),
        ]
    )
    assert imported["id"] == "downloaded"
    assert imported["files"] == ["SKILL.md", "assets/example.txt"]


def test_disabled_skill_is_not_routed_or_loaded():
    skills.save_skill(
        name="Off",
        description="Disabled skill",
        roles=[],
        enabled=False,
        content="Do not load.",
    )
    assert skills.routing_catalog("general") == ""
    with pytest.raises(ValueError, match="disabled"):
        skills.load_for_agent("off", "general")


def test_skill_loader_has_a_dedicated_tool_call_type():
    assert LoadSkillTool.tool_type is ToolType.SKILL


def test_skills_api_create_list_update_and_delete():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    payload = {
        "name": "Review",
        "description": "Review code changes",
        "roles": ["coder"],
        "enabled": True,
        "content": "Check tests.",
    }

    assert client.post("/api/skills", json=payload).status_code == 200
    listed = client.get("/api/skills").json()
    assert listed[0]["id"] == "review"
    payload["content"] = "Check tests and types."
    assert (
        client.put("/api/skills/review", json=payload).json()["content"]
        == payload["content"]
    )
    assert client.delete("/api/skills/review").status_code == 204
    assert client.get("/api/skills/review").status_code == 404


def test_skills_api_previews_text_resources():
    skills.import_files(
        [
            ("preview/SKILL.md", _skill_markdown(name="Preview").encode()),
            ("preview/references/example.md", b"# Example\n\nHello"),
        ]
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    response = client.get("/api/skills/preview/resources/references/example.md")
    assert response.status_code == 200
    assert response.text == "# Example\n\nHello"
