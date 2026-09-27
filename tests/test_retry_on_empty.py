#!/usr/bin/env python3
"""
Tests for the BM25 keyword-reduction retry mechanism.

When ``recall_session`` receives a multi-word query that matches zero
documents, it progressively drops the last keyword and retries.
These tests mock the FTS queries to simulate that scenario deterministically.
"""

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))
os.environ.setdefault("FTS_DB_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
) + "_test-fts.db")
os.environ.setdefault("SERVER_NAME", "memory")

from database import init_db
init_db()

from models import RecallSessionInput, ResponseFormat
from tools import recall_session
from tests.base import _call_tool


# ── Fixtures (module-level so closures can access them) ─────────────────


def _make_result_row(session_id="sess-1", part_id="part-1"):
    """Return a minimal result list matching the FTS SELECT columns.

    Keys must match the SQL aliases exactly:
    ``part_fts.directory AS session_directory`` → key ``session_directory``
    """
    return [
        {
            "session_id": session_id,
            "fts_text": "This text contains the keyword we are searching for",
            "session_slug": "test-session",
            "session_title": "Test Session",
            "server": "memory",
            "session_time_created": "2026-09-15T00:00:00",
            "session_directory": "/test/dir",
            "agent": "general",
            "part_id": part_id,
            "part_type": "text",
            "part_time_created": "2026-09-15T00:00:00",
            "tool": None,
            "title": None,
            "bm25_score": -5.0,
        }
    ]


def _make_stats_row(matches=2, sessions=1):
    """Return a plain tuple like fetch_recall_stats does."""
    return (matches, sessions)


class TestRetryOnEmpty(unittest.TestCase):
    """Verify the retry loop when all keywords initially return zero results."""

    # ── End-to-end via tools ──────────────────────────────────────────────

    def test_results_found_on_keyword_reduction(self):
        """When full keywords return 0, reduced keywords find results."""
        call_order = []

        def results_side_effect(*args, **kwargs):
            call_order.append("results")
            if len(call_order) == 1:
                return []
            return _make_result_row()

        def stats_side_effect(*args, **kwargs):
            call_order.append("stats")
            if len(call_order) <= 2:
                return _make_stats_row(0, 0)
            return _make_stats_row(1, 1)

        with patch("tools.fetch_recall_results", side_effect=results_side_effect), \
             patch("tools.fetch_recall_stats", side_effect=stats_side_effect):
            params = RecallSessionInput(
                keywords="a b c",
                response_format=ResponseFormat.JSON,
            )
            output = _call_tool(recall_session, params)
            data = json.loads(output)

        self.assertGreater(len(data["matches"]), 0,
                           "Should find results after reducing keywords")
        self.assertEqual(data["query"]["keywords"], ["a", "b", "c"],
                         "Query object should still show original keywords")

    def test_stats_found_on_keyword_reduction(self):
        """Stats should also benefit from keyword reduction.

        The retry loop lives inside ``_search_with_retry`` which calls
        ``_query_fts`` directly.  So we patch ``_query_fts`` here too.

        NOTE: The mock must return a *falsy* value (``None``) when there
        are no results — ``{"matches": 0, "sessions": 0}`` is truthy,
        so ``if row:`` passes and the code returns ``(0, 0)`` without
        retrying.
        """
        stats_call_count = 0

        def query_fts_side_effect(sql, params=(), fetch_all=True):
            nonlocal stats_call_count
            if fetch_all is False:
                stats_call_count += 1
                if stats_call_count == 1:
                    return None  # Falsy → triggers retry
                return {"matches": 3, "sessions": 2}
            return _make_result_row()

        with patch("services._ensure_fts_available"), \
             patch("services._query_fts", side_effect=query_fts_side_effect):
            params = RecallSessionInput(
                keywords="x y z",
                response_format=ResponseFormat.JSON,
            )
            output = _call_tool(recall_session, params)
            data = json.loads(output)

        self.assertGreater(data["total_matches"], 0,
                           "Stats should find matches after retry")
        self.assertGreater(data["total_sessions"], 0,
                           "Stats should find sessions after retry")

    def test_empty_when_all_keywords_exhausted(self):
        """When even single keyword returns 0, result stays empty."""
        def results_side_effect(*args, **kwargs):
            return []

        def stats_side_effect(*args, **kwargs):
            return _make_stats_row(0, 0)

        with patch("tools.fetch_recall_results", side_effect=results_side_effect), \
             patch("tools.fetch_recall_stats", side_effect=stats_side_effect):
            params = RecallSessionInput(
                keywords="nonexistent_xyz",
                response_format=ResponseFormat.JSON,
            )
            output = _call_tool(recall_session, params)
            data = json.loads(output)

        self.assertEqual(len(data["matches"]), 0)
        self.assertEqual(data["total_matches"], 0)
        self.assertEqual(data["total_sessions"], 0)

    def test_no_retry_when_first_try_succeeds(self):
        """When full keywords return results, no retry should happen."""
        call_count = {"results": 0}

        def results_side_effect(*args, **kwargs):
            call_count["results"] += 1
            return _make_result_row()

        with patch("tools.fetch_recall_results", side_effect=results_side_effect), \
             patch("tools.fetch_recall_stats", return_value=_make_stats_row(5, 3)):
            params = RecallSessionInput(
                keywords="database",
                response_format=ResponseFormat.JSON,
            )
            _call_tool(recall_session, params)

        self.assertEqual(call_count["results"], 1,
                         "Should not retry when first attempt succeeds")

    # ── Low-level via services._query_fts ─────────────────────────────────

    def test_stats_retry_with_filters(self):
        """Retry with directory/agent filters should pass them each time."""
        stats_call_count = 0
        stats_keywords = []  # Keyword param from each stats query
        stats_agents = []    # Agent param from each stats query

        def query_fts_side_effect(sql, params=(), fetch_all=True):
            nonlocal stats_call_count
            if fetch_all is False:
                stats_call_count += 1
                # With directory + agent:
                #   params = (directory, agent, quoted_keywords)
                if "MATCH" in sql and len(params) >= 3:
                    stats_keywords.append(params[2])
                    stats_agents.append(params[1])
                if stats_call_count == 1:
                    return None  # Falsy → triggers retry
                return {"matches": 1, "sessions": 1}
            return _make_result_row()

        with patch("services._ensure_fts_available"), \
             patch("services._query_fts", side_effect=query_fts_side_effect):
            params = RecallSessionInput(
                keywords="a b c",
                directory="/home/user/project",
                agent="explorer",
                response_format=ResponseFormat.JSON,
            )
            _call_tool(recall_session, params)

        # First call: 3 keywords, second call: 2 keywords
        self.assertIn('"c"', stats_keywords[0])
        self.assertNotIn('"c"', stats_keywords[1])
        # Agent should appear on both attempts
        self.assertEqual(stats_agents[0], "explorer")
        self.assertEqual(stats_agents[1], "explorer")


if __name__ == "__main__":
    unittest.main()
