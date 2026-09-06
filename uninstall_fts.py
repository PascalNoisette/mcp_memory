#!/usr/bin/env python3
"""
Uninstall script: remove the part_fts FTS5 table and its sync triggers.

This cleanly tears down the full-text search index that was created by
_database._init_fts5(). It does NOT touch the ``part`` source table or any
other schema objects -- only the FTS virtual table and the three triggers
that keep it in sync.

Usage
-----
  # Remove from the default database (DATABASE_PATH env or the default path):
  python uninstall_fts.py

  # Remove from a specific database file:
  python uninstall_fts.py /path/to/your.db

  # Dry-run (show what would be dropped without executing):
  python uninstall_fts.py --dry-run /path/to/your.db

Database path resolution (same as the application):
  1. First argument (positional), or
  2. DATABASE_PATH environment variable, or
  3. ~/.local/share/opencode/opencode-local.db

"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


# ---------------------------------------------------------------------------
# SQL statements -- order matters: triggers first, then the table
# ---------------------------------------------------------------------------

DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS part_fts_ai;",   # AFTER INSERT
    "DROP TRIGGER IF EXISTS part_fts_au;",   # AFTER UPDATE
    "DROP TRIGGER IF EXISTS part_fts_ad;",   # AFTER DELETE
)

DROP_FTS_TABLE = "DROP TABLE IF EXISTS part_fts;"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Re-use the shared resolver from database.py for the fallback path.
from database import resolve_db_path as _resolve_db_path_default


def resolve_db_path(cli_path: str | None) -> str:
    """Resolve the SQLite database path.

    Priority: CLI argument > DATABASE_PATH env var > default path.
    """
    if cli_path:
        return os.path.abspath(cli_path)

    # Delegate to the shared resolver (env var or default path).
    return os.path.abspath(_resolve_db_path_default())


def check_prerequisites(conn: sqlite3.Connection) -> None:
    """Verify the FTS table and triggers exist before attempting removal."""
    cur = conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table', 'trigger') AND name LIKE 'part_fts%'"
    )
    found = cur.fetchall()

    if not found:
        print("No FTS objects (part_fts table or triggers) found. Nothing to do.")
        sys.exit(0)

    print("Found the following FTS objects to remove:")
    for obj_type, obj_name in found:
        print(f"  - {obj_type}: {obj_name}")
    print()


def uninstall(conn: sqlite3.Connection) -> None:
    """Drop triggers (in any order) then the FTS table."""
    conn.execute("BEGIN")

    for sql in DROP_TRIGGERS:
        conn.execute(sql)

    conn.execute(DROP_FTS_TABLE)

    conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the part_fts FTS5 table and its sync triggers.",
    )
    parser.add_argument(
        "database",
        nargs="?",
        default=None,
        help="Path to the SQLite database file. "
             "Falls back to DATABASE_PATH env var or the default path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without actually doing it.",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.database)

    if not os.path.exists(db_path):
        print(f"Error: database file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Database: {db_path}")
    print()

    conn = sqlite3.connect(db_path)
    try:
        check_prerequisites(conn)

        if args.dry_run:
            print("[dry-run] Would execute:")
            for sql in DROP_TRIGGERS + (DROP_FTS_TABLE,):
                print(f"  {sql}")
        else:
            uninstall(conn)
            print("FTS objects removed successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
