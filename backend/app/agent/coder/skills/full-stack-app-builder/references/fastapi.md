# FastAPI / SQLite reference

The minimal starter has backend/app.py with GET /api/health and pyproject.toml at /workspace. It does not generate a database helper, repositories, application routes or tests. Implement only what the user requested, keeping a small app in app.py unless a separate module genuinely helps. Use uv add for new dependencies, not hand-written packaging.

Keep API routes under /api. Use typed Pydantic inputs, explicit HTTP errors and parameterized SQL values. For requested persistence, Python's sqlite3 is sufficient for a small app. Initialize tables through FastAPI lifespan and make the database location configurable so tests can use a temporary path. Return a newly created row using its actual inserted ID, not an arbitrary previous row. Adapt schemas and routes to the request.

Create backend/tests yourself. Set the temporary database configuration before importing or starting the app, using tmp_path and monkeypatch or an app factory/dependency override. Never unlink, truncate or connect tests to the live application's database. Use TestClient as a context manager to run lifespan. Test real requested API behavior, validation, persistence and user isolation when applicable; a health check is not application evidence. The installed httpx2 supports the current Starlette TestClient.

Run uv run python -m pytest backend/tests -q. Start the real preview with start_preview: uv run uvicorn backend.app:app --host 0.0.0.0 --port 6969. Existing repositories keep their own database and application structure.
