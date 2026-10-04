"""Tests for the Load skill agent tool (app/agent/tools/load_skill.py).

Scope:
- Tool schema: parameters (name + resource), required params, tool type.
- execute() against the REAL filesystem-backed skills service running in
  an isolated tmp directory: loading an existing skill's instructions,
  loading a bundled text resource, and every error branch the tool maps
  to a failed ToolResult — missing skill (FileNotFoundError), role
  restrictions / disabled skills / oversized instructions / unsafe
  resource paths (ValueError).

Mocks:
- None for the tool itself: app.services.skills runs for real against a
  per-test SKILLS_DIR (monkeypatched to tmp_path, same pattern as
  tests/test_skills.py). No DB, no network.
"""

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.agent.tools  # noqa: E402,F401
from app.agent.base import ToolType, get_tool_registry  # noqa: E402
from app.agent.tools.load_skill import LoadSkillTool  # noqa: E402
from app.services import skills  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path, monkeypatch):
    """Point the skills service at a per-test directory."""
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")


def _make_skill(name="FastAPI helper", roles=None, enabled=True, body="# Secret doc"):
    skills.save_skill(
        name=name,
        description="Build small services",
        roles=roles if roles is not None else [],
        enabled=enabled,
        content=body,
    )


# ── Schema ──────────────────────────────────────────────────────────


class TestLoadSkillSchema:
    def test_parameters_and_required_params(self):
        tool = LoadSkillTool()
        params = tool.get_parameters()
        assert set(params) == {"name", "resource"}
        assert params["name"]["type"] == "string"
        assert tool.get_required_params() == ["name"]

    def test_tool_metadata_and_registry(self):
        tool = LoadSkillTool()
        assert tool.name == "load_skill"
        assert tool.tool_type is ToolType.SKILL
        assert get_tool_registry().has_tool("load_skill")


# ── execute() ───────────────────────────────────────────────────────


class TestLoadSkillExecute:
    @pytest.mark.asyncio
    async def test_loads_existing_skill_instructions(self):
        _make_skill(body="# Secret instructions\nUse the template.")

        result = await LoadSkillTool().execute(name="fastapi-helper")

        assert result.success is True
        assert "# Secret instructions" in result.output
        assert result.output.startswith("Skill: FastAPI helper")
        assert "Bundled resources" in result.output
        call = result.tool_call
        assert call.status == "completed"
        assert call.error is None
        assert call.output == result.output
        assert call.title == "Loading skill fastapi-helper"

    @pytest.mark.asyncio
    async def test_loads_bundled_resource(self):
        skills.import_files(
            [
                (
                    "packed/SKILL.md",
                    (
                        "---\nname: Packed\ndescription: d\nroles: []\n"
                        "enabled: true\n---\n\n# Body\n"
                    ).encode(),
                ),
                ("packed/references/guide.md", b"Guide contents"),
            ]
        )

        result = await LoadSkillTool().execute(
            name="packed", resource="references/guide.md"
        )

        assert result.success is True
        assert result.output == "Guide contents"

    @pytest.mark.asyncio
    async def test_missing_skill_returns_failure(self):
        result = await LoadSkillTool().execute(name="does-not-exist")

        assert result.success is False
        assert result.output.startswith("Could not load skill:")
        assert "does-not-exist" in result.output
        call = result.tool_call
        assert call.status == "error"
        assert "does-not-exist" in call.error

    @pytest.mark.asyncio
    async def test_role_restricted_skill_returns_failure(self):
        _make_skill(name="Coders only", roles=["coder"])

        result = await LoadSkillTool().execute(name="coders-only", _skill_role="general")

        assert result.success is False
        assert "Could not load skill:" in result.output
        assert "unavailable" in result.output

    @pytest.mark.asyncio
    async def test_role_restricted_skill_loads_for_matching_role(self):
        _make_skill(name="Coders only", roles=["coder"])

        result = await LoadSkillTool().execute(name="coders-only", _skill_role="coder")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_disabled_skill_returns_failure(self):
        _make_skill(name="Off skill", enabled=False)

        result = await LoadSkillTool().execute(name="off-skill")

        assert result.success is False
        assert "disabled" in result.output

    @pytest.mark.asyncio
    async def test_unsafe_resource_path_returns_failure(self):
        _make_skill()

        result = await LoadSkillTool().execute(
            name="fastapi-helper", resource="../../etc/passwd"
        )

        assert result.success is False
        assert "Could not load skill:" in result.output

    @pytest.mark.asyncio
    async def test_missing_resource_returns_failure(self):
        _make_skill()

        result = await LoadSkillTool().execute(
            name="fastapi-helper", resource="references/missing.md"
        )

        assert result.success is False
        assert "Could not load skill:" in result.output

    @pytest.mark.asyncio
    async def test_oversized_skill_returns_failure(self):
        root = skills.skills_root() / "huge"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            "---\nname: Huge\ndescription: d\nroles: []\nenabled: true\n---\n\n"
            + "x" * (skills.MAX_INSTRUCTIONS + 1),
            encoding="utf-8",
        )

        result = await LoadSkillTool().execute(name="huge")

        assert result.success is False
        assert "too large" in result.output

    @pytest.mark.asyncio
    async def test_default_role_is_general(self):
        _make_skill(name="General use", roles=["general"])

        # No explicit _skill_role → defaults to "general" → allowed.
        result = await LoadSkillTool().execute(name="general-use")
        assert result.success is True
