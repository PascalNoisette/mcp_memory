"""
Output formatters for the MCP tool responses.

Each ``format_*`` function takes structured Python data and returns either a
Markdown string or a JSON string, depending on the requested
``ResponseFormat``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from models import ResponseFormat

from constants import (
    HIGHLIGHT_CONTEXT_CHARS,
    HIGHLIGHT_PREVIEW_CHARS,
)


# ---------------------------------------------------------------------------
# Binary / control-character sanitisation
# ---------------------------------------------------------------------------

_ALLOWED_CONTROL = frozenset("\n\t\r")  # newline, tab, carriage return


def sanitize_text(text: str) -> str:
    """Remove binary and control characters from *text*.

    Keeps printable Unicode characters and the three common whitespace
    characters ``\\n``, ``\\t``, ``\\r``.  All other bytes (including null
    ``\\x00`` and other C0/C1 control codes) are removed.

    This guards against corrupted ``part.data`` content that can break
    LLM loops or produce invalid JSON output.
    """
    return "".join(
        ch for ch in text if ch.isprintable() or ch in _ALLOWED_CONTROL
    )


def format_timestamp(ts: Optional[int | str], default: str = "unknown") -> str:
    """Convert a millisecond epoch timestamp to a human-readable string.

    If *ts* is already a string (e.g. from a prior formatting step), it is
    returned unchanged so that double-formatting is safe.
    """
    if ts is None:
        return default
    if isinstance(ts, str):
        return ts
    try:
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, OverflowError, ValueError):
        return default


def _extract_text_from_content_array(content: list[dict[str, Any]]) -> str:
    """Extract text from a ``content`` array (assistant messages).

    Assistant messages store their content in a ``content`` array with
    parts of different types (``"text"``, ``"reasoning"``, ``"tool"``,
    etc.).  This function extracts the ``text`` from each part,
    filtering out tool-call parts (which don't have meaningful text)
    and concatenating the rest.

    Returns:
        Sanitised text, or empty string if no extractable text is found.
    """
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        text = part.get("text", "")
        # Skip tool-call parts (their ``text`` is metadata, not content).
        if part_type == "tool":
            continue
        if isinstance(text, str) and text.strip():
            parts.append(text)
    full = "\n".join(parts)
    return sanitize_text(full) if full else ""


def parse_part_data(raw_data: str) -> dict[str, Any]:
    """Parse the JSON data field from a session_message row.

    Returns a dict with ``text`` key.  On parse failure a safe
    fallback dict is returned so callers never crash.
    Binary / control characters are stripped from the text value.

    **Assistant messages** have their text stored in a ``content``
    array (``[{"type": "text", "text": "..."}, ...]``).  When a
    top-level ``text`` key is absent, the function falls back to
    extracting text from this array.
    """
    try:
        parsed = json.loads(raw_data)
        if isinstance(parsed, dict):
            # --- User-like message: flat ``text`` field ---
            if "text" in parsed and isinstance(parsed["text"], str):
                parsed["text"] = sanitize_text(parsed["text"])
            # --- Assistant-like message: ``content`` array ---
            elif "content" in parsed and isinstance(parsed["content"], list):
                parsed["text"] = _extract_text_from_content_array(
                    parsed["content"]
                )
            return parsed
        return {"text": sanitize_text(str(parsed))}
    except (json.JSONDecodeError, TypeError):
        return {
            "text": sanitize_text(raw_data),
        }


def highlight_match(text: str, keyword: str) -> str:
    """Return a snippet with the keyword highlighted using ** markers."""
    low_text = text.lower()
    low_kw = keyword.lower()
    idx = low_text.find(low_kw)
    if idx == -1:
        return text[:HIGHLIGHT_PREVIEW_CHARS]

    start = max(0, idx - HIGHLIGHT_CONTEXT_CHARS)
    end = min(len(text), idx + len(keyword) + HIGHLIGHT_CONTEXT_CHARS)
    snippet = text[start:end]
    matched_text = text[idx : idx + len(keyword)]
    highlighted = snippet.replace(matched_text, f"**{keyword}**", 1)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{highlighted}{suffix}"


# ── Project formatter ────────────────────────────────────────────────────────


def format_projects(
    projects: list[dict[str, Any]], fmt: ResponseFormat
) -> str:
    """Format a list of project (directory) dicts for display.

    Each project now represents a directory path derived from sessions.
    """
    if fmt == ResponseFormat.MARKDOWN:
        if not projects:
            return "No projects found in the database."

        lines = ["# Projects (by Directory)", "", f"Found **{len(projects)}** project(s):", ""]
        for p in projects:
            lines.append(f"## {p['directory']}")
            lines.append(f"- **Sessions**: {p['session_count']}")
            lines.append(f"- **Latest Session**: {p['latest_session']}")
            lines.append("")
        return "\n".join(lines)

    return json.dumps({"projects": projects}, indent=2)


# ── Session formatter ────────────────────────────────────────────────────────


def format_sessions(
    directory: str,
    total: int,
    sessions: list[dict[str, Any]],
    offset: int,
    fmt: ResponseFormat,
) -> str:
    """Format a list of session dicts for display."""
    if fmt == ResponseFormat.MARKDOWN:
        if not sessions:
            return f"No sessions found for directory `{directory}`."

        lines = [
            f"# Sessions for `{directory}`",
            "",
            f"**Total**: {total} sessions | **Showing**: {len(sessions)} (offset {offset})",
            "",
        ]
        for s in sessions:
            title_display = s["title"] or s["slug"] or "(untitled)"
            lines.append(f"## {title_display}")
            lines.append(f"- **ID**: `{s['id']}`")
            if s["slug"]:
                lines.append(f"- **Slug**: `{s['slug']}`")
            if s["agent"]:
                lines.append(f"- **Agent**: {s['agent']}")
            if s["model"]:
                lines.append(f"- **Model**: {s['model']}")
            lines.append(f"- **Created**: {s['time_created']}")
            lines.append(f"- **Tokens**: {s['tokens_input']} in / {s['tokens_output']} out")
            if s["cost"] > 0:
                lines.append(f"- **Cost**: ${s['cost']:.6f}")
            lines.append("")
        return "\n".join(lines)

    return json.dumps(
        {
            "directory": directory,
            "total": total,
            "count": len(sessions),
            "offset": offset,
            "sessions": sessions,
        },
        indent=2,
    )


# ── Messages formatter ───────────────────────────────────────────────────────


def format_messages(
    session_id: str,
    total: int,
    messages: list[dict[str, Any]],
    offset: int,
    page_size: int,
    fmt: ResponseFormat,
) -> str:
    """Format a list of message/part dicts for display."""
    if fmt == ResponseFormat.MARKDOWN:
        if not messages:
            return f"No messages found for session `{session_id}`."

        lines = [
            f"# Messages for Session `{session_id}`",
            "",
            f"**Total**: {total} messages | **Showing**: {len(messages)} (offset {offset}, page_size {page_size})",
            "",
        ]
        for msg in messages:
            type_label = msg["type"].upper().replace("_", " ")
            text_preview = msg["text"] if msg["text"] else "(empty)"

            lines.append("---")
            lines.append(f"- **Part ID**: `{msg['part_id']}`")
            lines.append(f"- **Type**: `{type_label}`")
            lines.append(f"- **Time**: {msg['time_created']}")
            if msg.get("state_title"):
                lines.append(f"- **State Title**: `{msg['state_title']}`")
            if msg.get("state_tool"):
                lines.append(f"- **State Tool**: `{msg['state_tool']}`")
            lines.append(f"- **Content**:")
            lines.append(f"```\n{text_preview}\n```")
            lines.append("")
        return "\n".join(lines)

    # JSON — omit raw_data for cleanliness
    output_messages = [{k: v for k, v in msg.items() if k != "raw_data"} for msg in messages]
    return json.dumps(
        {
            "session_id": session_id,
            "total": total,
            "count": len(messages),
            "offset": offset,
            "page_size": page_size,
            "has_more": (offset + len(messages)) < total,
            "next_offset": offset + len(messages) if (offset + len(messages)) < total else None,
            "messages": output_messages,
        },
        indent=2,
    )


# ── Recall formatter ─────────────────────────────────────────────────────────


def format_recall(
    keywords: list[str],
    directory: Optional[str],
    agent: Optional[str],
    total_matches: int,
    total_sessions: int,
    matches: list[dict[str, Any]],
    offset: int,
    fmt: ResponseFormat,
    limit: int = 20,
) -> str:
    """Format keyword recall / search results for display."""
    if fmt == ResponseFormat.MARKDOWN:
        if not matches:
            keyword_str = " ".join(f"`{kw}`" for kw in keywords)
            scope_parts = []
            if directory:
                scope_parts.append(f"`{directory}`")
            if agent:
                scope_parts.append(f"agent `{agent}`")
            if scope_parts:
                scope = f" in {', '.join(scope_parts)}"
            else:
                scope = ""
            return (
                f"No matches found for keywords {keyword_str}{scope}."
                f"\n\n💡 Tip: Try fewer or different keywords, "
                f"or omit the filters to search more broadly."
            )

        lines = [
            f"# Recall: '{' '.join(keywords)}'",
            "",
            f"**{total_matches}** matching message(s) across **{total_sessions}** session(s) | "
            f"Showing {len(matches)} (offset {offset})",
            "",
            "> ⚠️ The first result may be your own current conversation.",
            "",
        ]
        if directory:
            lines.append(f"Scope: `{directory}`")
        if agent:
            lines.append(f"Agent: `{agent}`")
        if directory or agent:
            lines.append("")

        seen_sessions: set[str] = set()
        for m in matches:
            session_key = m["session_id"]
            title = m["session_title"] or m["session_slug"] or "(untitled)"

            if session_key not in seen_sessions:
                seen_sessions.add(session_key)
                server_info = f" · Server: {m['server']}" if m.get("server") else ""
                dir_info = f" (`{m['directory']}`)" if m.get("directory") else ""
                agent_info = f" · Agent: {m['agent']}" if m.get("agent") else ""
                lines.append(f"## 📂 {title} (`{session_key}`){server_info}{dir_info}{agent_info}")
                lines.append("")

            type_label = m["part_type"].upper().replace("_", " ")
            ts_str = format_timestamp(m["time_created"])
            lines.append(
                f"### 💬 Message `{m['part_id']}` "
                f"({type_label} · {ts_str})"
            )
            if m.get("tool"):
                lines.append(f"- **Tool**: `{m['tool']}`")
            if m.get("title"):
                lines.append(f"- **Title**: `{m['title']}`")
            if m["snippet"]:
                lines.append(f"```\n{m['snippet']}\n```")
            lines.append("")
        return "\n".join(lines)

    return json.dumps(
        {
            "query": {
                "keywords": keywords,
                "directory": directory,
                "agent": agent,
                "limit": limit,
                "offset": offset,
            },
            "total_matches": total_matches,
            "total_sessions": total_sessions,
            "count": len(matches),
            "has_more": (offset + len(matches)) < total_matches,
            "next_offset": offset + len(matches) if (offset + len(matches)) < total_matches else None,
            "matches": matches,
        },
        indent=2,
    )
