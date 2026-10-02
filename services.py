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

import json
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

    Queries ``SELECT directory, COUNT(*) FROM session_v2 GROUP BY directory``
    to derive projects from actual session data instead of the project table.
    """
    rows = _query(
        """
        SELECT s.directory, COUNT(*) AS session_count,
               MAX(s.time_created) AS latest_session_time
        FROM session_v2 s
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
        "SELECT COUNT(*) as cnt FROM session_v2 WHERE directory = ?",
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
        FROM session_v2
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


def _expand_assistant_parts(
    parent_id: str,
    content: list[dict[str, Any]],
    time_created_ms: int,
) -> list[dict[str, Any]]:
    """Expand an assistant message with multiple content parts into
    separate message entries (one per part).

    Each non-tool part (``reasoning``, ``text``) is combined into a single
    ``assistant`` message.  Each ``tool`` part becomes its own ``tool``
    message entry with the tool name, input and output in the ``text`` field.

    Args:
        parent_id: The original session_message id (used as part_id for
                   the assistant entry; each tool gets a synthetic id).
        content: The ``content`` array from the assistant message JSON.
        time_created_ms: Timestamp in milliseconds (used for the assistant
                         entry; tool entries inherit this timestamp).

    Returns:
        List of message dicts, one per content part.
    """
    messages: list[dict[str, Any]] = []
    assistant_parts: list[str] = []

    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        part_text = part.get("text", "")

        if part_type == "tool":
            # Build a synthetic tool message entry.
            # Tool data structure: {"type": "tool", "id": "...", "name": "...",
            #   "executed": bool, "state": {"status": "...", "input": {...}, "content": [...]}}
            name = part.get("name", "unknown")
            tool_id = part.get("id", "")
            executed = part.get("executed", False)
            state = part.get("state", {})
            status = state.get("status", "unknown")
            tool_input = state.get("input", {})
            tool_content = state.get("content", [])

            # Build the text: tool name, input, and output.
            exec_status = "executed" if executed else "not executed"
            lines: list[str] = [
                f"Tool: {name}",
                f"Tool ID: {tool_id}",
                f"Executed: {exec_status}",
                f"State status: {status}",
            ]
            if tool_input:
                inp_str = json.dumps(tool_input, ensure_ascii=False) if isinstance(tool_input, dict) else str(tool_input)
                lines.append(f"Input: {inp_str[:500]}")
            if tool_content:
                # Collect text content from state.content parts.
                output_parts: list[str] = []
                for tc in tool_content:
                    if isinstance(tc, dict) and tc.get("type") == "text":
                        output_parts.append(tc.get("text", ""))
                if output_parts:
                    out_str = "\n".join(output_parts)
                    lines.append(f"Output: {out_str[:500]}")
            tool_text = "\n".join(lines)
            tool_text = sanitize_text(tool_text)

            messages.append(
                {
                    "part_id": tool_id if tool_id else f"{parent_id}_tool",
                    "time_created": format_timestamp(time_created_ms),
                    "type": "tool",
                    "text": tool_text,
                    "full_text_length": len(tool_text),
                    "raw_data": json.dumps(part),
                    "state_title": None,
                    "state_tool": name,
                }
            )
        else:
            # Non-tool part: collect for the assistant entry.
            if isinstance(part_text, str) and part_text.strip():
                assistant_parts.append(part_text)

    # Add the combined assistant entry.
    if assistant_parts:
        messages.insert(
            0,
            {
                "part_id": parent_id,
                "time_created": format_timestamp(time_created_ms),
                "type": "assistant",
                "text": sanitize_text("\n\n".join(assistant_parts)),
                "full_text_length": len(sanitize_text("\n\n".join(assistant_parts))),
                "raw_data": json.dumps({"content": content}),
                "state_title": None,
                "state_tool": None,
            },
        )

    return messages


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

    # 1. Total count (snapshot at transaction start) — filter to user/assistant
    cur.execute(
        """
        SELECT COUNT(*) as cnt FROM session_message
        WHERE session_id = ?
          AND json_valid(data) = 1
          AND type IN ('user', 'assistant')
        """,
        (session_id,),
    )
    total = cur.fetchone()["cnt"]

    # 2. Paginated messages (same snapshot — no new inserts visible)
    cur.execute(
        """
        SELECT id, time_created, data,
               type,
               NULL AS state_title,
               NULL AS state_tool
        FROM session_message
        WHERE session_id = ?
          AND json_valid(data) = 1
          AND type IN ('user', 'assistant')
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
        # The ``type`` lives in the SQL column, not in the JSON data.
        msg_type = row["type"] if row["type"] else "unknown"

        # Expand assistant messages that contain tool parts into
        # separate message entries (one per content part).
        raw_data = row["data"] or "{}"
        if msg_type == "assistant":
            try:
                data_obj = json.loads(raw_data)
                content = data_obj.get("content", [])
                if isinstance(content, list) and len(content) > 1:
                    # There are multiple content parts — expand them.
                    messages.extend(
                        _expand_assistant_parts(
                            row["id"],
                            content,
                            row["time_created"],
                        )
                    )
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

        messages.append(
            {
                "part_id": row["id"],
                "time_created": format_timestamp(row["time_created"]),
                "type": msg_type,
                "text": text,
                "full_text_length": len(text),
                "raw_data": raw_data,
                "state_title": row["state_title"],
                "state_tool": row["state_tool"],
            }
        )

    return total, messages


def fetch_message_count(session_id: str) -> int:
    """Return the total number of messages (parts) for a session."""
    rows = _query(
        "SELECT COUNT(*) as cnt FROM session_message WHERE session_id = ?",
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


def _search_with_retry(
    directory: Optional[str],
    keywords: list[str],
    agent: Optional[str],
    server: Optional[str],
    title: Optional[str],
    tool: Optional[str],
    limit: int,
    offset: int,
    stats: bool = False,
    max_attempts: int = 1000,
) -> Any:
    """Search the FTS index with automatic keyword reduction on empty results.

    When the full keyword set returns zero matches, progressively drop the
    last keyword and retry (e.g. ``"a b c"`` → ``"a b"`` → ``"a"``).
    This mitigates BM25 over-specification: a query like
    ``"systemd service unit install daemon"`` may match nothing, but
    ``"systemd service"`` or even ``"systemd"`` alone may find results.

    The phrase bonus (``instr()`` boosting) is always computed from the
    **original** keyword set so that partial matches still benefit from
    it when the full phrase appears in the text.

    Args:
        directory, agent, server, title, tool: FTS filter parameters.
        keywords: Keyword list — reduced iteratively on empty results.
        limit: Max rows to return (ignored for stats mode).
        offset: Pagination offset.
        stats: If True, return ``(total_matches, total_sessions)``.
        max_attempts: Safety cap to avoid infinite loops.

    Returns:
        *stats=True* → ``(total_matches, total_sessions)``
        *stats=False* → list of result dicts
    """
    # Compute phrase text from the original keywords once (used by instr()
    # in the ORDER BY to boost exact-phrase matches regardless of which
    # reduced keyword set was tried).
    phrase_text: Optional[str] = (
        " ".join(keywords) if len(keywords) >= 2 else None
    )

    for attempt in range(max_attempts):
        reduced = keywords[: len(keywords) - attempt] if attempt > 0 else keywords
        if not reduced:
            # All keywords exhausted — fall back to a no-op (empty result).
            if stats:
                return 0, 0
            return []

        where, params = _build_fts_where_clause(
            directory, reduced, agent, server, title, tool
        )

        if stats:
            row = _query_fts(
                f"""
                SELECT COUNT(*) as matches,
                       COUNT(DISTINCT session_id) as sessions
                FROM part_fts
                {where}
                """,
                params,
                fetch_all=False,
            )
            if row:
                return row["matches"], row["sessions"]
            continue

        # Build ORDER BY — use phrase_text from the *original* keywords,
        # not from the reduced set.
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

        # If results found, build and return snippets immediately.
        if rows:
            results: list[dict] = []
            for row in rows:
                text = row["fts_text"] or ""
                part_type = row["part_type"] or "unknown"

                # Build snippet highlighting the first matched keyword.
                snippet = ""
                for kw in reduced:
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
                        "time_created": format_timestamp(
                            row["part_time_created"]
                        ),
                        "part_type": part_type,
                        "tool": row["tool"],
                        "title": row["title"],
                        "snippet": snippet or text[:200],
                        "full_text_length": len(text),
                        "bm25_score": row["bm25_score"],
                    }
                )
            return results

        # No results — next iteration will drop one more keyword.
        # (The for-loop handles this automatically.)

    # Should never reach here with max_attempts >= len(keywords),
    # but safeguard anyway.
    if stats:
        return 0, 0
    return []


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

    Automatically retries with fewer keywords when no matches are found,
    progressively dropping the last keyword until results appear or all
    keywords are exhausted.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    return _search_with_retry(
        directory, keywords, agent, server, title, tool, 0, 0, stats=True
    )


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

    Automatically retries with fewer keywords when no matches are found,
    progressively dropping the last keyword until results appear or all
    keywords are exhausted.

    Raises:
        FTSUnavailableError: If the full-text search table is not available.
    """
    _ensure_fts_available()
    return _search_with_retry(
        directory, keywords, agent, server, title, tool, limit, offset,
        stats=False,
    )
