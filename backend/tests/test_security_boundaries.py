"""Code-scanning security regression tests."""

import pytest
from app.services.safe_paths import bounded_text, confined_path


@pytest.mark.parametrize("relative", ["../outside", "/etc/passwd", "a/../../outside"])
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
