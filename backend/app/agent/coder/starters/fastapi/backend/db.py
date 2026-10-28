from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3

DATABASE = Path(
    os.environ.get("DATABASE_PATH", Path(__file__).resolve().parent / "app.sqlite3")
)


@contextmanager
def connection():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()
