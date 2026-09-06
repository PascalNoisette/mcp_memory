#!/usr/bin/env python3
"""
E2E tests for edge cases and error handling.

Tests:
  - Invalid session/directory IDs are handled gracefully.
  - Boundary values for pagination parameters.
  - Empty result formatting (markdown and JSON).
  - Unicode and special characters in text.
  - Large page sizes.
"""

import json
import os
import sqlite3
import unittest

os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))

from models import ListProjectsInput, ListSessionsInput, ReadMessagesInput, RecallSessionInput, ResponseFormat
from tools import list_projects, list_sessions, read_messages, recall_session

from config import MAX_PAGE_SIZE, MAX_SEARCH_LIMIT
from constants import TEXT_TRUNCATION_LIMIT

try:
    from .base import TestBase, _clean_db_path
except ImportError:
    from base import TestBase, _clean_db_path


class TestEdgeCaseInvalidIDs(TestBase):
    """Behavior with non-existent IDs."""

    def test_nonexistent_directory_sessions(self):
        """Requesting sessions for a directory that doesn't exist returns empty."""
        params = ListSessionsInput(
            directory="/nonexistent/path/that/does/not/exist",
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(list_sessions, params)
        data = json.loads(output)
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["sessions"]), 0)

    def test_nonexistent_session_messages(self):
        """Requesting messages for a session that doesn't exist returns empty."""
        params = ReadMessagesInput(
            session_id="0000000000000000000000000000000000000000",
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(read_messages, params)
        data = json.loads(output)
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["messages"]), 0)
        self.assertFalse(data["has_more"])

    def test_nonexistent_session_messages_markdown(self):
        """Markdown output for a non-existent session."""
        params = ReadMessagesInput(
            session_id="0000000000000000000000000000000000000000",
            response_format=ResponseFormat.MARKDOWN,
        )
        output = self._call_tool(read_messages, params)
        self.assertIn("No messages found", output)


class TestEdgeCasePaginationBoundaries(TestBase):
    """Boundary values for pagination parameters."""

    def setUp(self):
        super().setUp()
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for pagination tests")
        result = self.get_session_with_most_parts(directories[1])
        self.test_session_id = result["id"] if result else ""

    def test_max_page_size(self):
        """MAX_PAGE_SIZE (100) should work without error."""
        directories = self.get_directories()
        # Use a directory with many sessions
        params = ListSessionsInput(
            directory=directories[1],
            limit=MAX_PAGE_SIZE,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), data["total"])

    def test_max_search_limit(self):
        """MAX_SEARCH_LIMIT (100) should work without error."""
        params = RecallSessionInput(
            keywords="code",
            limit=MAX_SEARCH_LIMIT,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertLessEqual(len(data["matches"]), 100)

    def test_zero_limit(self):
        """Zero limit should raise a validation error."""
        with self.assertRaises(Exception):
            ListSessionsInput(
                directory=self.get_directories()[1],
                limit=0,
                response_format=ResponseFormat.JSON,
            )

    def test_zero_page_size(self):
        """Zero page_size should raise a validation error."""
        with self.assertRaises(Exception):
            ReadMessagesInput(
                session_id=self.test_session_id,
                page_size=0,
                response_format=ResponseFormat.JSON,
            )

    def test_negative_offset(self):
        """Negative offset should raise a validation error."""
        with self.assertRaises(Exception):
            ListSessionsInput(
                directory=self.get_directories()[1],
                limit=5,
                offset=-1,
                response_format=ResponseFormat.JSON,
            )

    def test_large_offset_returns_empty(self):
        params = ListSessionsInput(
            directory=self.get_directories()[1],
            limit=10,
            offset=999999999,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), 0)


class TestEdgeCaseEmptySearch(TestBase):
    """Edge cases for search/recall with unusual inputs."""

    def test_empty_keywords_string(self):
        """Empty keyword string should raise validation error."""
        with self.assertRaises(Exception):
            self._call_tool(recall_session, RecallSessionInput(
                keywords="",
                response_format=ResponseFormat.JSON,
            ))

    def test_whitespace_only_keywords(self):
        """Whitespace-only keywords should be treated as empty."""
        # After whitespace stripping, "" fails min_length=1 validation.
        with self.assertRaises(Exception):
            RecallSessionInput(
                keywords="   ",
                response_format=ResponseFormat.JSON,
            )

    def test_single_character_keyword(self):
        """A single character keyword should work."""
        params = RecallSessionInput(
            keywords="x",
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(recall_session, params)
        data = json.loads(output)
        # May return 0 or more results, shouldn't crash
        self.assertIsInstance(data["total_matches"], int)


class TestEdgeCaseUnicodeText(TestBase):
    """Handling of unicode and special characters."""

    def setUp(self):
        super().setUp()
        # Find a session with unicode/French content dynamically
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.title FROM session s
            WHERE (s.title LIKE '%La%' OR s.title LIKE '%base de données%'
                   OR s.title LIKE '%struct%' OR s.title LIKE '%opencode%')
              AND (SELECT COUNT(*) FROM part WHERE part.session_id = s.id) > 0
            ORDER BY (SELECT COUNT(*) FROM part WHERE part.session_id = s.id) DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        if row:
            self.unicode_session_id = row["id"]
        else:
            # Fallback: use any session with parts from directories[1]
            directories = self.get_directories()
            if len(directories) < 2:
                self.skipTest("Need at least 2 directories for unicode test")
            result = self.get_session_with_most_parts(directories[1])
            self.unicode_session_id = result["id"] if result else ""

    def test_unicode_in_session_title(self):
        """Sessions with unicode titles should be returned correctly."""
        params = ListSessionsInput(
            directory=self.get_directories()[1],
            limit=10,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        for s in data["sessions"]:
            self.assertIsInstance(s["title"], str)

    def test_unicode_in_message_text(self):
        """Messages with unicode text should be returned correctly."""
        params = ReadMessagesInput(
            session_id=self.unicode_session_id,
            page_size=3,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(read_messages, params)
        data = json.loads(output)
        self.assertGreater(len(data["messages"]), 0)


class TestEdgeCaseLargeMessages(TestBase):
    """Handling of large message content."""

    def setUp(self):
        super().setUp()
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for large messages test")
        result = self.get_session_with_most_parts(directories[1])
        self.large_session_id = result["id"] if result else ""

    def test_large_message_text(self):
        """A message with very long text should be truncated."""
        params = ReadMessagesInput(
            session_id=self.large_session_id,
            page_size=5,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        for m in data["messages"]:
            self.assertLessEqual(len(m["text"]), TEXT_TRUNCATION_LIMIT)

    def test_full_text_length_is_accurate(self):
        """full_text_length should reflect the actual text length before truncation."""
        params = ReadMessagesInput(
            session_id=self.large_session_id,
            page_size=5,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        for m in data["messages"]:
            # full_text_length should be >= len(text) (text may be truncated)
            self.assertGreaterEqual(m["full_text_length"], len(m["text"]))


class TestEdgeCaseAllFormats(TestBase):
    """Each tool should support both markdown and JSON formats."""

    def setUp(self):
        super().setUp()
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for formats test")
        result = self.get_session_with_most_parts(directories[1])
        self.format_session_id = result["id"] if result else ""

    def test_projects_json(self):
        params = ListProjectsInput(response_format=ResponseFormat.JSON)
        data = json.loads(self._call_tool(list_projects, params))
        self.assertIn("projects", data)

    def test_projects_markdown(self):
        params = ListProjectsInput(response_format=ResponseFormat.MARKDOWN)
        output = self._call_tool(list_projects, params)
        self.assertIsInstance(output, str)

    def test_sessions_json(self):
        params = ListSessionsInput(
            directory=self.get_directories()[1],
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertIn("sessions", data)

    def test_sessions_markdown(self):
        params = ListSessionsInput(
            directory=self.get_directories()[1],
            response_format=ResponseFormat.MARKDOWN,
        )
        output = self._call_tool(list_sessions, params)
        self.assertIsInstance(output, str)

    def test_messages_json(self):
        params = ReadMessagesInput(
            session_id=self.format_session_id,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        self.assertIn("messages", data)

    def test_messages_markdown(self):
        params = ReadMessagesInput(
            session_id=self.format_session_id,
            response_format=ResponseFormat.MARKDOWN,
        )
        output = self._call_tool(read_messages, params)
        self.assertIsInstance(output, str)

    def test_recall_json(self):
        params = RecallSessionInput(
            keywords="code",
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertIn("matches", data)

    def test_recall_markdown(self):
        params = RecallSessionInput(
            keywords="code",
            response_format=ResponseFormat.MARKDOWN,
        )
        output = self._call_tool(recall_session, params)
        self.assertIsInstance(output, str)


class TestEdgeCaseConsistency(TestBase):
    """Cross-tool consistency checks."""

    def test_directory_count_matches_across_calls(self):
        """Calling list_projects twice should return the same count."""
        data1 = json.loads(self._call_tool(list_projects, ListProjectsInput(
            response_format=ResponseFormat.JSON,
        )))["projects"]
        data2 = json.loads(self._call_tool(list_projects, ListProjectsInput(
            response_format=ResponseFormat.JSON,
        )))["projects"]
        self.assertEqual(len(data1), len(data2))

    def test_session_count_consistent_with_list(self):
        """Total from list_sessions should match actual DB count."""
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for consistency test")
        directory = directories[1]
        sessions_data = json.loads(self._call_tool(list_sessions, ListSessionsInput(
            directory=directory, response_format=ResponseFormat.JSON,
        )))
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM session WHERE directory = ?",
            (directory,),
        )
        db_count = cur.fetchone()["cnt"]
        conn.close()
        self.assertEqual(sessions_data["total"], db_count)


# ---------------------------------------------------------------------------
# Binary-character sanitisation tests
# ---------------------------------------------------------------------------


class TestEdgeCaseBinarySanitization(TestBase):
    """Binary characters in part.data must not leak into MCP output."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Find a real session to attach corrupted data to
        cur.execute("""
            SELECT s.id, s.directory FROM session s
            WHERE (SELECT COUNT(*) FROM part p WHERE p.session_id = s.id) > 0
            LIMIT 1
        """)
        row = cur.fetchone()
        self.binary_session_id = row["id"] if row else ""
        self.binary_directory = row["directory"] if row else ""
        conn.close()

    def test_binary_chars_stripped_from_messages(self):
        """Insert a part with null bytes and verify they are removed from output."""
        if not self.binary_session_id:
            self.skipTest("No session available for binary injection test")

        conn = sqlite3.connect(_clean_db_path())
        cur = conn.cursor()
        # Insert a part with binary characters in the text field
        corrupt_json = json.dumps({
            "type": "user_message",
            "text": "Hello\x00World\x01Foo\x1fBar",
        })
        cur.execute(
            "INSERT INTO part (session_id, message_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.binary_session_id, "test-binary-001", int(1700000000000), int(1700000000000), corrupt_json),
        )
        conn.commit()
        conn.close()

        # Call read_messages and verify output is clean
        params = ReadMessagesInput(
            session_id=self.binary_session_id,
            page_size=10,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(read_messages, params)
        data = json.loads(output)

        # The output must be valid JSON — if binary chars leaked, json.loads
        # would have raised an error earlier.
        for msg in data["messages"]:
            text = msg["text"]
            # No null bytes, no C0/C1 control characters (except \n, \t, \r)
            self.assertNotIn("\x00", text)
            self.assertNotIn("\x01", text)
            self.assertNotIn("\x1f", text)

        # Clean up injected row
        conn = sqlite3.connect(_clean_db_path())
        conn.execute(
            "DELETE FROM part WHERE message_id = 'test-binary-001'",
        )
        conn.commit()
        conn.close()

    def test_binary_chars_stripped_from_recall(self):
        """Recall results must also be free of binary characters."""
        if not self.binary_session_id:
            self.skipTest("No session available for binary injection test")

        conn = sqlite3.connect(_clean_db_path())
        cur = conn.cursor()
        corrupt_json = json.dumps({
            "type": "agent_message",
            "text": "Found\x00the\x01keyword\x1fcode",
        })
        cur.execute(
            "INSERT INTO part (session_id, message_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.binary_session_id, "test-binary-002", int(1700000000000), int(1700000000000), corrupt_json),
        )
        conn.commit()
        conn.close()

        params = RecallSessionInput(
            keywords="code",
            limit=10,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(recall_session, params)
        data = json.loads(output)

        for m in data["matches"]:
            snippet = m.get("snippet", "")
            self.assertNotIn("\x00", snippet)
            self.assertNotIn("\x01", snippet)
            self.assertNotIn("\x1f", snippet)

        # Clean up
        conn = sqlite3.connect(_clean_db_path())
        conn.execute(
            "DELETE FROM part WHERE message_id = 'test-binary-002'",
        )
        conn.commit()
        conn.close()

    def test_malformed_data_still_sanitized(self):
        """If part.data is not valid JSON, sanitization still happens."""
        if not self.binary_session_id:
            self.skipTest("No session available for binary injection test")

        conn = sqlite3.connect(_clean_db_path())
        cur = conn.cursor()
        # Non-JSON blob with binary bytes
        cur.execute(
            "INSERT INTO part (session_id, message_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.binary_session_id, "test-binary-003", int(1700000000000), int(1700000000000),
             "GARBLED\x00DATA\x07\x08"),
        )
        conn.commit()
        conn.close()

        params = ReadMessagesInput(
            session_id=self.binary_session_id,
            page_size=10,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(read_messages, params)
        data = json.loads(output)

        # At least one message should come back (our corrupted one)
        found_corrupt = any(
            m.get("type") == "parse_error"
            for m in data["messages"]
        )
        self.assertTrue(found_corrupt, "Expected parse_error entry for corrupted data")

        # And the text must be free of binary characters
        for msg in data["messages"]:
            self.assertNotIn("\x00", msg["text"])
            self.assertNotIn("\x07", msg["text"])
            self.assertNotIn("\x08", msg["text"])

        # Clean up
        conn = sqlite3.connect(_clean_db_path())
        conn.execute(
            "DELETE FROM part WHERE message_id = 'test-binary-003'",
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
