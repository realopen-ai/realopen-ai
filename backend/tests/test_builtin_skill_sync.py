"""Owned defaults upgrade safely; user-customized skill trees never do."""

import concurrent.futures
import json

import pytest

from app.services import builtin_skill_sync as sync


@pytest.fixture
def trees(tmp_path):
    root, bundled = tmp_path / "installed", tmp_path / "bundled"
    root.mkdir()
    source = bundled / "builder"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Default instructions v1")
    (source / "references").mkdir()
    (source / "references/api.md").write_text("Reference v1")
    return root, bundled, source


def test_owned_tree_upgrades_and_preserves_previous_version_backup(trees):
    root, bundled, source = trees
    sync.sync(root, bundled)
    original = sync.snapshot(root / "builder")
    (source / "SKILL.md").write_text("Default instructions v2")
    (source / "references/api.md").write_text("Reference v2")
    result = sync.sync(root, bundled)
    assert (root / "builder/SKILL.md").read_text() == "Default instructions v2"
    assert result["skills"]["builder"]["files"] == sync.snapshot(source)
    backup = next((root / ".builtin-backups/builder").iterdir())
    assert sync.snapshot(backup) == original
    assert sync.sync(root, bundled) == result


@pytest.mark.parametrize(
    "change", ["body", "disabled", "reference", "added", "deleted"]
)
def test_any_user_customization_keeps_entire_skill_tree(trees, change):
    root, bundled, source = trees
    sync.sync(root, bundled)
    target = root / "builder"
    if change == "body":
        (target / "SKILL.md").write_text("User instructions")
    elif change == "disabled":
        (target / "SKILL.md").write_text("enabled: false\nDefault instructions v1")
    elif change == "reference":
        (target / "references/api.md").write_text("Customized reference")
    elif change == "added":
        (target / "custom.md").write_text("User reference")
    else:
        (target / "references/api.md").unlink()
    before = sync.snapshot(target)
    (source / "SKILL.md").write_text("New default")
    result = sync.sync(root, bundled)
    assert sync.snapshot(target) == before
    assert result["skills"]["builder"]["customized"]
    assert result["skills"]["builder"]["update_available"]


def test_untracked_different_copies_are_preserved(trees):
    root, bundled, source = trees
    target = root / "builder"
    target.mkdir()
    (target / "SKILL.md").write_text("Untracked instructions")
    old = sync.snapshot(target)
    result = sync.sync(root, bundled)
    assert sync.snapshot(target) == old
    assert result["skills"]["builder"]["customized"]


def test_untracked_matching_default_records_hashes_for_future_updates(trees):
    root, bundled, source = trees
    target = root / "builder"
    for path in source.rglob("*"):
        if path.is_file():
            installed = target / path.relative_to(source)
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(path.read_bytes())
    result = sync.sync(root, bundled)
    assert result["skills"]["builder"]["files"] == sync.snapshot(source)
    assert not result["skills"]["builder"]["customized"]
    (source / "SKILL.md").write_text("Updated default")
    sync.sync(root, bundled)
    assert sync.snapshot(target) == sync.snapshot(source)


def test_custom_copies_and_linked_resources_never_get_overwritten(trees, tmp_path):
    root, bundled, source = trees
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("Private")
    (root / "builder").symlink_to(outside, target_is_directory=True)
    sync.sync(root, bundled)
    assert (outside / "SKILL.md").read_text() == "Private"
    (root / "builder").unlink()
    sync.sync(root, bundled)
    (root / "builder/references/api.md").unlink()
    (root / "builder/references/api.md").symlink_to(outside / "SKILL.md")
    (source / "SKILL.md").write_text("Updated")
    sync.sync(root, bundled)
    assert (root / "builder/references/api.md").is_symlink()
    assert (outside / "SKILL.md").read_text() == "Private"


def test_corrupt_provenance_cannot_authorize_overwriting_custom_content(trees):
    root, bundled, source = trees
    target = root / "builder"
    target.mkdir()
    (target / "SKILL.md").write_text("Custom")
    (root / sync.MANIFEST).write_text("invalid JSON")
    sync.sync(root, bundled)
    assert (target / "SKILL.md").read_text() == "Custom"


def test_concurrent_syncs_leave_valid_owned_state(trees):
    root, bundled, source = trees
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: sync.sync(root, bundled), range(12)))
    manifest = json.loads((root / sync.MANIFEST).read_text())
    assert manifest["skills"]["builder"]["files"] == sync.snapshot(source)
    assert sync.snapshot(root / "builder") == sync.snapshot(source)


def test_interrupted_update_can_resume_but_never_overwrites_intervening_user_edits(
    trees, monkeypatch
):
    root, bundled, source = trees
    sync.sync(root, bundled)
    (source / "SKILL.md").write_text("Default v2")
    (source / "references/api.md").write_text("Reference v2")
    original = sync.atomic_write

    def fail_reference(path, content):
        if path == root / "builder/references/api.md":
            raise OSError("Disk temporarily unavailable")
        return original(path, content)

    monkeypatch.setattr(sync, "atomic_write", fail_reference)
    with pytest.raises(OSError):
        sync.sync(root, bundled)
    monkeypatch.setattr(sync, "atomic_write", original)
    assert json.loads((root / sync.MANIFEST).read_text())["skills"]["builder"][
        "pending_files"
    ]
    sync.sync(root, bundled)
    assert sync.snapshot(root / "builder") == sync.snapshot(source)
    (source / "SKILL.md").write_text("Default v3")
    (source / "references/api.md").write_text("Reference v3")
    monkeypatch.setattr(sync, "atomic_write", fail_reference)
    with pytest.raises(OSError):
        sync.sync(root, bundled)
    monkeypatch.setattr(sync, "atomic_write", original)
    (root / "builder/references/api.md").write_text(
        "User customization after interruption"
    )
    before = sync.snapshot(root / "builder")
    sync.sync(root, bundled)
    assert sync.snapshot(root / "builder") == before
