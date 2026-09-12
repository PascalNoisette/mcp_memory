#!/usr/bin/env python3
"""
Shared test base, helpers, and fixtures.

All test files inherit from TestBase or import the free functions here
to avoid copy-pasting the same boilerplate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from typing import Any, Callable, Optional

# ── Environment ────────────────────────────────────────────────────────

# Primary database path — always opened read-only (file:…?mode=ro URI).
# The ``:rw`` suffix keeps FTS enabled (``is_db_readonly()`` returns False).
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + ":rw",
)

# Isolated FTS database for tests — never touches the live
# ``opencode_fts.db`` index.  The file is created at the start of the
# session and removed at the end by ``conftest.py``.
os.environ.setdefault(
    "FTS_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + "_test-fts.db",
)

# SERVER_NAME is required in the new architecture
os.environ.setdefault("SERVER_NAME", "memory")


# ── DB path helper ─────────────────────────────────────────────────────


def _clean_db_path() -> str:
    """Return the raw database path without the ``:mode`` suffix."""
    from database import resolve_db_path
    return resolve_db_path()


# ── Tool runner ────────────────────────────────────────────────────────────────
# The MCP tools are synchronous functions decorated with @tool_error_handler.
# They return plain strings (Markdown or JSON), not coroutines.


def _call_tool(func: Callable, params: Any) -> str:
    """Call a synchronous MCP tool with validated params.

    Unpacks Pydantic model fields as keyword arguments so the tool
    function receives them as individual kwargs matching its signature.
    Computed fields (like ``keywords_list``) are excluded since they
    are derived internally by the tool function, not passed in.
    """
    dump = params.model_dump(exclude={'keywords_list'})
    return func(**dump)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_directories() -> list[str]:
    """Get all directories from the database (newest first by latest session)."""
    conn = sqlite3.connect(_clean_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT s.directory
        FROM session s
        GROUP BY s.directory
        ORDER BY MAX(s.time_created) DESC
    """)
    ids = [r["directory"] for r in cur.fetchall()]
    conn.close()
    return ids


def get_session_count(directory: str) -> int:
    """Return the number of sessions for a directory."""
    conn = sqlite3.connect(_clean_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM session WHERE directory = ?",
        (directory,),
    )
    count = cur.fetchone()["cnt"]
    conn.close()
    return count


def get_session_with_most_parts(directory: str, min_parts: int = 50):
    """Find the session with the most parts for a given directory.

    Returns a sqlite3.Row with ``id`` and ``title``, or ``None`` if none
    has at least *min_parts* parts.
    """
    conn = sqlite3.connect(_clean_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.id, s.title
        FROM session s
        WHERE s.directory = ?
          AND (SELECT COUNT(*) FROM part WHERE part.session_id = s.id) > ?
        ORDER BY (SELECT COUNT(*) FROM part WHERE part.session_id = s.id) DESC
        LIMIT 1
        """,
        (directory, min_parts),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_project_list_as_json() -> list[dict]:
    """Call list_projects and return the parsed JSON data."""
    from models import ListProjectsInput
    from tools import list_projects

    params = ListProjectsInput(response_format="json")
    output = _call_tool(list_projects, params)
    return json.loads(output)


# ── TestBase class ─────────────────────────────────────────────────────────────

class TestBase(unittest.TestCase):
    """Base class providing shared helpers to all test classes."""

    _call_tool = staticmethod(_call_tool)

    def setUp(self) -> None:
        """Ensure database & FTS are initialised before each test."""
        from database import init_db

        init_db()

    @staticmethod
    def get_directories() -> list[str]:
        return get_directories()

    @staticmethod
    def get_session_count(directory: str) -> int:
        return get_session_count(directory)

    @staticmethod
    def get_session_with_most_parts(directory: str, min_parts: int = 50):
        return get_session_with_most_parts(directory, min_parts)

    @staticmethod
    def get_project_list_as_json() -> list[dict]:
        return get_project_list_as_json()
