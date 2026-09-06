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

from database import parse_db_path

# Point at the real database (opencode.db) with real sessions and messages.
# Append ``:rw`` so that FTS5 init runs during tests (read-write mode).
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + ":rw",
)


def _clean_db_path() -> str:
    """Return the raw database path without the ``:mode`` suffix."""
    path, _ = parse_db_path(os.environ["DATABASE_PATH"])
    return path


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


@pytest.fixture
def run_async():
    """Helper fixture to run async functions synchronously."""
    return asyncio.run
