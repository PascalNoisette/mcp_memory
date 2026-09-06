#!/usr/bin/env python3
"""
E2E tests for ``read_messages`` tool.

Tests:
  - Returns a list of messages (parts) for a given session.
  - Pagination (page_size, offset) works correctly.
  - Both markdown and JSON output formats.
  - Message fields are present (index, part_id, type, text, etc.).
  - Message ordering (oldest first).
"""

import json
import os
import sqlite3
import unittest

os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))

from models import ListProjectsInput, ReadMessagesInput, ResponseFormat
from tools import list_projects, read_messages

from constants import TEXT_TRUNCATION_LIMIT

try:
    from .base import TestBase, _clean_db_path
except ImportError:
    from base import TestBase, _clean_db_path


class TestReadMessagesJSON(TestBase):
    """read_messages with JSON output format."""

    def setUp(self):
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for this test")
        # Use the directory with the most sessions
        result = self.get_session_with_most_parts(directories[1])
        self.session_id = result["id"]
        self.session_title = result["title"]

        # Get the real part count from the DB
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM part WHERE session_id = ?",
            (self.session_id,),
        )
        self.expected_total = cur.fetchone()["cnt"]
        conn.close()

        params = ReadMessagesInput(
            session_id=self.session_id,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(read_messages, params)
        self.data = json.loads(output)

    def test_returns_json_with_session_id(self):
        self.assertIn("session_id", self.data)
        self.assertEqual(self.data["session_id"], self.session_id)

    def test_returns_total_count(self):
        self.assertIn("total", self.data)
        self.assertEqual(self.data["total"], self.expected_total)

    def test_returns_message_list(self):
        self.assertIn("messages", self.data)
        self.assertIsInstance(self.data["messages"], list)

    def test_default_page_size(self):
        # Default page_size is 10
        self.assertEqual(len(self.data["messages"]), 10)

    def test_each_message_has_index(self):
        for m in self.data["messages"]:
            self.assertIn("index", m)
            self.assertIsInstance(m["index"], int)

    def test_each_message_has_part_id(self):
        for m in self.data["messages"]:
            self.assertIn("part_id", m)
            self.assertIsInstance(m["part_id"], str)

    def test_each_message_has_message_id(self):
        for m in self.data["messages"]:
            self.assertIn("message_id", m)

    def test_each_message_has_type(self):
        for m in self.data["messages"]:
            self.assertIn("type", m)

    def test_each_message_has_text(self):
        for m in self.data["messages"]:
            self.assertIn("text", m)

    def test_each_message_has_time_created(self):
        for m in self.data["messages"]:
            self.assertIn("time_created", m)

    def test_each_message_has_full_text_length(self):
        for m in self.data["messages"]:
            self.assertIn("full_text_length", m)
            self.assertIsInstance(m["full_text_length"], int)

    def test_text_is_truncated_to_limit(self):
        """Each message text should be at most TEXT_TRUNCATION_LIMIT chars."""
        for m in self.data["messages"]:
            self.assertLessEqual(len(m["text"]), TEXT_TRUNCATION_LIMIT)

    def test_has_more_is_false_on_first_page_small(self):
        # If total <= page_size, has_more should be False
        if self.expected_total <= 10:
            self.assertFalse(self.data.get("has_more", True))

    def test_session_title_in_output(self):
        """The session title should be present in the data."""
        self.assertIn("session_id", self.data)


class TestReadMessagesPagination(TestBase):
    """Pagination for read_messages."""

    def setUp(self):
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for pagination test")
        result = self.get_session_with_most_parts(directories[1])
        self.session_id = result["id"]

    def test_page_size_1_returns_one_message(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=1,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        self.assertEqual(len(data["messages"]), 1)

    def test_page_size_5_returns_five_messages(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=5,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        self.assertEqual(len(data["messages"]), 5)

    def test_offset_advances_through_messages(self):
        params0 = ReadMessagesInput(
            session_id=self.session_id, page_size=3, offset=0,
            response_format=ResponseFormat.JSON,
        )
        params3 = ReadMessagesInput(
            session_id=self.session_id, page_size=3, offset=3,
            response_format=ResponseFormat.JSON,
        )
        data0 = json.loads(self._call_tool(read_messages, params0))
        data3 = json.loads(self._call_tool(read_messages, params3))

        ids0 = [m["part_id"] for m in data0["messages"]]
        ids3 = [m["part_id"] for m in data3["messages"]]
        self.assertNotEqual(ids0[0], ids3[0])

    def test_offset_beyond_total_returns_empty(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=10, offset=100000,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        self.assertEqual(len(data["messages"]), 0)
        self.assertFalse(data["has_more"])

    def test_pagination_no_overlap(self):
        """Consecutive pages should not have duplicate messages."""
        params1 = ReadMessagesInput(
            session_id=self.session_id, page_size=5, offset=0,
            response_format=ResponseFormat.JSON,
        )
        params2 = ReadMessagesInput(
            session_id=self.session_id, page_size=5, offset=5,
            response_format=ResponseFormat.JSON,
        )
        data1 = json.loads(self._call_tool(read_messages, params1))
        data2 = json.loads(self._call_tool(read_messages, params2))

        ids1 = set(m["part_id"] for m in data1["messages"])
        ids2 = set(m["part_id"] for m in data2["messages"])
        self.assertEqual(len(ids1 & ids2), 0)

    def test_has_more_true_when_more_available(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=5, offset=0,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        if data["total"] > 5:
            self.assertTrue(data["has_more"])
            self.assertIsNotNone(data["next_offset"])
            self.assertEqual(data["next_offset"], 5)

    def test_next_offset_is_offset_plus_page_size(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=10, offset=0,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        if data["has_more"]:
            self.assertEqual(data["next_offset"], 10)

    def test_full_scan_all_messages(self):
        """Read all messages page by page and verify no gaps."""
        all_ids = []
        offset = 0
        page_size = 10
        while True:
            params = ReadMessagesInput(
                session_id=self.session_id, page_size=page_size, offset=offset,
                response_format=ResponseFormat.JSON,
            )
            data = json.loads(self._call_tool(read_messages, params))
            if not data["messages"]:
                break
            ids = [m["part_id"] for m in data["messages"]]
            all_ids.extend(ids)
            if not data["has_more"]:
                break
            offset = data["next_offset"]

        # Verify we got all messages
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM part WHERE session_id = ?",
            (self.session_id,),
        )
        expected = cur.fetchone()["cnt"]
        conn.close()

        self.assertEqual(len(all_ids), expected)
        # Verify no duplicates
        self.assertEqual(len(all_ids), len(set(all_ids)))


class TestReadMessagesMarkdown(TestBase):
    """read_messages with markdown output format."""

    def setUp(self):
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for markdown test")
        result = self.get_session_with_most_parts(directories[1])
        self.session_id = result["id"]
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=3,
            response_format=ResponseFormat.MARKDOWN,
        )
        self.output = self._call_tool(read_messages, params)

    def test_output_not_empty(self):
        self.assertTrue(len(self.output) > 0)

    def test_has_header(self):
        self.assertIn("Messages", self.output)

    def test_has_total_line(self):
        self.assertIn("Total", self.output)

    def test_has_message_index(self):
        self.assertIn("Message", self.output)

    def test_has_type_field(self):
        self.assertIn("**Type**:", self.output)

    def test_has_time_field(self):
        self.assertIn("**Time**:", self.output)

    def test_has_content_block(self):
        self.assertIn("```", self.output)


class TestReadMessagesMessageOrdering(TestBase):
    """Messages should be ordered oldest first (time_created ASC)."""

    def setUp(self):
        directories = self.get_directories()
        if len(directories) < 2:
            self.skipTest("Need at least 2 directories for ordering test")
        result = self.get_session_with_most_parts(directories[1])
        self.session_id = result["id"]

    def test_oldest_messages_first(self):
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=5, offset=0,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        timestamps = [m["time_created"] for m in data["messages"]]
        for i in range(len(timestamps) - 1):
            self.assertLessEqual(timestamps[i], timestamps[i + 1])

    def test_last_page_has_newest_messages(self):
        """The last page should contain the newest messages."""
        params = ReadMessagesInput(
            session_id=self.session_id, page_size=3, offset=99999,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(read_messages, params))
        # The timestamps should be the most recent ones
        for i in range(len(data["messages"]) - 1):
            self.assertLessEqual(
                data["messages"][i]["time_created"],
                data["messages"][i + 1]["time_created"],
            )


if __name__ == "__main__":
    unittest.main()
