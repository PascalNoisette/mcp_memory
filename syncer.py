#!/usr/bin/env python3
"""
Background FTS synchronizer for the Kilo Code MCP Memory Server.

Maintains a **shared** SQLite database (``FTS_DB_PATH``) that contains
the ``part_fts`` FTS5 index.  A background thread polls the primary
database for new ``part`` rows and inserts them into the FTS index
using per-server **high-water marks**.

No triggers are needed — the syncer reads the primary database directly.

## Sync strategy

Watermarks are stored per-server in a ``part_fts_meta`` table:

| Key prefix             | Description                                   |
|------------------------|-----------------------------------------------|
| ``last_sync_ts:<name>`` | Max ``time_created`` of synced parts (ms)    |
| ``last_sync_rowid:<name>`` | Max ``rowid`` of synced parts for this server |

This allows multiple MCP instances (e.g. ``prod``, ``dev``) to share
the same FTS database while tracking independent sync progress.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from database import get_db, get_fts_db, get_server_name, resolve_fts_db_path

logger = logging.getLogger(__name__)

# ── Module-level state ───────────────────────────────────────────────────────

_syncer_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_lock = threading.Lock()


def _get_watermarks(fts_conn: sqlite3.Connection, server: str) -> tuple[int, int]:
    """Return (last_sync_ts, last_sync_rowid) for a specific server.

    Falls back to (0, 0) if no watermark has been recorded yet.
    """
    cur = fts_conn.cursor()
    try:
        cur.execute(
            "SELECT key, value FROM part_fts_meta "
            "WHERE key IN (?, ?)",
            (f"last_sync_ts:{server}", f"last_sync_rowid:{server}"),
        )
    except sqlite3.OperationalError:
        return 0, 0
    ts = 0
    rid = 0
    for key, value in cur.fetchall():
        if key == f"last_sync_ts:{server}":
            ts = int(value) if value else 0
        elif key == f"last_sync_rowid:{server}":
            rid = int(value) if value else 0
    return ts, rid


def _set_watermarks(
    fts_conn: sqlite3.Connection, server: str, ts: int, rowid: int
) -> None:
    """Update the high-water mark in the FTS DB for a specific server."""
    fts_conn.execute(
        "INSERT OR REPLACE INTO part_fts_meta (key, value) VALUES (?, ?)",
        (f"last_sync_ts:{server}", str(ts)),
    )
    fts_conn.execute(
        "INSERT OR REPLACE INTO part_fts_meta (key, value) VALUES (?, ?)",
        (f"last_sync_rowid:{server}", str(rowid)),
    )


def _incremental_sync(server: str) -> int:
    """Incremental sync: find new part rows for this server and insert into FTS.

    Args:
        server: This MCP instance's server name (used for watermarks and tagging).

    Returns:
        Number of rows synced in this pass.
    """
    fts_conn = get_fts_db()
    primary_conn = get_db()

    last_ts, last_rid = _get_watermarks(fts_conn, server)

    # Fetch new rows ordered by rowid (ensures consistent processing).
    cur = primary_conn.cursor()
    cur.execute(
        """
      SELECT sm.rowid AS data_rowid, sm.data, sm.session_id, sm.type,
              sm.id              AS message_id,
              sm.id              AS part_id,
              sv.directory,
              sv.agent,
              sv.title           AS session_title,
              sv.slug            AS session_slug,
              sv.time_created    AS session_time_created,
              sm.time_created    AS part_time_created,
              NULL               AS part_tool,
              NULL               AS part_title
        FROM session_message sm
        JOIN session_v2 sv ON sm.session_id = sv.id
        WHERE json_valid(sm.data) = 1
          AND sm.type IN ('user', 'assistant')
          AND (
              json_extract(sm.data, '$.text') IS NOT NULL
              OR json_extract(sm.data, '$.content') IS NOT NULL
              OR json_extract(sm.data, '$.value') IS NOT NULL
          )
          AND (sm.rowid > ? OR sm.time_created > ?)
        ORDER BY sm.id ASC
        """,
        (last_rid, last_ts),
    )
    new_rows = cur.fetchall()

    if not new_rows:
        return 0

    from formatters import parse_part_data

    count = 0
    max_rid = last_rid
    max_ts = last_ts

    for row in new_rows:
        data = row["data"] or "{}"
        session_id = row["session_id"]
        message_id = row["message_id"]
        part_id = row["part_id"]
        directory = row["directory"]
        agent = row["agent"]
        session_title = row["session_title"] or ""
        session_slug = row["session_slug"] or ""
        session_time_created = row["session_time_created"]
        part_time_created = row["part_time_created"]

        parsed = parse_part_data(data)
        text = parsed.get("text", "")
        # The ``part_type`` lives in the SQL column, not in the JSON data.
        part_type = row["type"] if row["type"] else "unknown"

        fts_conn.execute(
            """INSERT INTO part_fts
               (data_rowid, text, session_id, message_id, part_id, server,
                part_type, directory, agent, session_title, session_slug,
                session_time_created, part_time_created, tool, title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["data_rowid"], text, session_id, message_id, part_id, server,
             part_type, directory, agent, session_title, session_slug,
             session_time_created, part_time_created,
             row["part_tool"], row["part_title"]),
        )
        count += 1

        if str(row["data_rowid"]) > str(max_rid):
            max_rid = row["data_rowid"]

    # Update max timestamp for the highest rowid
    cur.execute(
        "SELECT MAX(time_created) FROM session_message WHERE rowid <= ?",
        (max_rid,),
    )
    max_ts = cur.fetchone()[0] or last_ts

    _set_watermarks(fts_conn, server, max_ts, max_rid)
    fts_conn.commit()

    logger.debug("Incremental FTS sync [%s]: %d rows.", server, count)
    return count


def _fts_syncer_loop(server: str, interval: int) -> None:
    """Background loop: periodically sync new part rows from primary to FTS.

    Args:
        server: This MCP instance's server name.
        interval: Seconds between sync cycles.
    """
    logger.info("FTS syncer thread started [%s, interval=%ds].", server, interval)

    while not _stop_event.is_set():
        try:
            _incremental_sync(server)
        except Exception as exc:
            logger.warning("FTS sync error [%s]: %s", server, exc)

        # Sleep in small increments so we can respond to stop quickly
        for _ in range(interval * 10):
            if _stop_event.is_set():
                break
            time.sleep(0.1)


def start_fts_syncer(
    fts_db_path: str | None = None,
    interval: int = 30,
    is_readonly: bool = False,
) -> None:
    """Start the background FTS sync thread.

    Args:
        fts_db_path: Path to the FTS database (ignored if ``FTS_DB_PATH`` env var is set).
        interval: Seconds between sync cycles (default 30).
        is_readonly: If True, skip syncer startup.
    """
    global _syncer_thread

    if is_readonly:
        logger.info("FTS syncer skipped — read-only mode.")
        return

    server = get_server_name()

    with _lock:
        if _syncer_thread is not None and _syncer_thread.is_alive():
            logger.debug("FTS syncer already running.")
            return

        _stop_event.clear()
        _syncer_thread = threading.Thread(
            target=_fts_syncer_loop,
            args=(server, interval),
            name="fts-syncer",
            daemon=True,
        )
        _syncer_thread.start()
        logger.info("FTS syncer thread started [%s].", server)


def stop_fts_syncer(timeout: float = 5.0) -> None:
    """Signal the syncer thread to stop and wait for it to finish.

    Args:
        timeout: Maximum seconds to wait for the thread to exit.
    """
    global _syncer_thread

    _stop_event.set()

    with _lock:
        thread = _syncer_thread
        _syncer_thread = None

    if thread is None:
        return

    logger.info("Waiting for FTS syncer thread to stop...")
    thread.join(timeout=timeout)

    if thread.is_alive():
        logger.warning("FTS syncer thread did not stop within %ds.", timeout)
    else:
        logger.info("FTS syncer thread stopped.")


def fts_sync_status(server: str | None = None) -> Optional[dict[str, Any]]:
    """Return sync status from the FTS DB.

    Args:
        server: Server name to get watermarks for.
                If None, uses ``SERVER_NAME`` env var.

    Returns:
        Dict with ``last_sync_ts``, ``last_sync_rowid``, ``total_indexed``,
        or ``None`` if the FTS DB is not initialized.
    """
    fts_db_path = resolve_fts_db_path()

    if not server:
        server = get_server_name()

    if not os.path.exists(fts_db_path):
        return None

    conn = sqlite3.connect(fts_db_path)
    try:
        cur = conn.cursor()

        # Get watermark for this server
        cur.execute(
            "SELECT key, value FROM part_fts_meta WHERE key IN (?, ?)",
            (f"last_sync_ts:{server}", f"last_sync_rowid:{server}"),
        )
        status: dict[str, Any] = {}
        for key, value in cur.fetchall():
            status[key] = value

        # Get total indexed
        cur.execute("SELECT COUNT(*) FROM part_fts")
        status["total_indexed"] = cur.fetchone()[0]

        return status
    finally:
        conn.close()
