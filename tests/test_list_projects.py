#!/usr/bin/env python3
"""
E2E tests for ``list_projects`` tool.

Tests:
  - Returns a non-empty list of projects (directories).
  - Each project has the expected fields (directory, session_count, latest_session).
  - Both markdown and JSON output formats work.
  - Results are sorted newest first.
"""

import json
import unittest

from models import ListProjectsInput, ResponseFormat
from tools import list_projects

try:
    from .base import TestBase
except ImportError:
    from base import TestBase


# ── Helpers ────────────────────────────────────────────────────────────────────


def _get_projects_json() -> dict:
    """Call the tool and return parsed JSON data."""
    params = ListProjectsInput(response_format=ResponseFormat.JSON)
    output = TestBase._call_tool(list_projects, params)
    return json.loads(output)


def _get_projects_markdown() -> str:
    """Call the tool and return markdown output."""
    params = ListProjectsInput(response_format=ResponseFormat.MARKDOWN)
    return TestBase._call_tool(list_projects, params)


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestListProjectsJSON(TestBase):
    """list_projects with JSON output format."""

    def test_returns_non_empty_list(self):
        data = _get_projects_json()
        self.assertGreater(len(data["projects"]), 0, "Should have at least one project")

    def test_has_projects_key(self):
        data = _get_projects_json()
        self.assertIn("projects", data)

    def test_each_project_has_directory(self):
        data = _get_projects_json()
        for p in data["projects"]:
            self.assertIn("directory", p)
            self.assertIsInstance(p["directory"], str)
            self.assertGreater(len(p["directory"]), 0)

    def test_each_project_has_session_count(self):
        data = _get_projects_json()
        for p in data["projects"]:
            self.assertIn("session_count", p)
            self.assertIsInstance(p["session_count"], int)
            self.assertGreater(p["session_count"], 0)

    def test_each_project_has_latest_session(self):
        data = _get_projects_json()
        for p in data["projects"]:
            self.assertIn("latest_session", p)
            self.assertIsInstance(p["latest_session"], str)

    def test_timestamps_are_readable(self):
        data = _get_projects_json()
        for p in data["projects"]:
            self.assertIn("-", p["latest_session"])
            self.assertIn(":", p["latest_session"])

    def test_sorting_newest_first(self):
        data = _get_projects_json()
        projects = data["projects"]
        if len(projects) < 2:
            self.skipTest("Need at least 2 projects to test sorting")
        from datetime import datetime
        for i in range(len(projects) - 1):
            t1 = datetime.strptime(projects[i]["latest_session"], "%Y-%m-%d %H:%M:%S UTC")
            t2 = datetime.strptime(projects[i + 1]["latest_session"], "%Y-%m-%d %H:%M:%S UTC")
            self.assertGreaterEqual(t1, t2, "Projects should be sorted by latest_session DESC")


class TestListProjectsMarkdown(TestBase):
    """list_projects with markdown output format."""

    def setUp(self):
        self.output = _get_projects_markdown()

    def test_output_not_empty(self):
        self.assertTrue(len(self.output) > 0)

    def test_has_header(self):
        self.assertIn("# Projects (by Directory)", self.output)

    def test_has_count_line(self):
        self.assertIn("project(s)", self.output)

    def test_has_directory_entries(self):
        self.assertIn("**Sessions**:", self.output)

    def test_has_latest_session_entries(self):
        self.assertIn("**Latest Session**:", self.output)


if __name__ == "__main__":
    unittest.main()
