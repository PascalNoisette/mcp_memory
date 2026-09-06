#!/usr/bin/env python3
"""
Business-logic / query layer for the Kilo Code MCP Memory Server.

Each function accepts validated parameters and returns structured Python
data (lists of dicts).  Formatting into Markdown or JSON is handled by
*formatters.py*.
"""

from __future__ import annotations

from typing import Any, Optional

from database import get_db, is_fts_available
from constants import HIGHLIGHT_CONTEXT_CHARS, TEXT_TRUNCATION_LIMIT
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
            "Please reindex the database — the full-text search table "
            "(part_fts) is missing. If the database file is writable, FTS "
            "will be recreated automatically on the next server restart. "
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

    # 1. Total count (snapshot at transaction start)
    cur.execute(
        "SELECT COUNT(*) as cnt FROM part WHERE session_id = ?",
        (session_id,),
    )
    total = cur.fetchone()["cnt"]

    # 2. Paginated messages (same snapshot — no new inserts visible)
    cur.execute(
        """
        SELECT id, message_id, time_created, data
        FROM part
        WHERE session_id = ?
        ORDER BY time_created ASC
        LIMIT ? OFFSET ?
        """,
        (session_id, page_size, offset),
    )
    rows = cur.fetchall()

    messages: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=offset):
        parsed = parse_part_data(row["data"] or "{}")
        text = sanitize_text(parsed.get("text", ""))
        messages.append(
            {
                "index": idx,
                "part_id": row["id"],
                "message_id": row["message_id"],
                "time_created": format_timestamp(row["time_created"]),
                "type": parsed.get("type", "unknown"),
                "text": text[:TEXT_TRUNCATION_LIMIT] if len(text) > TEXT_TRUNCATION_LIMIT else text,
                "full_text_length": len(text),
                "raw_data": row["data"],
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
) -> tuple[str, list]:
    """Build a parameterised WHERE clause for the FTS5 index.

    Uses ``part_fts MATCH`` (full-text search) instead of ``LIKE`` on
    the raw JSON ``part.data`` column.  This eliminates JSON noise,
    provides BM25 relevance ranking, and is dramatically faster.

    Returns ``(clause_sql, params)`` where *clause_sql* starts with ``WHERE``
    (or is empty when the caller prepends nothing).
    """
    parts: list[str] = []
    params: list[Any] = []

    if directory:
        parts.append("s.directory = ?")
        params.append(directory)

    if agent:
        parts.append("s.agent = ?")
        params.append(agent)

    if keywords:
        # Join keywords with spaces — FTS5 treats spaces as implicit OR
        # (matching any keyword).  For AND semantics users can pass
        # explicit ``AND`` in the keyword string.
        # Note: FTS5 MATCH does not support table aliases, so we must
        # use the real table name ``part_fts`` here.
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
) -> tuple[int, int]:
    """Return ``(total_matches, total_sessions)`` for the given search.

    ``total_matches`` = number of part rows matching the keywords.
    ``total_sessions`` = number of distinct sessions containing matches.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    where, params = _build_fts_where_clause(directory, keywords, agent)

    row = _query(
        f"""
        SELECT COUNT(DISTINCT part_fts.session_id)
        FROM part_fts
        JOIN session s ON part_fts.session_id = s.id
        {where}
        """,
        params,
        fetch_all=False,
    )
    total_sessions = row[0] if row else 0

    row = _query(
        f"""
        SELECT COUNT(*)
        FROM part_fts
        JOIN session s ON part_fts.session_id = s.id
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
) -> list[dict[str, Any]]:
    """Return a page of search-match results.

    Uses the FTS5 ``part_fts`` index so results are ranked by BM25
    relevance (lower score = more relevant) rather than by recency.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    where, params = _build_fts_where_clause(directory, keywords, agent)

    rows = _query(
        f"""
        SELECT
            p.id             AS part_id,
            p.data,
            part_fts.session_id,
            part_fts.message_id,
            p.time_created   AS part_time_created,
            part_fts.text    AS fts_text,
            s.slug           AS session_slug,
            s.title          AS session_title,
            s.time_created   AS session_time_created,
            s.directory      AS session_directory,
            s.agent,
            s.model,
            ROUND(bm25(part_fts), 2) AS bm25_score
        FROM part_fts
        JOIN part p ON part_fts.rowid = p.rowid
        JOIN session s ON part_fts.session_id = s.id
        {where}
        ORDER BY bm25(part_fts) ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )

    results: list[dict] = []
    for row in rows:
        # Parse the actual JSON data from the part table to extract type
        # (the FTS index only stores plain text, not structured JSON).
        parsed = parse_part_data(row["data"] or "{}")
        text = sanitize_text(parsed.get("text", "") or "")
        part_type = parsed.get("type", "unknown")

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
                "directory": row["session_directory"],
                "agent": row["agent"],
                "part_id": row["part_id"],
                "message_id": row["message_id"],
                "time_created": format_timestamp(row["part_time_created"]),
                "part_type": part_type,
                "snippet": snippet or text[:200],
                "full_text_length": len(text),
                "bm25_score": row["bm25_score"],
            }
        )
    return results
