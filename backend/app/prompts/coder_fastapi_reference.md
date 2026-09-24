Private reference for FastAPI + SQLite tasks. Adapt names and schemas instead of
copying blindly. Keep the application at the workspace root unless the user asks
for a package layout.

Recommended setup tool arguments:

```json
{
  "name": "notes-api",
  "dependencies": ["fastapi", "uvicorn"],
  "dev_dependencies": ["pytest", "httpx2"]
}
```

If a dependency is missing later, use `uv add PACKAGE` or
`uv add --dev PACKAGE`; never use pip. Useful commands:

```sh
uv run pytest -q
uv run python -m py_compile main.py
curl -fsS http://127.0.0.1:6969/health
```

Working single-file example:

```python
import sqlite3
from contextlib import closing

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()
DATABASE = "notes.db"


class NoteCreate(BaseModel):
    content: str = Field(min_length=1)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


@app.on_event("startup")
def create_schema() -> None:
    with closing(connect()) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS notes "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL)"
        )
        connection.commit()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/notes", status_code=201)
def create_note(note: NoteCreate) -> dict[str, object]:
    with closing(connect()) as connection:
        cursor = connection.execute(
            "INSERT INTO notes (content) VALUES (?)", (note.content,)
        )
        connection.commit()
        return {"id": cursor.lastrowid, "content": note.content}


@app.get("/notes")
def list_notes() -> list[dict[str, object]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            "SELECT id, content FROM notes ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]
```

Test with `from fastapi.testclient import TestClient`; `httpx2` must be in the
development dependencies. Start the tested API only with `start_preview`, using
`uv run uvicorn main:app --host 0.0.0.0 --port 6969` and port `6969`.
