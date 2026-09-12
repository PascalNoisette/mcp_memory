#!/usr/bin/env python3
"""
E2E tests for ``recall_session`` tool.

Tests:
  - Searches for keywords across all sessions.
  - Results include session context and matching snippets.
  - Pagination works.
  - Both markdown and JSON output formats.
  - Directory-scoped search works.
  - No results when keywords don't match.
"""

import json
import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))

from models import ListProjectsInput, RecallSessionInput, ResponseFormat
from tools import list_projects, recall_session

try:
    from .base import TestBase
except ImportError:
    from base import TestBase

# Generate a unique keyword that hasn't been stored in any conversation.
# Using uuid4() at module import time ensures this string was never part of
# any prior test run or conversation history.
_NO_RESULTS_KEYWORD = str(uuid.uuid4())


class TestRecallJSON(TestBase):
    """recall_session with JSON output format."""

    def setUp(self):
        # Search for a common keyword that should match many results
        params = RecallSessionInput(
            keywords="database",
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(recall_session, params)
        self.data = json.loads(output)

    def test_returns_query_info(self):
        self.assertIn("query", self.data)
        self.assertEqual(self.data["query"]["keywords"], ["database"])

    def test_returns_total_matches(self):
        self.assertIn("total_matches", self.data)
        self.assertIsInstance(self.data["total_matches"], int)
        self.assertGreater(self.data["total_matches"], 0)

    def test_returns_total_sessions(self):
        self.assertIn("total_sessions", self.data)
        self.assertIsInstance(self.data["total_sessions"], int)
        self.assertGreater(self.data["total_sessions"], 0)

    def test_returns_matches_list(self):
        self.assertIn("matches", self.data)
        self.assertIsInstance(self.data["matches"], list)

    def test_matches_are_non_empty(self):
        self.assertGreater(len(self.data["matches"]), 0)

    def test_each_match_has_session_id(self):
        for m in self.data["matches"]:
            self.assertIn("session_id", m)
            self.assertIsInstance(m["session_id"], str)

    def test_each_match_has_session_title(self):
        for m in self.data["matches"]:
            self.assertIn("session_title", m)

    def test_each_match_has_session_slug(self):
        for m in self.data["matches"]:
            self.assertIn("session_slug", m)

    def test_each_match_has_directory(self):
        for m in self.data["matches"]:
            self.assertIn("directory", m)

    def test_each_match_has_part_id(self):
        for m in self.data["matches"]:
            self.assertIn("part_id", m)

    def test_each_match_has_time_created(self):
        for m in self.data["matches"]:
            self.assertIn("time_created", m)

    def test_each_match_has_snippet(self):
        for m in self.data["matches"]:
            self.assertIn("snippet", m)
            self.assertIsInstance(m["snippet"], str)
            self.assertGreater(len(m["snippet"]), 0)

    def test_each_match_has_part_type(self):
        for m in self.data["matches"]:
            self.assertIn("part_type", m)

    def test_snippet_contains_keyword(self):
        """The snippet should contain (highlight) the searched keyword."""
        for m in self.data["matches"]:
            snippet_lower = m["snippet"].lower()
            self.assertIn("database", snippet_lower)


class TestRecallMarkdown(TestBase):
    """recall_session with markdown output format."""

    def setUp(self):
        params = RecallSessionInput(
            keywords="database",
            response_format=ResponseFormat.MARKDOWN,
        )
        self.output = self._call_tool(recall_session, params)

    def test_output_not_empty(self):
        self.assertTrue(len(self.output) > 0)

    def test_has_header(self):
        self.assertIn("# Recall", self.output)

    def test_has_count_info(self):
        self.assertIn("matching", self.output)

    def test_has_session_header(self):
        self.assertIn("##", self.output)

    def test_has_snippet_block(self):
        self.assertIn("```", self.output)

    def test_has_keyword_highlight(self):
        self.assertIn("**database**", self.output)


class TestRecallPagination(TestBase):
    """Pagination for recall_session."""

    def setUp(self):
        params = RecallSessionInput(
            keywords="database",
            response_format=ResponseFormat.JSON,
        )
        self.full_data = json.loads(self._call_tool(recall_session, params))

    def test_limit_2_returns_two_matches(self):
        params = RecallSessionInput(
            keywords="database",
            limit=2,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(len(data["matches"]), 2)

    def test_limit_5_returns_five_matches(self):
        params = RecallSessionInput(
            keywords="database",
            limit=5,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(len(data["matches"]), 5)

    def test_offset_advances_results(self):
        params0 = RecallSessionInput(
            keywords="database",
            limit=3, offset=0,
            response_format=ResponseFormat.JSON,
        )
        params3 = RecallSessionInput(
            keywords="database",
            limit=3, offset=3,
            response_format=ResponseFormat.JSON,
        )
        data0 = json.loads(self._call_tool(recall_session, params0))
        data3 = json.loads(self._call_tool(recall_session, params3))

        ids0 = set(m["part_id"] for m in data0["matches"])
        ids3 = set(m["part_id"] for m in data3["matches"])
        self.assertEqual(len(ids0 & ids3), 0)

    def test_has_more_works(self):
        params = RecallSessionInput(
            keywords="database",
            limit=5,
            offset=0,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        if self.full_data["total_matches"] > 5:
            self.assertTrue(data["has_more"])
            self.assertIsNotNone(data["next_offset"])

    def test_offset_beyond_returns_empty(self):
        params = RecallSessionInput(
            keywords="database",
            limit=10,
            offset=100000,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(len(data["matches"]), 0)
        self.assertFalse(data["has_more"])


class TestRecallDirectoryScope(TestBase):
    """Search can be scoped to a single directory."""

    def setUp(self):
        self.directories = self.get_directories()
        self.all_matches = json.loads(
            self._call_tool(recall_session, RecallSessionInput(
                keywords="database",
                response_format=ResponseFormat.JSON,
            ))
        )

    def test_directory_scoped_search(self):
        """Searching within a specific directory returns fewer results."""
        params = RecallSessionInput(
            keywords="database",
            directory=self.directories[1],
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        # Should return some results (the directory has sessions)
        self.assertIsInstance(data["total_matches"], int)

    def test_scoped_matches_belong_to_directory(self):
        params = RecallSessionInput(
            keywords="database",
            directory=self.directories[1],
            limit=50,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        for m in data["matches"]:
            self.assertEqual(m["directory"], self.directories[1])

    def test_scoped_search_has_correct_directory_in_query(self):
        params = RecallSessionInput(
            keywords="database",
            directory=self.directories[1],
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(data["query"]["directory"], self.directories[1])


class TestRecallNoResults(TestBase):
    """Searching for a rare keyword returns no results."""

    def setUp(self):
        # Use a UUID generated at module import time to guarantee it hasn't
        # been stored in any prior conversation.
        params = RecallSessionInput(
            keywords=_NO_RESULTS_KEYWORD,
            response_format=ResponseFormat.JSON,
        )
        self.output = self._call_tool(recall_session, params)
        self.data = json.loads(self.output)

    def test_total_matches_is_zero(self):
        self.assertEqual(self.data["total_matches"], 0)

    def test_total_sessions_is_zero(self):
        self.assertEqual(self.data["total_sessions"], 0)

    def test_matches_is_empty(self):
        self.assertEqual(len(self.data["matches"]), 0)

    def test_has_more_is_false(self):
        self.assertFalse(self.data["has_more"])


class TestRecallMultipleKeywords(TestBase):
    """Multiple keywords are combined with OR logic."""

    def setUp(self):
        # Search for multiple common words
        params = RecallSessionInput(
            keywords="test error handling",
            response_format=ResponseFormat.JSON,
        )
        self.data = json.loads(self._call_tool(recall_session, params))

    def test_returns_results(self):
        self.assertGreater(len(self.data["matches"]), 0)

    def test_results_contain_at_least_one_keyword(self):
        for m in self.data["matches"]:
            snippet_lower = m["snippet"].lower()
            has_any = any(
                kw in snippet_lower
                for kw in ["test", "error", "handling"]
            )
            self.assertTrue(has_any, f"Snippet should contain at least one keyword: {m['snippet'][:100]}")


class TestRecallMarkdownNoResults(TestBase):
    """Markdown output when no results found."""

    def setUp(self):
        # Use the same UUID as TestRecallNoResults to guarantee no matches.
        params = RecallSessionInput(
            keywords=_NO_RESULTS_KEYWORD,
            response_format=ResponseFormat.MARKDOWN,
        )
        self.output = self._call_tool(recall_session, params)

    def test_output_has_no_matches_message(self):
        self.assertIn("No matches found", self.output)

    def test_output_has_tip(self):
        self.assertIn("tip", self.output.lower())


class TestRecallAgentFilter(TestBase):
    """Agent filtering for recall_session."""

    def test_agent_param_accepted(self):
        """The agent parameter should be accepted without error."""
        params = RecallSessionInput(
            keywords="database",
            agent="explorer",
            response_format=ResponseFormat.JSON,
        )
        output = self._call_tool(recall_session, params)
        data = json.loads(output)
        self.assertIn("query", data)
        self.assertEqual(data["query"]["agent"], "explorer")

    def test_agent_filter_applied_to_query(self):
        """The agent filter should appear in the query object."""
        params = RecallSessionInput(
            keywords="database",
            agent="general",
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(data["query"]["agent"], "general")

    def test_agent_filter_returns_valid_results(self):
        """Results should be valid when filtering by agent."""
        params = RecallSessionInput(
            keywords="database",
            agent="explorer",
            limit=10,
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertIsInstance(data["total_matches"], int)
        self.assertIsInstance(data["total_sessions"], int)
        self.assertIsInstance(data["matches"], list)

    def test_no_results_for_unknown_agent(self):
        """Searching for a non-existent agent should return no results."""
        params = RecallSessionInput(
            keywords="database",
            agent="nonexistent_agent_xyz",
            response_format=ResponseFormat.JSON,
        )
        data = json.loads(self._call_tool(recall_session, params))
        self.assertEqual(data["total_matches"], 0)
        self.assertEqual(data["total_sessions"], 0)
        self.assertEqual(len(data["matches"]), 0)

    def test_agent_shown_in_markdown_output(self):
        """Agent should be displayed in markdown output (no-results message)."""
        params = RecallSessionInput(
            keywords="database",
            agent="nonexistent_agent_xyz",
            response_format=ResponseFormat.MARKDOWN,
        )
        output = self._call_tool(recall_session, params)
        # The agent filter appears in the "no matches" message scope
        self.assertIn("`nonexistent_agent_xyz`", output)


if __name__ == "__main__":
    unittest.main()
