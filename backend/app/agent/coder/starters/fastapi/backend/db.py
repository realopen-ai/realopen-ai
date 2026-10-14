from contextlib import contextmanager
from pathlib import Path
import sqlite3

DATABASE = Path(__file__).resolve().parent / "app.sqlite3"


@contextmanager
def connection():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()
