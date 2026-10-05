"""Code-scanning security regression tests."""

from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.api import reports
from app.services.integrations import libreoffice


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["../private", "a/b", "a\\b", "demo\n", "", "."])
async def test_report_identifiers_reject_unsafe_values(identifier):
    with pytest.raises(HTTPException) as caught:
        await reports.download_report(identifier)
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_report_download_does_not_follow_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "_get_data_dir", lambda: tmp_path)
    (tmp_path / "reports").mkdir()
    private = tmp_path / "private.pdf"
    private.write_bytes(b"private")
    (tmp_path / "reports" / "demo.pdf").symlink_to(private)
    with pytest.raises(HTTPException) as caught:
        await reports.download_report("demo")
    assert caught.value.status_code == 400


def test_slide_manifest_does_not_follow_symlink(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    private = tmp_path / "private.json"
    private.write_text('{"version": 1, "count": 1}')
    (cache / "manifest.json").symlink_to(private)
    with pytest.raises(ValueError, match="Symbolic"):
        libreoffice.read_slides_manifest(cache)


@pytest.mark.asyncio
async def test_report_slide_does_not_follow_symlink(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(reports, "_get_data_dir", lambda: tmp_path)
    cache = tmp_path / "reports" / "demo_slides"
    cache.mkdir(parents=True)
    (cache / "manifest.json").write_text(json.dumps({"version": libreoffice.SLIDES_MANIFEST_VERSION, "count": 1}))
    private = tmp_path / "private.jpg"
    private.write_bytes(b"private")
    (cache / "slide_001.jpg").symlink_to(private)
    with pytest.raises(HTTPException) as caught:
        await reports.get_report_slide_image("demo", 1, variant="full")
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_pdf_conversion_rejects_linked_output_before_running_converter(tmp_path, monkeypatch):
    monkeypatch.setattr(libreoffice, "is_available", lambda: True)
    converter = AsyncMock()
    monkeypatch.setattr(libreoffice, "_run_soffice", converter)
    source = tmp_path / "demo.pptx"
    source.write_bytes(b"source")
    private = tmp_path / "private.pdf"
    private.write_bytes(b"private")
    source.with_suffix(".pdf").symlink_to(private)
    with pytest.raises(ValueError, match="Symbolic"):
        await libreoffice.convert_pptx_to_pdf(source)
    converter.assert_not_called()
    assert private.read_bytes() == b"private"
