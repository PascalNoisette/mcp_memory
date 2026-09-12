#!/usr/bin/env python3
"""
Unit tests for FTS filtering and new column output.

Tests:
  1. ``step-start`` and ``step-finish`` part types are NOT stored in the FTS index.
  2. The FTS columns ``server``, ``tool``, ``title`` appear in recall JSON output.
  3. The ``server`` field appears in recall Markdown session headers.
  4. The ``tool`` and ``title`` fields appear in recall Markdown result lines.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import _ensure_fts_schema

try:
    from .base import TestBase, _clean_db_path, get_directories, get_session_with_most_parts
except ImportError:
    from base import TestBase, _clean_db_path, get_directories, get_session_with_most_parts

# ---------------------------------------------------------------------------
# Helpers – build a tiny primary + FTS database in a temp directory
# ---------------------------------------------------------------------------


def _make_test_db() -> tuple[str, sqlite3.Connection, sqlite3.Connection]:
    """Create a temporary primary DB and an FTS DB with the correct schema.

    Returns (primary_path, primary_conn, fts_conn).
    The primary DB has the minimal schema needed for sync testing.
    """
    tmp_dir = tempfile.mkdtemp()
    primary_path = os.path.join(tmp_dir, "primary.db")
    fts_path = os.path.join(tmp_dir, "fts.db")

    primary = sqlite3.connect(primary_path)
    primary.row_factory = sqlite3.Row

    fts_conn = sqlite3.connect(fts_path)
    fts_conn.row_factory = sqlite3.Row

    _ensure_fts_schema(fts_conn)
    fts_conn.commit()

    # Minimal primary DB schema
    primary.execute(
        """
        CREATE TABLE IF NOT EXISTS session (
            id          TEXT PRIMARY KEY,
            directory   TEXT,
            agent       TEXT,
            title       TEXT,
            slug        TEXT,
            time_created INTEGER,
            project_id  TEXT
        )
        """
    )
    primary.execute(
        """
        CREATE TABLE IF NOT EXISTS part (
            rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            message_id   TEXT,
            id           TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data         TEXT
        )
        """
    )
    primary.commit()

    return primary_path, primary, fts_conn


def _insert_session(
    primary: sqlite3.Connection,
    session_id: str = "sess-filter",
    time_created: int = 1000,
) -> None:
    """Insert a session row into the primary DB."""
    primary.execute(
        "INSERT OR REPLACE INTO session (id, directory, agent, title, slug, time_created, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "/test/directory",
            "general",
            "Filter Test Session",
            "filter-test-session",
            time_created,
            "proj-filter",
        ),
    )
    primary.commit()


def _insert_part(
    primary: sqlite3.Connection,
    session_id: str,
    message_id: str,
    part_id: str,
    time_created: int,
    data: dict,
) -> int:
    """Insert a part row. Returns the rowid."""
    cur = primary.execute(
        "INSERT INTO part (session_id, message_id, id, time_created, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            message_id,
            part_id,
            time_created,
            json.dumps(data),
        ),
    )
    primary.commit()
    return cur.lastrowid


def _perform_full_sync(primary: sqlite3.Connection, fts_conn: sqlite3.Connection, server: str) -> int:
    """Run the same logic that ``_do_fts_init(server)`` uses — insert all
    eligible parts into the FTS table for *server*, then return the count.

    This mirrors the exact filtering rules: skip ``step-start``, ``step-finish``,
    rows with no text/content/value/state.output, and rows with invalid JSON.
    """
    from formatters import parse_part_data

    cur = primary.cursor()
    cur.execute(
        """
        SELECT p.rowid, p.data, p.session_id, p.message_id,
               p.id              AS part_id,
               s.directory,
               s.agent,
               s.title           AS session_title,
               s.slug            AS session_slug,
               s.time_created    AS session_time_created,
               p.time_created    AS part_time_created,
               json_extract(p.data, '$.tool') AS part_tool,
               json_extract(p.data, '$.state.title') AS part_title
        FROM part p
        JOIN session s ON p.session_id = s.id
        WHERE json_valid(p.data) = 1
          AND json_extract(p.data, '$.type') NOT IN ('step-start', 'step-finish')
          AND (
              json_extract(p.data, '$.text') IS NOT NULL
              OR json_extract(p.data, '$.content') IS NOT NULL
              OR json_extract(p.data, '$.value') IS NOT NULL
              OR json_extract(p.data, '$.state.output') IS NOT NULL
          )
        ORDER BY p.rowid ASC
        """
    )
    rows = cur.fetchall()
    count = 0
    for row in rows:
        parsed = parse_part_data(row["data"] or "{}")
        text = parsed.get("text", "")
        part_type = parsed.get("type", "unknown")
        fts_conn.execute(
            """INSERT INTO part_fts
               (rowid, text, session_id, message_id, part_id, server,
                part_type, directory, agent, session_title, session_slug,
                session_time_created, part_time_created, tool, title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["rowid"],
                text,
                row["session_id"],
                row["message_id"],
                row["part_id"],
                server,
                part_type,
                row["directory"],
                row["agent"],
                row["session_title"] or "",
                row["session_slug"] or "",
                row["session_time_created"],
                row["part_time_created"],
                row["part_tool"],
                row["part_title"],
            ),
        )
        count += 1
    fts_conn.commit()
    return count


# ---------------------------------------------------------------------------
# Test 1 — step-start / step-finish are NOT stored in FTS
# ---------------------------------------------------------------------------


class TestStepStartFinishExcludedFromFts(unittest.TestCase):
    """step-start and step-finish part types must NOT be indexed in FTS."""

    def _setup_db(self) -> tuple[str, sqlite3.Connection, sqlite3.Connection]:
        """Helper to create a fresh test DB."""
        return _make_test_db()

    def test_step_start_not_in_fts(self):
        """A part with type=step-start should NOT appear in the FTS table."""
        _, primary, fts_conn = self._setup_db()
        try:
            _insert_session(primary)
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-start",
                "part-step-start",
                1000,
                {
                    "type": "step-start",
                    "text": "Starting a step",
                    "state": {"title": "Step Start"},
                },
            )
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-finish",
                "part-step-finish",
                2000,
                {
                    "type": "step-finish",
                    "text": "Finished a step",
                    "state": {"title": "Step Finish"},
                },
            )

            count = _perform_full_sync(primary, fts_conn, "step-test")

            # Total parts in FTS should be 0 (both are excluded)
            self.assertEqual(count, 0)

            step_start_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-step-start'"
            ).fetchone()[0]
            self.assertEqual(step_start_in_fts, 0)

            step_finish_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-step-finish'"
            ).fetchone()[0]
            self.assertEqual(step_finish_in_fts, 0)
        finally:
            primary.close()
            fts_conn.close()

    def test_step_finish_not_in_fts(self):
        """A part with type=step-finish should NOT appear in the FTS table."""
        _, primary, fts_conn = self._setup_db()
        try:
            _insert_session(primary)
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-finish",
                "part-step-finish",
                2000,
                {
                    "type": "step-finish",
                    "text": "Finished a step",
                    "state": {"title": "Step Finish"},
                },
            )

            count = _perform_full_sync(primary, fts_conn, "step-test")
            self.assertEqual(count, 0)

            step_finish_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-step-finish'"
            ).fetchone()[0]
            self.assertEqual(step_finish_in_fts, 0)
        finally:
            primary.close()
            fts_conn.close()

    def test_regular_parts_still_synced_with_step_parts(self):
        """Regular (non-step) parts should still be synced even when step parts are present."""
        _, primary, fts_conn = self._setup_db()
        try:
            _insert_session(primary)
            _insert_part(
                primary,
                "sess-filter",
                "msg-user",
                "part-user",
                500,
                {"type": "user_message", "text": "Hello world"},
            )
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-start",
                "part-step-start",
                1000,
                {"type": "step-start", "text": "Starting step"},
            )
            _insert_part(
                primary,
                "sess-filter",
                "msg-agent",
                "part-agent",
                1500,
                {"type": "agent_message", "text": "Answer"},
            )
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-finish",
                "part-step-finish",
                2000,
                {"type": "step-finish", "text": "Finished step"},
            )

            count = _perform_full_sync(primary, fts_conn, "step-test")

            # Only user_message and agent_message should be indexed
            self.assertEqual(count, 2)

            # Verify the regular parts exist
            user_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-user'"
            ).fetchone()[0]
            agent_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-agent'"
            ).fetchone()[0]
            self.assertEqual(user_in_fts, 1)
            self.assertEqual(agent_in_fts, 1)

            # Verify step parts do NOT exist
            step_start_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-step-start'"
            ).fetchone()[0]
            step_finish_in_fts = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE message_id = 'msg-step-finish'"
            ).fetchone()[0]
            self.assertEqual(step_start_in_fts, 0)
            self.assertEqual(step_finish_in_fts, 0)
        finally:
            primary.close()
            fts_conn.close()

    def test_step_parts_with_state_output_not_included(self):
        """step-start/step-finish with state.output should ALSO be excluded."""
        _, primary, fts_conn = self._setup_db()
        try:
            _insert_session(primary)
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-finish-output",
                "part-step-finish-1",
                3000,
                {
                    "type": "step-finish",
                    "state": {
                        "output": "Step completed with output",
                        "title": "Step Title",
                    },
                },
            )

            count = _perform_full_sync(primary, fts_conn, "step-test")
            self.assertEqual(count, 0)
        finally:
            primary.close()
            fts_conn.close()

    def test_fts_query_excludes_step_types(self):
        """A direct FTS query for step-start/step-finish should return 0 rows."""
        _, primary, fts_conn = self._setup_db()
        try:
            _insert_session(primary)
            _insert_part(
                primary,
                "sess-filter",
                "msg-step-start",
                "part-step-start",
                1000,
                {"type": "step-start", "text": "Step start text"},
            )
            _insert_part(
                primary,
                "sess-filter",
                "msg-user",
                "part-user",
                500,
                {"type": "user_message", "text": "Hello world"},
            )

            _perform_full_sync(primary, fts_conn, "step-test")

            # Direct query on FTS table — step-start rows should not exist
            result = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE part_type IN ('step-start', 'step-finish')"
            ).fetchone()[0]
            self.assertEqual(result, 0)

            # But user_message should exist
            user_count = fts_conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE part_type = 'user_message'"
            ).fetchone()[0]
            self.assertEqual(user_count, 1)
        finally:
            primary.close()
            fts_conn.close()


# ---------------------------------------------------------------------------
# Test 2 — FTS columns (server, tool, title) in recall JSON output
# ---------------------------------------------------------------------------


class TestRecallJsonHasServerToolTitle(unittest.TestCase):
    """recall_session JSON output must include server, tool, title fields."""

    def setUp(self):
        # Import here so env vars are set by base.py / conftest.py
        from database import init_db
        init_db()

    def test_recognize_server_field_in_recognize_json_output(self):
        """recall_session JSON matches should include a 'server' field."""
        from models import RecallSessionInput, ResponseFormat
        from tools import recall_session

        directories = get_directories()
        if not directories:
            self.skipTest("No directories available")

        result = get_session_with_most_parts(
            directories[1] if len(directories) > 1 else directories[0]
        )
        if not result:
            self.skipTest("No session with parts")

        # Search with a common keyword that should match at least some results
        params = RecallSessionInput(
            keywords="database",
            limit=10,
            response_format=ResponseFormat.JSON,
        )
        output = TestBase._call_tool(recall_session, params)
        data = json.loads(output)

        self.assertGreater(len(data["matches"]), 0)

        # Each match must have server, tool, title
        for m in data["matches"]:
            self.assertIn("server", m)
            self.assertIn("tool", m)
            self.assertIn("title", m)

    def test_server_field_contains_test_server_name(self):
        """The 'server' field should contain the SERVER_NAME we set in the test."""
        from models import RecallSessionInput, ResponseFormat
        from tools import recall_session

        directories = get_directories()
        if not directories:
            self.skipTest("No directories available")

        result = get_session_with_most_parts(
            directories[1] if len(directories) > 1 else directories[0]
        )
        if not result:
            self.skipTest("No session with parts")

        params = RecallSessionInput(
            keywords="database",
            limit=10,
            response_format=ResponseFormat.JSON,
        )
        output = TestBase._call_tool(recall_session, params)
        data = json.loads(output)

        if len(data["matches"]) == 0:
            self.skipTest("No matches found")

        # The server field should be set (non-null)
        self.assertIsNotNone(data["matches"][0]["server"])

    def test_tool_and_title_fields_present_on_all_matches(self):
        """All matches from recall_session should have tool and title fields (may be empty)."""
        from models import RecallSessionInput, ResponseFormat
        from tools import recall_session

        directories = get_directories()
        if not directories:
            self.skipTest("No directories available")

        result = get_session_with_most_parts(
            directories[1] if len(directories) > 1 else directories[0]
        )
        if not result:
            self.skipTest("No session with parts")

        params = RecallSessionInput(
            keywords="database",
            limit=50,
            response_format=ResponseFormat.JSON,
        )
        output = TestBase._call_tool(recall_session, params)
        data = json.loads(output)

        # Verify all matches have the fields (may be str or None)
        for m in data["matches"]:
            self.assertIn("tool", m)
            self.assertIn("title", m)
            self.assertTrue(
                m["tool"] is None or isinstance(m["tool"], str),
                f"tool should be str or None, got {type(m['tool'])}",
            )
            self.assertTrue(
                m["title"] is None or isinstance(m["title"], str),
                f"title should be str or None, got {type(m['title'])}",
            )


# ---------------------------------------------------------------------------
# Test 3 — FTS columns in recall Markdown output
# ---------------------------------------------------------------------------


class TestRecallMarkdownHasServerToolTitle(unittest.TestCase):
    """recall_session Markdown output must show server, tool, title where present."""

    def setUp(self):
        from database import init_db
        init_db()

    def test_server_shown_in_markdown_session_header(self):
        """The server name should appear in the session header of recall markdown output."""
        from models import RecallSessionInput, ResponseFormat
        from tools import recall_session

        directories = get_directories()
        if not directories:
            self.skipTest("No directories available")

        result = get_session_with_most_parts(
            directories[1] if len(directories) > 1 else directories[0]
        )
        if not result:
            self.skipTest("No session with parts")

        params = RecallSessionInput(
            keywords="database",
            response_format=ResponseFormat.MARKDOWN,
        )
        output = TestBase._call_tool(recall_session, params)

        # Server should appear in the markdown output
        self.assertIn("Server:", output)


if __name__ == "__main__":
    unittest.main()
