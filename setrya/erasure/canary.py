"""Read-only exact-marker discovery in one declared SQLite text boundary."""
import re
import sqlite3

from setrya.drill import InputError
from .journal import identifier


class SQLiteCanaryScanner:
    def __init__(self, db: sqlite3.Connection, *, table: str, key: str, content: str, scope_column: str):
        names = table, key, content, scope_column
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in names):
            raise InputError("expected explicit SQLite table and column names")
        self.db = db
        # Use a qualified table name on every column so SQLite cannot treat a
        # misspelled double-quoted column as a literal string and return no hits.
        self.query = (f'SELECT t."{key}" FROM "{table}" t WHERE t."{scope_column}"=? '
                      f'AND instr(t."{content}",?)>0 LIMIT 4097')

    def find(self, *, scope: str, marker: str) -> set[str]:
        identifier(scope, "canary scope")
        if not isinstance(marker, str) or not marker.isprintable() or not 8 <= len(marker) <= 256:
            raise InputError("use a unique printable synthetic marker of 8 to 256 characters")
        rows = self.db.execute(self.query, (scope, marker)).fetchall()
        keys = {identifier(key, "canary copy key") for key, in rows}
        if len(rows) > 4096 or len(rows) != len(keys):
            raise InputError("canary scan needs at most 4096 distinct scoped keys")
        return keys
