# FastAPI / SQLite reference

The starter uses /workspace/backend/app.py, backend/db.py and backend/tests/test_api.py; pyproject.toml is at /workspace. Run `uv run python -m pytest backend/tests -q`. Use `uv add` for dependencies instead of writing pyproject.toml.

Keep routes under /api. Use typed Pydantic inputs and explicit HTTP errors; never concatenate SQL values. The database helper supplies a sqlite3.Row connection and transaction:

```python
from fastapi import HTTPException
from pydantic import BaseModel
from backend.db import connection

class ItemInput(BaseModel):
    title: str

@app.post("/api/items", status_code=201)
def create_item(item: ItemInput):
    title = item.title.strip()
    if not title:
        raise HTTPException(422, "Title is required")
    with connection() as db:
        cursor = db.execute("INSERT INTO items (title) VALUES (?)", (title,))
        return {"id": cursor.lastrowid, "title": title}
```

This example assumes the application creates the items table during startup. Adapt tables/routes to the request. Use FastAPI lifespan for initialization. Isolate test databases with pytest temporary paths and monkeypatch the db module's DATABASE; never erase real workspace data for tests. Use TestClient as a context manager to run lifespan. Test the requested behavior, invalid input and persistence. Include httpx2 for current Starlette TestClient support.

Start via start_preview: `uv run uvicorn backend.app:app --host 0.0.0.0 --port 6969`.
