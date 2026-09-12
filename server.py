#!/usr/bin/env python3
"""
MCP Server for Kilo Code Conversations.

Entry-point that bootstraps the application by importing all layers
(database → services → formatters → tools) and starting the FastMCP server.

## Architecture

Each MCP instance manages **one** primary opencode database (via
``DATABASE_PATH``) and writes to a **shared** FTS index (via
``FTS_DB_PATH``).  A background sync thread periodically copies new
``part`` rows from the primary into the FTS index:

    Primary DB (opencode-local.db)  ──sync──►  FTS DB (shared-fts.db)

Usage:
    SERVER_NAME=prod DATABASE_PATH=/path/to/prod.db python server.py
"""

from __future__ import annotations

# Graceful shutdown: close database connections on exit.
import atexit
import signal
import sys

# Initialise the database (sets up connection pools, mode detection).
from database import init_db

init_db()

# Start the FTS background sync thread (reads primary, writes FTS DB).
# This must happen after init_db() so the db mode is known.
from config import INTERVAL_SYNC
from database import resolve_fts_db_path, is_db_readonly

if not is_db_readonly():
    from syncer import start_fts_syncer

    start_fts_syncer(
        fts_db_path=resolve_fts_db_path(),
        interval=INTERVAL_SYNC,
        is_readonly=False,
    )
else:
    # In read-only mode, just log that FTS is disabled
    from database import logger as db_logger
    db_logger.info("FTS disabled — read-only mode.")

# Import tool definitions (creates FastMCP server object).
from tools import mcp


def _shutdown() -> None:
    """Close all thread-local database connections and stop the syncer."""
    from syncer import stop_fts_syncer
    from database import close_all

    stop_fts_syncer()
    close_all()


def _install_shutdown_handlers() -> None:
    """Register shutdown handlers for clean database connection cleanup."""
    atexit.register(_shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))


_install_shutdown_handlers()

if __name__ == "__main__":
    mcp.run()
