"""Code-scanning security regression tests."""

from app.services import secrets


def test_secret_names_and_parent_links_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_state_dir", lambda: tmp_path)
    assert not secrets.set_secret("tool\n", "key", "value")
    assert not secrets.set_secret("tool", "../key", "value")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "key").write_text("private")
    (tmp_path / "tool_secrets").mkdir()
    (tmp_path / "tool_secrets" / "tool").symlink_to(outside, target_is_directory=True)
    assert secrets.get_secret("tool", "key") is None
    assert not secrets.set_secret("tool", "key", "replace")
    assert not secrets.clear_secret("tool", "key")
    assert (outside / "key").read_text() == "private"
