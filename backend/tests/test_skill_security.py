"""Code-scanning security regression tests."""

import pytest
from app.services import skills


def test_skill_parent_symlink_cannot_read_outside_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    record = skills.save_skill(
        name="Demo", description="Demo", roles=[], enabled=True, content="Hello"
    )
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.md").write_text("private")
    (skills.SKILLS_DIR / record["id"] / "references").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic"):
        skills.read_resource("demo", "references/secret.md")


def test_skill_linked_instructions_cannot_be_read_or_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")
    directory = skills.skills_root() / "demo"
    directory.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("private")
    (directory / "SKILL.md").symlink_to(outside)
    with pytest.raises(ValueError, match="Symbolic"):
        skills.get_skill("demo")
    with pytest.raises(ValueError, match="Symbolic"):
        skills.save_skill(
            name="Demo", description="Demo", roles=[], enabled=True, content="replace"
        )
    assert outside.read_text() == "private"
    assert skills.list_skills() == []
