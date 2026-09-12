#!/usr/bin/env python3
"""
Business-logic / query layer for the Kilo Code MCP Memory Server.

Each function accepts validated parameters and returns structured Python
data (lists of dicts).  Formatting into Markdown or JSON is handled by
*formatters.py*.

## Recall queries

The FTS index (``part_fts``) stores **all metadata needed for recall**:
session_id, message_id, part_id, directory, agent, server, session
title/slug, and timestamps.  Queries run entirely against the FTS table
— no JOIN to the primary database is needed.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from database import get_db, get_fts_db, is_fts_available
from constants import HIGHLIGHT_CONTEXT_CHARS
from formatters import (
    format_timestamp,
    highlight_match,
    parse_part_data,
    sanitize_text,
)


def _query(sql: str, params: tuple = (), fetch_all: bool = True):
    """Execute a SELECT query and return rows.

    *fetch_all* controls whether all rows are returned (``fetchall``)
    or only the first row (``fetchone``).  Returns ``None`` when
    ``fetchone`` produces no result.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    if fetch_all:
        return cur.fetchall()
    return cur.fetchone()


def _query_fts(sql: str, params: tuple = (), fetch_all: bool = True):
    """Execute a SELECT query against the **FTS** database.

    Since the FTS table now stores all metadata needed for recall queries,
    no cross-database attachment is required.

    Args:
        sql: The SQL query (references ``fts.part_fts``).
        params: Query parameters.
        fetch_all: If True, return all rows; otherwise return the first row.

    Returns:
        Query results as a list of dicts (or a single dict if fetch_all=False).
    """
    conn = get_fts_db()
    cur = conn.cursor()
    cur.execute(sql, params)
    if fetch_all:
        return cur.fetchall()
    return cur.fetchone()


# ── Custom exception ─────────────────────────────────────────────────────────


class FTSUnavailableError(Exception):
    """Raised when the FTS5 full-text search index is missing or unavailable."""

    pass


def _ensure_fts_available() -> None:
    """Verify the FTS5 table exists and raise if it does not.

    This serves as a runtime guard so that ``recall_session`` produces a
    clear, actionable message instead of a raw SQLite error.
    """
    if not is_fts_available():
        raise FTSUnavailableError(
            "The full-text search index is not available. "
            "It will be created automatically on the next MCP server start. "
            "Meanwhile, all read tools (list_projects, list_sessions, "
            "read_messages) continue to work normally."
        )


# ── Projects ─────────────────────────────────────────────────────────────────


def fetch_projects() -> list[dict[str, Any]]:
    """Return all projects (directories) with session counts.

    Queries ``SELECT directory, COUNT(*) FROM session GROUP BY directory``
    to derive projects from actual session data instead of the project table.
    """
    rows = _query(
        """
        SELECT s.directory, COUNT(*) AS session_count,
               MAX(s.time_created) AS latest_session_time
        FROM session s
        GROUP BY s.directory
        ORDER BY latest_session_time DESC
        """
    )
    return [
        {
            "directory": row["directory"],
            "session_count": row["session_count"],
            "latest_session": format_timestamp(row["latest_session_time"]),
        }
        for row in rows
    ]


# ── Sessions ─────────────────────────────────────────────────────────────────


def fetch_session_count(directory: str) -> int:
    """Return the total number of sessions for a directory."""
    rows = _query(
        "SELECT COUNT(*) as cnt FROM session WHERE directory = ?",
        (directory,),
    )
    return rows[0]["cnt"]


def fetch_sessions(
    directory: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Return a page of sessions for a directory."""
    rows = _query(
        """
        SELECT id, slug, title, agent, model,
               time_created, time_updated,
               tokens_input, tokens_output, tokens_reasoning,
               cost
        FROM session
        WHERE directory = ?
        ORDER BY time_created DESC
        LIMIT ? OFFSET ?
        """,
        (directory, limit, offset),
    )
    return [
        {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "agent": row["agent"],
            "model": row["model"],
            "time_created": format_timestamp(row["time_created"]),
            "time_updated": format_timestamp(row["time_updated"]),
            "tokens_input": row["tokens_input"] or 0,
            "tokens_output": row["tokens_output"] or 0,
            "tokens_reasoning": row["tokens_reasoning"] or 0,
            "cost": row["cost"] or 0.0,
        }
        for row in rows
    ]


# ── Messages ─────────────────────────────────────────────────────────────────


def _fetch_messages_with_count(
    session_id: str,
    page_size: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Return (total_count, messages) using a single connection.

    Both queries run on the same thread-local connection, so they see a
    consistent snapshot of the database.  This prevents the count from
    drifting when new ``part`` rows are inserted between the count query
    and the page query — a race condition that occurs when the Kilo Code
    application writes to the shared SQLite database concurrently.

    Returns:
        ``(total_count, messages)`` where *total_count* is the number of
        ``part`` rows for *session_id* and *messages* is the requested page.
    """
    conn = get_db()
    cur = conn.cursor()

    # 1. Total count (snapshot at transaction start) — exclude step-start/step-finish
    cur.execute(
        """
        SELECT COUNT(*) as cnt FROM part
        WHERE session_id = ?
          AND json_valid(data) = 1
          AND json_extract(data, '$.type') NOT IN ('step-start', 'step-finish')
        """,
        (session_id,),
    )
    total = cur.fetchone()["cnt"]

    # 2. Paginated messages (same snapshot — no new inserts visible)
    cur.execute(
        """
        SELECT id, message_id, time_created, data,
               json_extract(data, '$.state.title') AS state_title,
               json_extract(data, '$.state.tool')  AS state_tool
        FROM part
        WHERE session_id = ?
          AND json_valid(data) = 1
          AND json_extract(data, '$.type') NOT IN ('step-start', 'step-finish')
        ORDER BY time_created ASC
        LIMIT ? OFFSET ?
        """,
        (session_id, page_size, offset),
    )
    rows = cur.fetchall()

    messages: list[dict[str, Any]] = []
    for row in rows:
        parsed = parse_part_data(row["data"] or "{}")
        text = sanitize_text(parsed.get("text", ""))
        messages.append(
            {
                "part_id": row["id"],
                "time_created": format_timestamp(row["time_created"]),
                "type": parsed.get("type", "unknown"),
                "text": text,
                "full_text_length": len(text),
                "raw_data": row["data"],
                "state_title": row["state_title"],
                "state_tool": row["state_tool"],
            }
        )

    return total, messages


def fetch_message_count(session_id: str) -> int:
    """Return the total number of messages (parts) for a session."""
    rows = _query(
        "SELECT COUNT(*) as cnt FROM part WHERE session_id = ?",
        (session_id,),
    )
    return rows[0]["cnt"]


def fetch_messages(
    session_id: str,
    page_size: int,
    offset: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Return (total_count, messages) for a session.

    Both values are computed in a single database call to guarantee
    consistency — the total count cannot drift between the count and
    the page query even when the database is actively being written to.

    Returns:
        A ``(total_count, messages)`` tuple where *total_count* is the
        total number of ``part`` rows for *session_id* and *messages*
        is the requested page of messages.
    """
    return _fetch_messages_with_count(session_id, page_size, offset)


# ── Recall (full-text search) ────────────────────────────────────────────────


def _build_fts_where_clause(
    directory: Optional[str],
    keywords: list[str],
    agent: Optional[str] = None,
    server: Optional[str] = None,
    title: Optional[str] = None,
    tool: Optional[str] = None,
) -> tuple[str, list]:
    """Build a parameterised WHERE clause for the FTS5 index.

    Uses ``fts.part_fts MATCH`` (full-text search) instead of ``LIKE`` on
    the raw JSON ``part.data`` column.  This eliminates JSON noise,
    provides BM25 relevance ranking, and is dramatically faster.

    All filterable columns (directory, agent, server) are stored directly
    in the FTS table — no JOIN to the primary database is needed.

    Returns ``(clause_sql, params)`` where *clause_sql* starts with ``WHERE``
    (or is empty when the caller prepends nothing).
    """
    parts: list[str] = []
    params: list[Any] = []

    # Track whether we should exclude tool parts.
    # Exclude tool parts only when neither title nor tool filter is active.
    show_tool_parts = (title is not None) or (tool is not None)

    if directory:
        parts.append("part_fts.directory = ?")
        params.append(directory)

    if agent:
        parts.append("part_fts.agent = ?")
        params.append(agent)

    if server:
        parts.append("part_fts.server = ?")
        params.append(server)

    # Only exclude tool-type parts when neither title nor tool filter is used.
    if not show_tool_parts:
        parts.append("part_fts.part_type != 'tool'")

    if title:
        # Fulltext search on part_fts.title column.
        # Split by spaces and quote each keyword for FTS5 MATCH.
        title_keywords = [kw.strip() for kw in title.split() if kw.strip()]
        if title_keywords:
            quoted = " ".join(f'"{kw}"' for kw in title_keywords)
            parts.append("part_fts.title MATCH ?")
            params.append(quoted)

    if tool:
        # Exact match on part_fts.tool column.
        parts.append("part_fts.tool = ?")
        params.append(tool)

    if keywords:
        # Join keywords with spaces — FTS5 treats spaces as implicit OR
        # (matching any keyword).  For AND semantics users can pass
        # explicit ``AND`` in the keyword string.
        # Quote each keyword individually to handle special characters
        # (hyphens in UUIDs, etc.) that FTS5 might interpret as boolean
        # operators, while still allowing space-separated multi-term OR.
        quoted = " ".join(f'"{kw}"' for kw in keywords)
        parts.append("part_fts MATCH ?")
        params.append(quoted)

    if not parts:
        return "WHERE 1=0", []

    return "WHERE " + " AND ".join(parts), params


def fetch_recall_stats(
    directory: Optional[str],
    keywords: list[str],
    agent: Optional[str] = None,
    server: Optional[str] = None,
    title: Optional[str] = None,
    tool: Optional[str] = None,
) -> tuple[int, int]:
    """Return ``(total_matches, total_sessions)`` for the given search.

    ``total_matches`` = number of part rows matching the keywords.
    ``total_sessions`` = number of distinct sessions containing matches.

    All queries run against the FTS table only — no JOIN to the primary
    database is needed because all metadata is stored in the FTS index.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    where, params = _build_fts_where_clause(directory, keywords, agent, server, title, tool)

    row = _query_fts(
        f"""
        SELECT COUNT(DISTINCT part_fts.session_id)
        FROM part_fts
        {where}
        """,
        params,
        fetch_all=False,
    )
    total_sessions = row[0] if row else 0

    row = _query_fts(
        f"""
        SELECT COUNT(*)
        FROM part_fts
        {where}
        """,
        params,
        fetch_all=False,
    )
    total_matches = row[0] if row else 0

    return total_matches, total_sessions


def fetch_recall_results(
    directory: Optional[str],
    keywords: list[str],
    limit: int,
    offset: int,
    agent: Optional[str] = None,
    server: Optional[str] = None,
    title: Optional[str] = None,
    tool: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return a page of search-match results.

    Uses the FTS5 ``part_fts`` index so results are ranked by BM25
    relevance (lower score = more relevant) rather than by recency.

    All metadata (session title, directory, agent, server, timestamps,
    part_id) is stored directly in the FTS table — no JOIN to the primary
    database is needed.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    where, params = _build_fts_where_clause(directory, keywords, agent, server, title, tool)

    # Build exact-phrase text for `instr()` phrase boosting.
    # Only apply phrase boosting when 2+ keywords are provided —
    # a phrase requires at least two words to be meaningful.
    phrase_text: Optional[str] = None
    if len(keywords) >= 2:
        phrase_text = ' '.join(keywords)

    # Conditionally build ORDER BY with phrase bonus.
    # The CASE subtracts 3.0 from bm25 when the row also contains
    # the exact phrase, making it more negative = higher rank.
    # We use `instr()` (not FTS5 MATCH) because SQLite does not
    # allow the MATCH operator inside CASE expressions.
    # Phrase param must be placed *before* LIMIT/OFFSET in the
    # params tuple because it is used as a ? placeholder in ORDER BY.
    if phrase_text:
        order_by = (
            "ORDER BY bm25(part_fts) "
            "- CASE WHEN instr(part_fts.text, ?) > 0 THEN 3.0 "
            "ELSE 0.0 END ASC"
        )
        query_params: tuple = (*params, phrase_text, limit, offset)
    else:
        order_by = "ORDER BY bm25(part_fts) ASC"
        query_params = (*params, limit, offset)

    rows = _query_fts(
        f"""
        SELECT
            part_fts.session_id,
            part_fts.text    AS fts_text,
            part_fts.session_slug      AS session_slug,
            part_fts.session_title     AS session_title,
            part_fts.server            AS server,
            part_fts.session_time_created   AS session_time_created,
            part_fts.directory         AS session_directory,
            part_fts.agent,
            part_fts.part_id           AS part_id,
            part_fts.part_type         AS part_type,
            part_fts.part_time_created AS part_time_created,
            part_fts.tool              AS tool,
            part_fts.title             AS title,
            ROUND(bm25(part_fts), 2)   AS bm25_score
        FROM part_fts
        {where}
        {order_by}
        LIMIT ? OFFSET ?
        """,
        query_params,
    )

    results: list[dict] = []
    for row in rows:
        # The FTS table now stores all metadata including part_type.
        text = row["fts_text"] or ""
        part_type = row["part_type"] or "unknown"

        # Build snippet highlighting the first matched keyword.
        snippet = ""
        for kw in keywords:
            if kw.lower() in text.lower():
                snippet = highlight_match(text, kw)
                break
        if not snippet:
            snippet = text[:200]

        results.append(
            {
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "session_slug": row["session_slug"],
                "server": row["server"],
                "directory": row["session_directory"],
                "agent": row["agent"],
                "part_id": row["part_id"],
                "time_created": format_timestamp(row["part_time_created"]),
                "part_type": part_type,
                "tool": row["tool"],
                "title": row["title"],
                "snippet": snippet or text[:200],
                "full_text_length": len(text),
                "bm25_score": row["bm25_score"],
            }
        )
    return results
