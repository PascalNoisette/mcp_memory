#!/usr/bin/env python3
"""
Configuration for the Kilo Code MCP Memory Server.

Centralizes all tunable constants and environment-based settings.
"""

from __future__ import annotations

# Database – import the shared resolver so all modules use the same
# path (DATABASE_PATH env var or the default).  Test modules can still
# override via ``os.environ.setdefault("DATABASE_PATH", ...)`` before
# this module is imported.
from database import resolve_db_path

DATABASE_PATH: str = resolve_db_path()

# Pagination / search limits
DEFAULT_PAGE_SIZE: int = 10        # default messages per page
DEFAULT_LIST_LIMIT: int = 20       # default sessions/projects per page
DEFAULT_SEARCH_LIMIT: int = 20     # default search results per page
MAX_PAGE_SIZE: int = 100           # hard upper bound for pagination
MAX_SEARCH_LIMIT: int = 100
MAX_KEYWORD_LENGTH: int = 500
