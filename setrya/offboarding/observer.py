"""Read both tenant controls through a dedicated SQLite reader."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def open_reader(path):
    """Open an existing SQLite database for SELECTs with a read-only URI."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as reader:
        reader.execute("PRAGMA query_only = ON")
        reader.set_authorizer(_read_access)
        yield reader


def _read_access(action, operation, name, *_):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_SAVEPOINT and name == "setrya_read_pair" and operation in ("BEGIN", "RELEASE"):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def read_pair(reader, query, tenants):
    """Run both tenant reads against one database snapshot."""
    subject, control = tenants
    if subject == control:
        raise ValueError("The retained tenant must differ from the departing tenant.")

    def read(tenant):
        try:
            row = reader.execute(query, (tenant,)).fetchone()
        except sqlite3.Error:
            return "unavailable"
        return "absent" if row is None else "present"

    try:
        # A savepoint also works inside a transaction owned by the caller.
        reader.execute("SAVEPOINT setrya_read_pair")
        try:
            return {"subject": read(subject), "control": read(control)}
        finally:
            reader.execute("RELEASE setrya_read_pair")
    except sqlite3.Error:
        return {"subject": "unavailable", "control": "unavailable"}
