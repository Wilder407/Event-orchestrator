"""Create (or reset) the SQLite database from schema.sql.

Usage:
    python init_db.py           # create events.db if missing
    python init_db.py --reset   # delete and rebuild (handy between demo runs)
"""
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("EVENTS_DB", HERE / "events.db"))
SCHEMA_PATH = HERE / "schema.sql"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset=False):
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with get_conn() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
    print(f"Database ready at {DB_PATH}")


if __name__ == "__main__":
    init_db(reset="--reset" in sys.argv)
