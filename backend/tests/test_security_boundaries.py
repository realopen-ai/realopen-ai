"""Code-scanning security regression tests."""

import pytest
from app.services.safe_paths import bounded_text, confined_path


@pytest.mark.parametrize("relative", ["../outside", "/etc/passwd", "a/../../outside", "", "."])
def test_confined_path_rejects_traversal(tmp_path, relative):
    with pytest.raises(ValueError):
        confined_path(tmp_path, relative)


@pytest.mark.parametrize("dangling", [False, True])
def test_confined_path_rejects_parent_symlinks(tmp_path, dangling):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic"):
        confined_path(root, "linked/secret")


def test_bounded_text_enforces_byte_limit(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="large"):
        bounded_text(file, 10)
    assert bounded_text(file, 11) == "x" * 11


def test_confined_path_returns_normalized_descendant(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    assert confined_path(root, "nested/./file") == root / "nested/file"
    with pytest.raises(ValueError):
        confined_path(root, "../storage-extra/file")


def test_confined_path_rejects_file_and_root_symlinks(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    (root / "file").symlink_to(outside)
    with pytest.raises(ValueError, match="Symbolic"):
        confined_path(root, "file")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="Symbolic"):
        confined_path(linked_root, "other")
