#!/usr/bin/env python3
"""
MCP Server for Kilo Code Conversations.

Entry-point that bootstraps the application by importing all layers
(database → services → formatters → tools) and starting the FastMCP server.

Usage:
    python server.py
"""

from __future__ import annotations

# Graceful shutdown: close database connections on exit.
import atexit
import signal
import sys

# Initialise the FTS5 full-text search index before importing the tool
# definitions (which create the FastMCP server object).  This prevents
# race conditions where a request arrives before the FTS table is ready
# and avoids the per-connection race that ``_init_fts5()`` had.
from database import init_db

init_db()

from tools import mcp


def _shutdown() -> None:
    """Close all thread-local database connections."""
    from database import _ConnectionPool

    _ConnectionPool.close_all()


def _install_shutdown_handlers() -> None:
    """Register shutdown handlers for clean database connection cleanup."""
    atexit.register(_shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_shutdown(), sys.exit(0)))


_install_shutdown_handlers()

if __name__ == "__main__":
    mcp.run()
