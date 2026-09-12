#!/usr/bin/env python3
"""
Configuration for the Kilo Code MCP Memory Server.

Centralizes all tunable constants and environment-based settings.

Environment variables:
    DATABASE_PATH  (required)    Primary opencode DB path.
    SERVER_NAME    (required)    Unique tag for this MCP instance (e.g. 'prod', 'dev').
    FTS_DB_PATH    (optional)   Shared FTS5 index path.
"""

from __future__ import annotations

from database import resolve_db_path, resolve_fts_db_path

DATABASE_PATH: str = resolve_db_path()
FTS_DB_PATH: str = resolve_fts_db_path()

# FTS sync (seconds between background sync cycles)
INTERVAL_SYNC: int = 30

# Pagination / search limits
DEFAULT_PAGE_SIZE: int = 10        # default messages per page
DEFAULT_LIST_LIMIT: int = 20       # default sessions/projects per page
DEFAULT_SEARCH_LIMIT: int = 20     # default search results per page
MAX_PAGE_SIZE: int = 100           # hard upper bound for pagination
MAX_SEARCH_LIMIT: int = 100
MAX_KEYWORD_LENGTH: int = 500
