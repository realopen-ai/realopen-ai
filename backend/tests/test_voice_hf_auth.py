from app.voice import hf_cache


def test_ensure_hf_env_bridges_cli_token(tmp_path, monkeypatch):
    cli_home = tmp_path / "user"
    token_file = cli_home / ".cache" / "huggingface" / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("hf_test_token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(cli_home))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "project-cache"))
    monkeypatch.delenv("HF_TOKEN", raising=False)

    hf_cache.ensure_hf_env()

    assert hf_cache.os.environ["HF_TOKEN"] == "hf_test_token"


def test_ensure_hf_env_preserves_explicit_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "project-cache"))
    monkeypatch.setenv("HF_TOKEN", "hf_operator_token")

    hf_cache.ensure_hf_env()

    assert hf_cache.os.environ["HF_TOKEN"] == "hf_operator_token"
