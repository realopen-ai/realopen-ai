"""Code-scanning security regression tests."""




def test_voice_runtime_probe_does_not_expose_internal_exception(monkeypatch):
    from app.services import voice_model_installer
    from app.voice import models_store

    def fail():
        raise RuntimeError("SECRET /private/path")

    monkeypatch.setattr(voice_model_installer, "voice_runtime_entries", fail)
    assert "SECRET" not in str(models_store._missing_runtime_entries())
