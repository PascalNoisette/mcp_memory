#!/usr/bin/env python3
"""
pytest configuration and shared fixtures for the test suite.

Usage:
    pytest tests/              # run all tests
    pytest tests/ -v           # verbose
    pytest tests/ -k "test_edge" # run only edge case tests

The fixtures here mirror the helpers in base.py but provide
pytest-compatible dependency injection.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any

import pytest

# Primary database path — always opened read-only (file:…?mode=ro URI).
# The ``:rw`` suffix keeps FTS enabled (``is_db_readonly()`` returns False).
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + ":rw",
)

# Isolated FTS database for tests.
# Resolves to ``{primary_db_path}_test-fts.db`` — never touches the live
# ``opencode_fts.db`` index.  The file is created at the start of the
# session and removed at the end.
os.environ.setdefault(
    "FTS_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + "_test-fts.db",
)

# SERVER_NAME is required in the new architecture
os.environ.setdefault("SERVER_NAME", "memory")


def _clean_db_path() -> str:
    """Return the raw database path without the ``:mode`` suffix."""
    from database import resolve_db_path
    return resolve_db_path()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def db_path() -> str:
    """Path to the opencode database (clean path without mode suffix)."""
    return _clean_db_path()


@pytest.fixture(scope="session")
def directories(db_path: str) -> list[str]:
    """Cached list of all directories (newest first by latest session)."""
    conn = sqlite3.connect(db_path)
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


@pytest.fixture(scope="session")
def project_list_json() -> list[dict[str, Any]]:
    """Fetch the project list via the MCP tool (cached for the session)."""
    from models import ListProjectsInput
    from tools import list_projects

    params = ListProjectsInput(response_format="json")
    output = list_projects(**params.model_dump())
    return json.loads(output)["projects"]


@pytest.fixture(scope="session")
def project_with_many_sessions(directories: list[str]) -> str:
    """Return the directory that has the most sessions."""
    conn = sqlite3.connect(_clean_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    best_dir = directories[0]
    best_count = 0

    for d in directories:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM session WHERE directory = ?",
            (d,),
        )
        cnt = cur.fetchone()["cnt"]
        if cnt > best_count:
            best_count = cnt
            best_dir = d

    conn.close()
    return best_dir


@pytest.fixture
def sample_directory(directories: list[str]) -> str:
    """A single directory for tests that need exactly one."""
    return directories[0]


@pytest.fixture
def sample_session_id(directories: list[str]) -> str:
    """A session ID from the directory with the most sessions."""
    conn = sqlite3.connect(_clean_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM session WHERE directory = ? ORDER BY time_created DESC LIMIT 1",
        (directories[0],),
    )
    row = cur.fetchone()
    conn.close()
    return row["id"] if row else ""


@pytest.fixture
def db_connection(db_path: str):
    """Provide a sqlite3 connection (auto-closed after test)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── FTS cleanup (runs after all tests in the session) ─────────────────────────


def pytest_sessionfinish(session, exitstatus):
    """Remove the test FTS database after the session ends.

    This hook runs after all tests are collected and executed.  The
    FTS database created during ``init_db()`` is deleted so that:

      * Subsequent ``pytest`` runs start fresh (FTS gets recreated).
      * The live ``opencode_fts.db`` is never modified.
      * No manual cleanup is needed.
    """
    fts_path = os.environ.get("FTS_DB_PATH")
    if fts_path:
        import atexit

        parent = os.path.dirname(fts_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass

        def _remove():
            try:
                if os.path.exists(fts_path):
                    os.unlink(fts_path)
                # Also clean up WAL/shm files
                for suffix in ("-wal", "-shm"):
                    wal = fts_path + suffix
                    if os.path.exists(wal):
                        os.unlink(wal)
            except OSError:
                pass

        atexit.register(_remove)
