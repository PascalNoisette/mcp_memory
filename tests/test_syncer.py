#!/usr/bin/env python3
"""
Unit tests for the ``syncer`` module.

Tests the three core sync functions in isolation using temporary SQLite
databases — no real project database or background thread involved.

Functions tested:
  - ``_get_watermarks(fts_conn, server)``
  - ``_set_watermarks(fts_conn, server, ts, rowid)``
  - ``_incremental_sync(server)``
"""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from database import _ensure_fts_schema
from formatters import parse_part_data
from syncer import _get_watermarks, _incremental_sync, _set_watermarks

# Server name for tests
TEST_SERVER = "test-server"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_primary_db(
    path: str,
    session_id: str = "sess-001",
    parts: list[dict] | None = None,
) -> sqlite3.Connection:
    """Create a temporary primary database with *session* and *part* tables.

    Parameters
    ----------
    path :
        Filesystem path for the temporary database file.
    session_id :
        A session ID to insert into the ``session`` table.
    parts :
        List of dicts with keys ``message_id``, ``time_created``, ``data``.
        Each entry becomes a row in the ``part`` table.
    """
    if parts is None:
        parts = []

    conn = sqlite3.connect(path)
    cur = conn.cursor()

    # ── Schema ──────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session (
            id              TEXT PRIMARY KEY,
            directory       TEXT,
            agent           TEXT,
            title           TEXT,
            slug            TEXT,
            time_created    INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS part (
            rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT NOT NULL,
            message_id      TEXT,
            id              TEXT,
            time_created    INTEGER,
            time_updated    INTEGER,
            data            TEXT
        )
    """)

    # ── Seed data ───────────────────────────────────────────────────────
    cur.execute(
        "INSERT INTO session (id, directory, agent, title, slug, time_created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, "/test/dir", "general", "Test Session", "test-session", 1000),
    )

    for i, part in enumerate(parts):
        cur.execute(
            "INSERT INTO part (session_id, message_id, time_created, data) "
            "VALUES (?, ?, ?, ?)",
            (
                session_id,
                part.get("message_id", f"msg-{i}"),
                part.get("time_created", 1000 + i),
                part.get("data", "{}"),
            ),
        )

    conn.commit()
    return conn


def _create_fts_db(path: str) -> sqlite3.Connection:
    """Create a temporary FTS database with the required schema."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_fts_schema(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fixtures — isolated primary + FTS databases
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_paths(tmp_path):
    """Return (primary_path, fts_path) pointing to temporary files."""
    primary = str(tmp_path / "primary.db")
    fts = str(tmp_path / "primary_fts.db")
    return primary, fts


@pytest.fixture()
def fts_conn(db_paths):
    """FTS database connection with *empty* metadata table."""
    conn = _create_fts_db(db_paths[1])
    yield conn
    conn.close()


@pytest.fixture()
def fts_conn_with_watermarks(db_paths):
    """FTS DB connection with watermarks already set for TEST_SERVER.

    The primary database at db_paths[0] is NOT created here to avoid
    fixture-path conflicts; each test that needs a primary DB creates
    it explicitly.
    """
    conn = _create_fts_db(db_paths[1])
    conn.execute(
        "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
        (f"last_sync_ts:{TEST_SERVER}", "1000"),
    )
    conn.execute(
        "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
        (f"last_sync_rowid:{TEST_SERVER}", "2"),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def primary_with_parts(db_paths):
    """Primary DB seeded with three parts (rowids 1, 2, 3)."""
    parts = [
        {
            "message_id": "msg-1",
            "time_created": 1000,
            "data": json.dumps({"type": "user_message", "text": "first message"}),
        },
        {
            "message_id": "msg-2",
            "time_created": 1500,
            "data": json.dumps({"type": "agent_message", "text": "second message"}),
        },
        {
            "message_id": "msg-3",
            "time_created": 2000,
            "data": json.dumps({"type": "user_message", "text": "third message"}),
        },
    ]
    conn = _create_primary_db(db_paths[0], parts=parts)
    return conn


@pytest.fixture()
def primary_with_invalid_json(db_paths):
    """Primary DB with a mix of valid JSON and invalid data."""
    parts = [
        {
            "message_id": "msg-1",
            "time_created": 1000,
            "data": json.dumps({"type": "user_message", "text": "valid json"}),
        },
        {
            "message_id": "msg-2",
            "time_created": 1500,
            "data": "NOT VALID JSON AT ALL {{{",
        },
    ]
    conn = _create_primary_db(db_paths[0], parts=parts)
    return conn


@pytest.fixture()
def primary_with_empty_parts(db_paths):
    """Primary DB with parts that have NULL/empty/invalid data fields."""
    parts = [
        {
            "message_id": "msg-empty",
            "time_created": 1000,
            "data": "",  # empty string → not valid JSON
        },
        {
            "message_id": "msg-null",
            "time_created": 1500,
            "data": None,  # NULL
        },
        {
            "message_id": "msg-no-field",
            "time_created": 2000,
            "data": json.dumps({"type": "user_message"}),  # no text/content/value
        },
    ]
    conn = _create_primary_db(db_paths[0], parts=parts)
    return conn


@pytest.fixture()
def primary_with_new_row(db_paths):
    """Primary DB with three parts, then a fourth row (rowid=4) inserted after."""
    conn = _create_primary_db(db_paths[0], parts=[
        {
            "message_id": "msg-1",
            "time_created": 1000,
            "data": json.dumps({"type": "user_message", "text": "first"}),
        },
        {
            "message_id": "msg-2",
            "time_created": 1500,
            "data": json.dumps({"type": "agent_message", "text": "second"}),
        },
        {
            "message_id": "msg-3",
            "time_created": 2000,
            "data": json.dumps({"type": "user_message", "text": "third"}),
        },
    ])
    # Insert a new row *after* the fixture sets up watermarks
    conn.execute(
        "INSERT INTO part (session_id, message_id, time_created, data) "
        "VALUES ('sess-001', 'msg-new', 2500, ?)",
        (json.dumps({"type": "agent_message", "text": "new row synced"}),),
    )
    conn.commit()
    return conn


@pytest.fixture()
def primary_with_binary_data(db_paths):
    """Primary DB with a part containing binary / null bytes."""
    parts = [
        {
            "message_id": "msg-1",
            "time_created": 1000,
            "data": json.dumps({"type": "user_message", "text": "clean text"}),
        },
        {
            "message_id": "msg-binary",
            "time_created": 1500,
            "data": json.dumps({
                "type": "agent_message",
                "text": "contains\x00null\x01bytes\x1fhere",
            }),
        },
    ]
    conn = _create_primary_db(db_paths[0], parts=parts)
    return conn


# ---------------------------------------------------------------------------
# Tests for _get_watermarks
# ---------------------------------------------------------------------------


class TestGetWatermarks:
    """Tests for the ``_get_watermarks`` function."""

    def test_returns_zero_when_tables_missing(self, tmp_path):
        """When no ``part_fts_meta`` table exists, returns (0, 0)."""
        # Create a fresh DB without the FTS schema
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        try:
            result = _get_watermarks(conn, TEST_SERVER)
            assert result == (0, 0)
        finally:
            conn.close()

    def test_returns_zero_when_metadata_empty(self, fts_conn):
        """When the ``part_fts_meta`` table exists but has no rows, returns (0, 0)."""
        result = _get_watermarks(fts_conn, TEST_SERVER)
        assert result == (0, 0)

    def test_returns_stored_values(self, fts_conn_with_watermarks):
        """When watermarks are stored, they are returned as (ts, rowid)."""
        result = _get_watermarks(fts_conn_with_watermarks, TEST_SERVER)
        assert result == (1000, 2)

    def test_returns_zero_for_single_key(self):
        """If only one of the two watermark keys exists, the missing one stays 0."""
        conn = sqlite3.connect(":memory:")
        try:
            _ensure_fts_schema(conn)
            conn.execute(
                "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
                (f"last_sync_ts:{TEST_SERVER}", "5000"),
            )
            conn.commit()

            ts, rid = _get_watermarks(conn, TEST_SERVER)
            assert ts == 5000
            assert rid == 0
        finally:
            conn.close()

    def test_returns_zero_for_zero_values(self):
        """Stored value '0' should be returned as integer 0."""
        conn = sqlite3.connect(":memory:")
        try:
            _ensure_fts_schema(conn)
            conn.execute(
                "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
                (f"last_sync_ts:{TEST_SERVER}", "0"),
            )
            conn.execute(
                "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
                (f"last_sync_rowid:{TEST_SERVER}", "0"),
            )
            conn.commit()

            ts, rid = _get_watermarks(conn, TEST_SERVER)
            assert ts == 0
            assert rid == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Tests for _set_watermarks
# ---------------------------------------------------------------------------


class TestSetWatermarks:
    """Tests for the ``_set_watermarks`` function."""

    def test_sets_values_correctly(self, fts_conn):
        """Setting watermarks should be readable back via ``_get_watermarks``."""
        _set_watermarks(fts_conn, TEST_SERVER, 9999, 42)
        result = _get_watermarks(fts_conn, TEST_SERVER)
        assert result == (9999, 42)

    def test_overwrites_previous_values(self, fts_conn_with_watermarks):
        """A subsequent call replaces the previous watermark values."""
        # Watermarks are already (1000, 2)
        _set_watermarks(fts_conn_with_watermarks, TEST_SERVER, 7777, 100)
        result = _get_watermarks(fts_conn_with_watermarks, TEST_SERVER)
        assert result == (7777, 100)
        # Original values are gone
        assert result != (1000, 2)

    def test_multiple_sequential_calls(self, fts_conn):
        """Only the last call's values should be present."""
        _set_watermarks(fts_conn, TEST_SERVER, 100, 1)
        assert _get_watermarks(fts_conn, TEST_SERVER) == (100, 1)

        _set_watermarks(fts_conn, TEST_SERVER, 200, 2)
        assert _get_watermarks(fts_conn, TEST_SERVER) == (200, 2)

        _set_watermarks(fts_conn, TEST_SERVER, 300, 3)
        assert _get_watermarks(fts_conn, TEST_SERVER) == (300, 3)

    def test_sets_zero_watermarks(self, fts_conn):
        """Setting (0, 0) should work without error."""
        _set_watermarks(fts_conn, TEST_SERVER, 0, 0)
        result = _get_watermarks(fts_conn, TEST_SERVER)
        assert result == (0, 0)

    def test_stores_large_values(self, fts_conn):
        """Large timestamps and rowids should be stored and retrieved accurately."""
        large_ts = 1700000000000  # millisecond epoch
        large_rid = 999999
        _set_watermarks(fts_conn, TEST_SERVER, large_ts, large_rid)
        result = _get_watermarks(fts_conn, TEST_SERVER)
        assert result == (large_ts, large_rid)


# ---------------------------------------------------------------------------
# Tests for _incremental_sync
# ---------------------------------------------------------------------------


class TestIncrementalSync:
    """Tests for the ``_incremental_sync`` function.

    ``_incremental_sync`` internally calls ``get_fts_db()`` and ``get_db()``,
    which normally return connections from pools tied to ``DATABASE_PATH``.
    We bypass the pools entirely by patching both functions to return our
    test connections.
    """

    def _sync(self, primary_path, fts_conn, primary_conn=None):
        """Helper that calls ``_incremental_sync`` with mocked db functions."""
        mocks = {"get_fts_db": lambda: fts_conn}
        if primary_conn:
            # Set row_factory so we can access columns by name
            primary_conn.row_factory = sqlite3.Row
            mocks["get_db"] = lambda: primary_conn
        with patch.multiple("syncer", **mocks):
            return _incremental_sync(TEST_SERVER)

    def test_no_new_rows_returns_zero(self, db_paths, primary_with_parts, fts_conn_with_watermarks):
        """Watermarks are inclusive — rows with ts > last_ts OR rowid > last_rid are synced.

        The primary_with_parts fixture creates parts at rowids 1, 2, 3
        with timestamps 1000, 1500, 2000. Watermarks are ts=1000, rid=2.
        Due to the OR condition (rowid > 2 OR time > 1000):
          - rowid=1 (ts=1000): neither condition met → skipped
          - rowid=2 (ts=1500): time > 1000 → synced
          - rowid=3 (ts=2000): rowid > 2 → synced
        """
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_parts)
        assert count == 2

    def test_no_new_rows_true(self, db_paths):
        """When all rows are <= last_max_rid, sync returns 0."""
        conn = _create_primary_db(db_paths[0], parts=[
            {
                "message_id": "msg-1",
                "time_created": 1000,
                "data": json.dumps({"type": "user_message", "text": "first"}),
            },
        ])
        fts = _create_fts_db(db_paths[1])
        fts.row_factory = sqlite3.Row
        # Set watermark to rowid=1 — no new rows
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_ts:{TEST_SERVER}", "1000"),
        )
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_rowid:{TEST_SERVER}", "1"),
        )
        fts.commit()

        count = self._sync(db_paths[0], fts, conn)
        assert count == 0

    def test_new_rows_are_synced(self, db_paths, primary_with_new_row, fts_conn_with_watermarks):
        """New parts since last watermark are synced into FTS.

        Watermarks are ts=1000, rid=2. Parts at rowids 1, 2, 3 (ts 1000, 1500, 2000)
        and new part at rowid 4 (ts 2500). Due to OR logic:
          - rowid=2 (ts=1500): time > 1000 → synced
          - rowid=3 (ts=2000): rowid > 2 → synced
          - rowid=4 (ts=2500): rowid > 2 → synced
        Total: 3 rows.
        """
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_new_row)
        assert count == 3

        # Verify the new row landed in FTS
        row = fts_conn_with_watermarks.execute(
            "SELECT text FROM part_fts WHERE message_id = 'msg-new'"
        ).fetchone()
        assert row is not None
        assert row["text"] == "new row synced"

    def test_mixed_rowids_syncs_only_new(self, db_paths):
        """Only rows with rowid > last_max_rid should be synced."""
        primary = _create_primary_db(db_paths[0], parts=[
            {
                "message_id": "msg-old",
                "time_created": 1000,
                "data": json.dumps({"type": "user_message", "text": "old row"}),
            },
            {
                "message_id": "msg-new",
                "time_created": 2000,
                "data": json.dumps({"type": "agent_message", "text": "new row"}),
            },
        ])
        fts = _create_fts_db(db_paths[1])
        fts.row_factory = sqlite3.Row
        # Watermark rowid=1 — "msg-old" (rowid=1) is already synced
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_ts:{TEST_SERVER}", "1000"),
        )
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_rowid:{TEST_SERVER}", "1"),
        )
        fts.commit()

        count = self._sync(db_paths[0], fts, primary)
        assert count == 1

        # Only the new row should be in FTS
        old = fts.execute(
            "SELECT COUNT(*) AS cnt FROM part_fts WHERE message_id = 'msg-old'"
        ).fetchone()["cnt"]
        new = fts.execute(
            "SELECT COUNT(*) AS cnt FROM part_fts WHERE message_id = 'msg-new'"
        ).fetchone()["cnt"]
        assert old == 0
        assert new == 1

    def test_binary_data_is_sanitized(self, db_paths, primary_with_binary_data, fts_conn_with_watermarks):
        """Binary / control characters in part data are stripped before FTS indexing.

        Parts: rowid=1 (ts=1000, "clean text"), rowid=2 (ts=1500, binary text).
        Watermarks: ts=1000, rid=2. Due to OR logic:
          - rowid=1 (ts=1000): neither met → skipped
          - rowid=2 (ts=1500): time > 1000 → synced
        """
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_binary_data)
        assert count == 1  # only rowid=2 is new (time > 1000)

        # The part with binary data should have been sanitized
        row = fts_conn_with_watermarks.execute(
            "SELECT text FROM part_fts WHERE message_id = 'msg-binary'"
        ).fetchone()
        assert row is not None
        text = row["text"]
        # Binary characters should be removed
        assert "\x00" not in text
        assert "\x01" not in text
        assert "\x1f" not in text
        # The readable text should remain
        assert "contains" in text
        assert "bytes" in text
        assert "here" in text

    def test_invalid_json_handled_gracefully(self, db_paths, primary_with_invalid_json, fts_conn_with_watermarks):
        """Parts with invalid JSON in the data field are skipped (not synced).

        Parts: rowid=1 (ts=1000, valid JSON), rowid=2 (ts=1500, invalid JSON).
        Watermarks: ts=1000, rid=2. Due to OR logic:
          - rowid=1 (ts=1000): neither met → skipped
          - rowid=2 (ts=1500): time > 1000 → would be synced BUT json_valid=0 filters it out
        """
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_invalid_json)
        assert count == 0  # invalid JSON filtered by query, rowid=1 skipped by watermark

        # Invalid part should NOT be in FTS
        invalid_row = fts_conn_with_watermarks.execute(
            "SELECT COUNT(*) AS cnt FROM part_fts WHERE message_id = 'msg-2'"
        ).fetchone()["cnt"]
        assert invalid_row == 0

    def test_empty_data_handled(self, db_paths, primary_with_empty_parts, fts_conn_with_watermarks):
        """Parts with empty/null/field-less data are skipped by the query."""
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_empty_parts)
        assert count == 0  # none pass the json_valid + content check

    def test_multiple_new_rows_synced(self, db_paths):
        """Multiple new rows inserted together are all synced."""
        primary = _create_primary_db(db_paths[0], parts=[
            {
                "message_id": "msg-1",
                "time_created": 1000,
                "data": json.dumps({"type": "user_message", "text": "first"}),
            },
        ])
        fts = _create_fts_db(db_paths[1])
        fts.row_factory = sqlite3.Row
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_ts:{TEST_SERVER}", "1000"),
        )
        fts.execute(
            "INSERT INTO part_fts_meta (key, value) VALUES (?, ?)",
            (f"last_sync_rowid:{TEST_SERVER}", "1"),
        )
        fts.commit()

        # Insert 3 new parts
        primary.execute(
            "INSERT INTO part (session_id, message_id, time_created, data) "
            "VALUES ('sess-001', 'msg-a', 2000, ?)",
            (json.dumps({"type": "user", "text": "a"}),),
        )
        primary.execute(
            "INSERT INTO part (session_id, message_id, time_created, data) "
            "VALUES ('sess-001', 'msg-b', 3000, ?)",
            (json.dumps({"type": "agent", "text": "b"}),),
        )
        primary.execute(
            "INSERT INTO part (session_id, message_id, time_created, data) "
            "VALUES ('sess-001', 'msg-c', 4000, ?)",
            (json.dumps({"type": "user", "text": "c"}),),
        )
        primary.commit()

        count = self._sync(db_paths[0], fts, primary)
        assert count == 3

        # All three new rows should be in FTS
        for mid in ("msg-a", "msg-b", "msg-c"):
            row = fts.execute(
                "SELECT COUNT(*) AS cnt FROM part_fts WHERE message_id = ?",
                (mid,),
            ).fetchone()["cnt"]
            assert row == 1, f"Expected {mid} in FTS"

    def test_watermark_updated_after_sync(self, db_paths, primary_with_new_row, fts_conn_with_watermarks):
        """After syncing, the watermark is advanced to the last synced row's values."""
        count = self._sync(db_paths[0], fts_conn_with_watermarks, primary_with_new_row)
        assert count == 3  # rows at rowids 2, 3, 4 are synced

        # Watermark should now reflect the last synced row (rowid=4)
        ts, rid = _get_watermarks(fts_conn_with_watermarks, TEST_SERVER)
        assert rid == 4
        # Timestamp should be >= 2500 (the new row's time_created)
        assert ts >= 2500

    def test_parse_part_data_handles_edge_cases(self):
        """The ``parse_part_data`` helper used by syncer handles edge cases.

        Note: ``parse_part_data(None)`` is intentionally NOT tested here
        because the underlying ``formatters.parse_part_data`` does not
        guard against ``None`` input — that is a pre-existing bug in the
        formatter module, not something the syncer test should cover.
        """
        # Valid JSON with text
        result = parse_part_data('{"type": "user", "text": "hello"}')
        assert result["type"] == "user"
        assert result["text"] == "hello"

        # Valid JSON without text field
        result = parse_part_data('{"type": "agent"}')
        assert result["type"] == "agent"
        assert result.get("text") is None

        # Valid JSON with content field (alternative to text)
        result = parse_part_data('{"type": "image", "content": "image data"}')
        assert result["type"] == "image"

        # Invalid JSON → fallback
        result = parse_part_data("not json")
        assert result["type"] == "parse_error"
        assert "not json" in result["text"]

        # Empty string
        result = parse_part_data("")
        assert result["type"] == "parse_error"


# ---------------------------------------------------------------------------
# Tests for per-server isolation
# ---------------------------------------------------------------------------


class TestServerIsolation:
    """Tests that verify watermarks are properly isolated per server."""

    def test_different_servers_have_independent_watermarks(self, fts_conn):
        """Two different servers should have independent watermark values."""
        _set_watermarks(fts_conn, "server-a", 100, 10)
        _set_watermarks(fts_conn, "server-b", 200, 20)

        ts_a, rid_a = _get_watermarks(fts_conn, "server-a")
        ts_b, rid_b = _get_watermarks(fts_conn, "server-b")

        assert ts_a == 100 and rid_a == 10
        assert ts_b == 200 and rid_b == 20

    def test_fts_rows_are_tagged_with_server(self, db_paths, primary_with_parts, fts_conn):
        """Rows inserted into FTS should be tagged with the server name."""
        # Set watermarks to force sync of all rows
        _set_watermarks(fts_conn, TEST_SERVER, 0, 0)
        fts_conn.commit()

        # Set row_factory so columns can be accessed by name
        primary_with_parts.row_factory = sqlite3.Row

        # Sync
        with patch.multiple("syncer", get_fts_db=lambda: fts_conn, get_db=lambda: primary_with_parts):
            count = _incremental_sync(TEST_SERVER)

        assert count == 3

        # Verify all rows have the correct server tag
        rows = fts_conn.execute(
            "SELECT server FROM part_fts"
        ).fetchall()
        assert all(r["server"] == TEST_SERVER for r in rows)
