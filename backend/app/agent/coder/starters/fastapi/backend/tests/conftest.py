import pytest
from fastapi.testclient import TestClient
from backend import db


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    # Patch the module, not an imported copy of DATABASE. Never unlink app data.
    monkeypatch.setattr(db, "DATABASE", tmp_path / "test.sqlite3")


@pytest.fixture
def client(isolated_database):
    # Import after patching too: app initialization must not reach live data,
    # even if implementation code adds import-time database initialization.
    from backend.app import app

    # Lifespan runs only after the temporary database has been selected.
    with TestClient(app) as test_client:
        yield test_client
