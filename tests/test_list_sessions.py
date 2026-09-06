#!/usr/bin/env python3
"""
E2E tests for ``list_sessions`` tool.

Tests:
  - Returns a list of sessions for a given directory.
  - Pagination (limit, offset) works correctly.
  - Both markdown and JSON output formats.
  - Session fields are present (id, slug, title, agent, etc.).
  - Cross-directory filtering works.
"""

import json
import os
import sqlite3
import unittest

os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))

from models import ListProjectsInput, ListSessionsInput, ResponseFormat
from tools import list_projects, list_sessions

from config import DEFAULT_LIST_LIMIT

try:
    from .base import TestBase, _clean_db_path
except ImportError:
    from base import TestBase, _clean_db_path


def _get_projects_json():
    """Get a list of projects from the tool (module-level helper)."""
    params = ListProjectsInput(response_format=ResponseFormat.JSON)
    output = TestBase._call_tool(list_projects, params)
    return json.loads(output)["projects"]


class TestListSessionsJSON(TestBase):
    """list_sessions with JSON output format."""

    def setUp(self):
        # Discover a directory with sessions at runtime
        projects = _get_projects_json()
        if len(projects) < 2:
            self.skipTest("Need at least 2 projects for this test")
        # Use the second directory (has the most sessions)
        self.directory = projects[1]["directory"]
        self.expected_count = self.get_session_count(self.directory)

        params = ListSessionsInput(
            directory=self.directory,
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(list_sessions, params)
        self.data = json.loads(output)

    def test_returns_json_with_directory(self):
        self.assertIn("directory", self.data)
        self.assertEqual(self.data["directory"], self.directory)

    def test_returns_total_count(self):
        self.assertIn("total", self.data)
        self.assertEqual(self.data["total"], self.expected_count)

    def test_returns_session_list(self):
        self.assertIn("sessions", self.data)
        self.assertIsInstance(self.data["sessions"], list)

    def test_default_limit_returns_correct_number(self):
        # Default limit is DEFAULT_LIST_LIMIT (20)
        self.assertEqual(len(self.data["sessions"]), min(DEFAULT_LIST_LIMIT, self.expected_count))

    def test_each_session_has_id(self):
        for s in self.data["sessions"]:
            self.assertIn("id", s)
            self.assertIsInstance(s["id"], str)

    def test_each_session_has_slug(self):
        for s in self.data["sessions"]:
            self.assertIn("slug", s)

    def test_each_session_has_title(self):
        for s in self.data["sessions"]:
            self.assertIn("title", s)

    def test_each_session_has_agent(self):
        for s in self.data["sessions"]:
            self.assertIn("agent", s)

    def test_each_session_has_tokens(self):
        for s in self.data["sessions"]:
            self.assertIn("tokens_input", s)
            self.assertIn("tokens_output", s)
            self.assertIn("tokens_reasoning", s)
            self.assertIsInstance(s["tokens_input"], int)
            self.assertIsInstance(s["tokens_output"], int)

    def test_each_session_has_timestamps(self):
        for s in self.data["sessions"]:
            self.assertIn("time_created", s)
            self.assertIn("time_updated", s)

    def test_sessions_sorted_newest_first(self):
        sessions = self.data["sessions"]
        if len(sessions) < 2:
            self.skipTest("Need at least 2 sessions")
        from datetime import datetime
        for i in range(len(sessions) - 1):
            t1 = datetime.strptime(sessions[i]["time_created"], "%Y-%m-%d %H:%M:%S UTC")
            t2 = datetime.strptime(sessions[i + 1]["time_created"], "%Y-%m-%d %H:%M:%S UTC")
            self.assertGreaterEqual(t1, t2)


class TestListSessionsPagination(TestBase):
    """Pagination for list_sessions."""

    def setUp(self):
        projects = _get_projects_json()
        # Find a directory with at least 5 sessions to ensure pagination tests pass
        conn = sqlite3.connect(_clean_db_path())
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for p in projects:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM session WHERE directory = ?",
                (p["directory"],),
            )
            cnt = cur.fetchone()["cnt"]
            if cnt >= 5:
                self.directory = p["directory"]
                break
        else:
            # Fallback: use the first directory
            self.directory = projects[0]["directory"]
        conn.close()

    def test_limit_1_returns_one_session(self):
        params = ListSessionsInput(
            directory=self.directory,
            limit=1,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), 1)

    def test_limit_5_returns_five_sessions(self):
        params = ListSessionsInput(
            directory=self.directory,
            limit=5,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), 5)

    def test_offset_skips_sessions(self):
        """Offset 0 gives first N, offset 1 gives next N."""
        params0 = ListSessionsInput(
            directory=self.directory, limit=2, offset=0,
            response_format=ResponseFormat.JSON,
        )
        params2 = ListSessionsInput(
            directory=self.directory, limit=2, offset=2,
            response_format=ResponseFormat.JSON,
        )
        data0 = json.loads(self._call_tool(list_sessions, params0))
        data2 = json.loads(self._call_tool(list_sessions, params2))

        # The session IDs should be different
        ids0 = [s["id"] for s in data0["sessions"]]
        ids2 = [s["id"] for s in data2["sessions"]]
        self.assertNotEqual(ids0[0], ids2[0])

    def test_offset_beyond_total_returns_empty(self):
        params = ListSessionsInput(
            directory=self.directory,
            limit=10,
            offset=10000,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), 0)

    def test_offset_with_limit(self):
        """Offset + limit should return the next page."""
        params_first = ListSessionsInput(
            directory=self.directory, limit=3, offset=0,
            response_format=ResponseFormat.JSON,
        )
        params_second = ListSessionsInput(
            directory=self.directory, limit=3, offset=3,
            response_format=ResponseFormat.JSON,
        )
        data_first = json.loads(self._call_tool(list_sessions, params_first))
        data_second = json.loads(self._call_tool(list_sessions, params_second))

        # No overlap between pages
        ids_first = set(s["id"] for s in data_first["sessions"])
        ids_second = set(s["id"] for s in data_second["sessions"])
        self.assertEqual(len(ids_first & ids_second), 0)

    def test_pagination_with_max_page_size(self):
        params = ListSessionsInput(
            directory=self.directory,
            limit=100,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        # Should return sessions without error; count respects the limit
        self.assertGreater(len(data["sessions"]), 0)
        self.assertLessEqual(len(data["sessions"]), min(data["total"], 100))


class TestListSessionsMarkdown(TestBase):
    """list_sessions with markdown output format."""

    def setUp(self):
        projects = _get_projects_json()
        if len(projects) < 2:
            self.skipTest("Need at least 2 projects for this test")
        self.directory = projects[1]["directory"]
        params = ListSessionsInput(
            directory=self.directory,
            response_format=ResponseFormat.MARKDOWN,
        )
        self.output = self._call_tool(list_sessions, params)

    def test_output_not_empty(self):
        self.assertTrue(len(self.output) > 0)

    def test_has_session_header(self):
        self.assertIn("Sessions", self.output)

    def test_has_total_line(self):
        self.assertIn("Total", self.output)

    def test_has_showing_line(self):
        self.assertIn("Showing", self.output)


class TestListSessionsCrossDirectory(TestBase):
    """Sessions are scoped to the given directory."""

    def setUp(self):
        projects = _get_projects_json()
        if len(projects) < 3:
            self.skipTest("Need at least 3 projects for cross-directory test")
        self.directory_a = projects[1]["directory"]  # has many sessions
        self.directory_b = projects[2]["directory"]  # has fewer sessions

    def test_different_directories_have_different_sessions(self):
        params_a = ListSessionsInput(
            directory=self.directory_a, limit=3,
            response_format=ResponseFormat.JSON,
        )
        params_b = ListSessionsInput(
            directory=self.directory_b, limit=3,
            response_format=ResponseFormat.JSON,
        )
        data_a = json.loads(self._call_tool(list_sessions, params_a))
        data_b = json.loads(self._call_tool(list_sessions, params_b))

        ids_a = set(s["id"] for s in data_a["sessions"])
        ids_b = set(s["id"] for s in data_b["sessions"])
        self.assertEqual(len(ids_a & ids_b), 0)

    def test_directory_with_fewer_sessions_returns_all(self):
        params = ListSessionsInput(
            directory=self.directory_b, limit=100,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(list_sessions, params))
        self.assertEqual(len(data["sessions"]), data["total"])


if __name__ == "__main__":
    unittest.main()
