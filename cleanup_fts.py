#!/usr/bin/env python3
"""
Cleanup / reset script for the **secondary** FTS database.

This removes the FTS index database file entirely.  On the next MCP server
start the ``syncer`` thread will perform a full resync from the primary
database.

This tool does **NOT** touch the primary opencode database in any way.

Usage
-----
  # Remove the FTS database (default path):
  python cleanup_fts.py

  # Remove from a specific FTS database path:
  python cleanup_fts.py --fts-db /path/to/fts.db

  # Show sync status without removing:
  python cleanup_fts.py --status

  # Dry-run (show what would be removed):
  python cleanup_fts.py --dry-run

Database path resolution:
  1. ``--fts-db`` CLI argument (if provided)
  2. ``FTS_DB_PATH`` environment variable
  3. Derived from ``DATABASE_PATH`` (appends ``_fts.db``)
  4. Default: ``~/.local/share/opencode/opencode-local.db_fts``

"""

from __future__ import annotations

import argparse
import os
import sys

from database import resolve_db_path, resolve_fts_db_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset or inspect the secondary FTS database for the Kilo Code MCP Memory Server.",
    )
    parser.add_argument(
        "--fts-db",
        default=None,
        help="Path to the FTS database file. "
             "Falls back to FTS_DB_PATH env var or derived from DATABASE_PATH.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without actually doing it.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show FTS sync status without removing anything.",
    )
    args = parser.parse_args()

    fts_db_path = args.fts_db or resolve_fts_db_path()
    primary_path = resolve_db_path()

    print(f"Primary database: {primary_path}")
    print(f"FTS database:     {fts_db_path}")
    print()

    # Status mode
    if args.status:
        from syncer import fts_sync_status

        status = fts_sync_status(fts_db_path)
        if status is None:
            print("FTS database does not exist. No data to display.")
            return

        print("FTS Sync Status:")
        print(f"  Last sync timestamp (time_created): {status.get('last_sync_ts', 'N/A')}")
        print(f"  Last sync rowid:                    {status.get('last_sync_rowid', 'N/A')}")
        print(f"  Total indexed parts:                {status.get('total_indexed', 'N/A')}")
        return

    # Dry-run
    if args.dry_run:
        if not os.path.exists(fts_db_path):
            print("FTS database does not exist. Nothing to remove.")
            return

        size = os.path.getsize(fts_db_path)
        print(f"[dry-run] Would remove: {fts_db_path}")
        print(f"  Size: {size:,} bytes")
        return

    # Remove
    if not os.path.exists(fts_db_path):
        print("FTS database does not exist. Nothing to remove.")
        print("The next MCP server start will auto-create and resync the FTS index.")
        return

    size = os.path.getsize(fts_db_path)
    print(f"Removing FTS database: {fts_db_path}")
    print(f"  Size: {size:,} bytes")

    try:
        os.remove(fts_db_path)
        print("FTS database removed successfully.")
        print("The next MCP server start will auto-create and resync the FTS index.")
    except OSError as exc:
        print(f"Error removing FTS database: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
