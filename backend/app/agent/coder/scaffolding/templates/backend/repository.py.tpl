"""SQLite persistence. Values are bound parameters; identifiers come from the schema."""

import re

from backend.db import connection


def quoted(identifier):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", identifier):
        raise ValueError("Invalid schema identifier")
    return '"' + identifier + '"'


class Repository:
    def __init__(self, name, fields):
        self.table = quoted(name)
        self.fields = {f["name"]: f for f in fields}
        self.columns = {"id": '"id"', **{name: quoted(name) for name in self.fields}}

    def initialize(self):
        types = {
            "string": "TEXT",
            "integer": "INTEGER",
            "number": "REAL",
            "boolean": "INTEGER",
        }
        definitions = ['"id" INTEGER PRIMARY KEY AUTOINCREMENT']
        for name, field in self.fields.items():
            definition = self.columns[name] + " " + types[field["type"]]
            if field["required"]:
                definition += " NOT NULL"
            definitions.append(definition)
        with connection() as db:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} ({', '.join(definitions)})"
            )

    def decode(self, row):
        if row is None:
            return None
        result = dict(row)
        for name, field in self.fields.items():
            if field["type"] == "boolean" and result[name] is not None:
                result[name] = bool(result[name])
        return result

    def list(self, search="", sort="id", descending=False, limit=25, offset=0):
        # Only map to trusted schema identifiers, never splice a query value.
        column = self.columns[sort]
        direction = "DESC" if descending else "ASC"
        text_columns = [
            self.columns[name]
            for name, field in self.fields.items()
            if field["type"] == "string"
        ]
        where, parameters = "", []
        if search and text_columns:
            where = " WHERE " + " OR ".join(
                f"{name} LIKE ? ESCAPE '\\'" for name in text_columns
            )
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters = ["%" + escaped + "%"] * len(text_columns)
        elif search:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        with connection() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM {self.table}{where}", parameters
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM {self.table}{where} ORDER BY {column} {direction}, id ASC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        return {
            "items": [self.decode(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get(self, record_id):
        with connection() as db:
            return self.decode(
                db.execute(
                    f"SELECT * FROM {self.table} WHERE id = ?", (record_id,)
                ).fetchone()
            )

    def create(self, values):
        names = [name for name in self.fields if name in values]
        columns = ", ".join(self.columns[name] for name in names)
        placeholders = ", ".join("?" for _ in names)
        with connection() as db:
            cursor = db.execute(
                f"INSERT INTO {self.table} ({columns}) VALUES ({placeholders})",
                [values[name] for name in names],
            )
            return self.decode(
                db.execute(
                    f"SELECT * FROM {self.table} WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
            )

    def update(self, record_id, values):
        names = [name for name in self.fields if name in values]
        with connection() as db:
            if names:
                assignments = ", ".join(self.columns[name] + " = ?" for name in names)
                db.execute(
                    f"UPDATE {self.table} SET {assignments} WHERE id = ?",
                    [*[values[name] for name in names], record_id],
                )
            return self.decode(
                db.execute(
                    f"SELECT * FROM {self.table} WHERE id = ?", (record_id,)
                ).fetchone()
            )

    def delete(self, record_id):
        with connection() as db:
            return (
                db.execute(
                    f"DELETE FROM {self.table} WHERE id = ?", (record_id,)
                ).rowcount
                > 0
            )
