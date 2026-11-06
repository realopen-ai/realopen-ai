"""Generators and download routes must agree on the persisted file location."""

import io
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pptx import Presentation

from app.config import settings
from app.agent.tracing import Trace
from app.api import reports
from app.services import pptx_gen, report_gen, excel_gen
from app.services.integrations import libreoffice


@pytest.mark.parametrize("service", [pptx_gen, report_gen, excel_gen])
def test_generator_output_matches_download_storage(service, tmp_path, monkeypatch):
    directory = tmp_path / "configured shared data"
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(directory))
    assert service._get_reports_dir() == reports._get_data_dir() / "reports"
    assert service._get_reports_dir().is_dir()


@pytest.mark.asyncio
async def test_presentation_download_works_without_libreoffice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        pptx_gen,
        "_generate_slide_markdown",
        AsyncMock(
            return_value="# Test deck\n- Intro\n\n---\n\n## Details\n- A useful point"
        ),
    )
    monkeypatch.setattr(libreoffice, "is_available", lambda: False)
    generated = await pptx_gen.generate_presentation("Storage regression")
    assert (tmp_path / generated["file_path"]).is_file()
    assert (tmp_path / "reports" / f"{generated['report_id']}.source.json").is_file()
    app = FastAPI()
    app.include_router(reports.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get(generated["download_url"])
    assert response.status_code == 200
    assert len(Presentation(io.BytesIO(response.content)).slides) == 2


def test_native_defaults_are_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "REALOPEN_RUNTIME", "native")
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", "")
    monkeypatch.setattr(settings, "REALOPEN_STATE_DIR", "")
    root = Path(__file__).resolve().parents[2] / "data"
    assert settings.get_data_dir() == root
    assert settings._get_state_dir() == root / "state"
    assert not (tmp_path / "data").exists()


def test_state_and_traces_follow_configured_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "REALOPEN_STATE_DIR", "")
    monkeypatch.setattr(settings, "AGENT_TRACE_DIR", "")
    assert settings._get_state_dir() == tmp_path / "state"
    trace = Trace({"messages": [{"role": "user", "content": "Storage test"}]})
    assert trace.directory == str(tmp_path / "agent_traces")


def test_custom_template_migration_preserves_originals_without_resurrecting_deleted_files(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "old templates"
    (legacy / "custom").mkdir(parents=True)
    source = legacy / "custom" / "example.pptx"
    source.write_bytes(b"legacy template")
    monkeypatch.setattr(pptx_gen, "_get_templates_dir", lambda: legacy)
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path / "data"))
    directory = pptx_gen.get_custom_templates_dir()
    target = directory / source.name
    assert target.read_bytes() == source.read_bytes()
    target.write_bytes(b"updated template")
    assert pptx_gen.get_custom_templates_dir() == directory
    assert target.read_bytes() == b"updated template"
    target.unlink()
    pptx_gen.get_custom_templates_dir()
    assert not target.exists()
    assert source.read_bytes() == b"legacy template"
