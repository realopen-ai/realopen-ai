"""OCR engine is built in; additional trained data is user-installed."""

from pathlib import Path
from types import SimpleNamespace

from app.services import ocr, deps_manager


def test_both_images_ship_only_english():
    root = Path(__file__).parents[1]
    for name in ("Dockerfile", "Dockerfile.dev"):
        dockerfile = (root / name).read_text()
        assert "tesseract-ocr tesseract-ocr-eng" in dockerfile
        assert "tesseract-ocr-fra" not in dockerfile
        assert "tesseract-ocr-ara" not in dockerfile


def test_optional_languages_use_existing_dependency_install_flow(monkeypatch):
    monkeypatch.setattr(deps_manager, "_detect_distro", lambda: "debian")
    monkeypatch.setattr(ocr, "installed_languages", lambda: {"eng", "fra"})
    french = deps_manager.get_dependency("tesseract-ocr-fra")
    arabic = deps_manager.get_dependency("tesseract-ocr-ara")
    assert deps_manager.is_installed(french)
    assert not deps_manager.is_installed(arabic)
    assert deps_manager.is_available_for_distro(arabic)
    assert (
        deps_manager.get_install_command(arabic)
        == "apt-get install -y --no-install-recommends tesseract-ocr-ara"
    )
    assert ocr.default_language() == "eng+fra"
    monkeypatch.setattr(ocr, "installed_languages", lambda: {"eng", "ara", "fra"})
    assert ocr.default_language() == "eng+fra+ara"


def test_language_detection_uses_trained_data_not_engine_presence(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        ocr.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="List of available languages (2):\neng\nfra\nosd\n"
        ),
    )
    assert ocr.installed_languages() == {"eng", "fra"}
    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    assert ocr.installed_languages() == set()
    assert ocr.default_language() == "eng"
