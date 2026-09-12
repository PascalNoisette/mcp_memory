#!/usr/bin/env python3
"""
Database access layer for the Kilo Code MCP Memory Server.

Provides a thread-safe connection pool for the SQLite backend.  Connections
are created lazily per-thread and closed explicitly via ``close_all``.

## Architecture

Each MCP instance manages **one** primary opencode database (via
``DATABASE_PATH``) and writes to a **shared** FTS index (via
``FTS_DB_PATH``).  The ``SERVER_NAME`` env var identifies this instance
so that rows in the shared FTS index are tagged and can be filtered.

    ┌──────────────────────┐            ┌──────────────────────┐
    │  MCP instance (prod)  │            │  MCP instance (dev)   │
    │  SERVER_NAME=prod     │            │  SERVER_NAME=dev      │
    │  DB: prod.db:ro       │            │  DB: dev.db:ro        │
    │                       │            │                       │
    │  ┌────────┐           │            │  ┌────────┐           │
    │  │  part   │──────────┼────────────┤  │  part   │──────────┼─────┐
    │  └────────┘           │            │  └────────┘           │     │
    │  ┌────────┐           │            │  ┌────────┐           │     │
    │  │session │           │            │  │session │           │     │
    │  └────────┘           │            │  └────────┘           │     │
    └──────────────────────┘            └──────────────────────┘     │
                                                                      │
    Shared FTS Database (opencode-local.db_fts.db)                    │
    ┌──────────────────────────────────────────────────────────────┐  │
    │  part_fts  (FTS5)  ← includes a "server" column for tagging  │◄─┘
    │  part_fts_meta (per-server watermarks)                       │
    └──────────────────────────────────────────────────────────────┘

Environment variables:

    DATABASE_PATH  (required)    Primary opencode DB path.
                                 Optional :ro/:rw suffix.
                                 Example: "/home/user/.local/share/opencode/opencode-local.db:ro"

    SERVER_NAME    (required)    Tag for this instance.
                                 Example: "prod", "dev"

    FTS_DB_PATH    (optional)   Shared FTS5 index path.
                                 Defaults to "{DATABASE_PATH without mode}_fts.db".
                                 Example: "/home/user/.local/share/opencode/shared-fts.db"

"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)


# ── Environment resolution ───────────────────────────────────────────────────

def resolve_db_path() -> str:
    """Return the primary database path from ``DATABASE_PATH`` env var."""
    raw = os.environ.get(
        "DATABASE_PATH",
        os.path.expanduser("~/.local/share/opencode/opencode-local.db"),
    )
    return _parse_db_spec(raw)[0]


def resolve_db_mode() -> str:
    """Return the access mode (``""``, ``"ro"``, or ``"rw"``)."""
    raw = os.environ.get(
        "DATABASE_PATH",
        os.path.expanduser("~/.local/share/opencode/opencode-local.db"),
    )
    return _parse_db_spec(raw)[1]


def _parse_db_spec(raw: str) -> tuple[str, str]:
    """Extract (path, mode) from a raw DATABASE_PATH string.

    Supports an optional ``:ro`` or ``:rw`` suffix.
    """
    last_colon = raw.rfind(":")
    if last_colon != -1:
        path = raw[:last_colon]
        mode = raw[last_colon + 1:].strip().lower()
        if mode not in ("ro", "rw", ""):
            return raw, ""
        return path, mode
    return raw, ""


def get_server_name() -> str:
    """Return the ``SERVER_NAME`` env var.  Required — raises if missing."""
    name = os.environ.get("SERVER_NAME")
    if not name:
        raise EnvironmentError(
            "SERVER_NAME environment variable is not set. "
            "Each MCP instance must have a unique SERVER_NAME "
            "(e.g. 'prod', 'dev') to tag FTS rows."
        )
    return name


def resolve_fts_db_path() -> str:
    """Return the FTS database path.

    Uses ``FTS_DB_PATH`` if set, otherwise derives it from
    ``DATABASE_PATH`` by stripping the ``:mode`` suffix and appending
    ``_fts.db``.
    """
    fts = os.environ.get("FTS_DB_PATH")
    if fts:
        return fts

    db_path = resolve_db_path()
    mode = resolve_db_mode()
    if mode:
        db_path = db_path[: -len(mode) - 1]  # strip ":mode"

    if db_path.endswith(".db"):
        return db_path[:-3] + "_fts.db"
    return db_path + "_fts.db"


def is_db_readonly() -> bool:
    """Return ``True`` when the primary DB should be opened read-only."""
    return resolve_db_mode() == "ro"


# ── Module-level init flag and lock ──────────────────────────────────────────

_init_lock = threading.Lock()
_fts_initialized = threading.Event()
_fts_available = False


def _create_primary_schema(db_path: str) -> None:
    """Create the primary database schema (session + part tables).

    This is needed when the primary database file does not exist yet
    (e.g. fresh test runs).  Opens the database in read-write mode
    briefly to create the tables, then closes it.
    """
    # Ensure parent directory exists
    parent = os.path.dirname(db_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session (
                id              TEXT PRIMARY KEY,
                slug            TEXT NOT NULL,
                title           TEXT,
                directory       TEXT NOT NULL,
                agent           TEXT,
                model           TEXT,
                time_created    TEXT NOT NULL,
                time_updated    TEXT NOT NULL,
                tokens_input    INTEGER,
                tokens_output   INTEGER,
                tokens_reasoning INTEGER,
                cost            REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS part (
                id              TEXT PRIMARY KEY,
                message_id      TEXT NOT NULL,
                session_id      TEXT NOT NULL,
                time_created    TEXT NOT NULL,
                data            TEXT,
                FOREIGN KEY (session_id) REFERENCES session(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_part_session
            ON part (session_id)
        """)
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the database connection pool and FTS index.

    This function is **idempotent**.

    **Mode-aware**: In ``:ro`` mode the FTS index is disabled.  For all
    other modes the FTS index is available and a full resync from the
    primary database is performed at init time.  The ``syncer`` background
    thread handles incremental updates thereafter.
    """
    global _fts_available

    if _fts_initialized.is_set():
        return

    with _init_lock:
        if _fts_initialized.is_set():
            return

        get_server_name()  # validate env vars early

        db_path = resolve_db_path()

        # Create primary database schema if the file doesn't exist
        if not os.path.exists(db_path):
            _create_primary_schema(db_path)

        if is_db_readonly():
            logger.info(
                "FTS disabled — mode is 'ro'. "
                "Read tools work normally; recall_session is unavailable."
            )
            _fts_available = False
        else:
            server = get_server_name()
            logger.info(
                "FTS enabled — performing full resync for server '%s' "
                "(separate DB: %s).",
                server,
                resolve_fts_db_path(),
            )
            _do_fts_init(server)

        _fts_initialized.set()


# ── Primary database connection pool ─────────────────────────────────────────

_primary_connections: dict[int, sqlite3.Connection] = {}
_primary_lock = threading.Lock()


class _PrimaryConnectionPool:
    """Per-thread SQLite connection pool for the **primary** database.

    The primary database is always opened **read-only** (SQLite URI
    ``mode=ro``), regardless of the ``DATABASE_PATH`` mode suffix.
    """

    @classmethod
    def get(cls) -> sqlite3.Connection:
        """Return a thread-local connection to the primary database."""
        thread_id = threading.current_thread().ident
        if thread_id is None:
            raise RuntimeError("threading.current_thread().ident is None")

        with _primary_lock:
            if thread_id not in _primary_connections:
                db_path = resolve_db_path()

                # Ensure the parent directory exists before connecting
                parent = os.path.dirname(db_path)
                if parent:
                    try:
                        os.makedirs(parent, exist_ok=True)
                    except OSError:
                        pass

                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.row_factory = sqlite3.Row
                _primary_connections[thread_id] = conn
            return _primary_connections[thread_id]

    @classmethod
    def close_all(cls) -> None:
        """Close every tracked primary connection and clear the pool."""
        with _primary_lock:
            for conn in _primary_connections.values():
                try:
                    conn.close()
                except sqlite3.ProgrammingError:
                    pass
            _primary_connections.clear()


# ── FTS database connection ──────────────────────────────────────────────────

_fts_connections: dict[int, sqlite3.Connection] = {}
_fts_lock = threading.Lock()


def _ensure_fts_schema(conn: sqlite3.Connection) -> None:
    """Ensure the ``part_fts`` FTS5 table and ``part_fts_meta`` exist.

    Uses ``CREATE VIRTUAL TABLE IF NOT EXISTS`` so multiple MCP instances
    can safely target the same FTS database file — only the first one
    actually creates the table.

    If the table exists but is missing the ``server`` column (legacy schema),
    it is dropped and recreated with the correct schema.

    The ``server`` column is stored directly in the FTS5 index so recall
    queries can filter by server without a JOIN.
    """
    fts_cols = [
        "text",
        "session_id",
        "message_id",
        "part_id",
        "server",
        "part_type",
        "directory",
        "agent",
        "session_title",
        "session_slug",
        "session_time_created",
        "part_time_created",
        "tool",
        "title",
    ]

    # Check if the table exists and has the server column
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='part_fts'"
    ).fetchall()

    needs_recreate = False
    if tables:
        # Table exists — check for missing columns (legacy schema)
        cols = conn.execute("PRAGMA table_info(part_fts)").fetchall()
        col_names = {c[1] for c in cols}
        if not {"server", "tool", "title"}.issubset(col_names):
            conn.execute("DROP TABLE IF EXISTS part_fts")
            needs_recreate = True

    if needs_recreate or not tables:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS part_fts USING fts5(
                {', '.join(fts_cols)},
                tokenize='unicode61'
            )
            """
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS part_fts_meta (
            key       TEXT PRIMARY KEY,
            value     TEXT
        )
        """
    )


def _ensure_fts_tables_exist(fts_path: str) -> None:
    """Lightweight check: ensure the FTS tables exist in the file.

    Opens the file, runs ``CREATE TABLE IF NOT EXISTS``, and closes it
    immediately.  Used during startup to guarantee the schema exists
    before background sync starts.
    """
    parent = os.path.dirname(fts_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            return

    conn = sqlite3.connect(fts_path)
    try:
        _ensure_fts_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _do_fts_init(server: str) -> None:
    """Perform the initial FTS full resync for this server.

    Because multiple MCP instances share the same FTS database, we **do not**
    drop/recreate the table.  Instead we:

    1. Ensure the FTS table exists (``IF NOT EXISTS``).
    2. Delete any existing rows tagged with this server's name.
    3. Re-insert all rows from the primary database, tagged with this server.
    4. Reset this server's watermark to zero.

    Args:
        server: This MCP instance's server name.
    """
    global _fts_available

    fts_path = resolve_fts_db_path()
    db_path = resolve_db_path()

    # Ensure parent directory exists
    parent = os.path.dirname(fts_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            logger.warning("Could not create FTS DB directory: %s", parent)
            _fts_available = False
            return

    max_retries = 3
    retry_delay = 0.5

    for attempt in range(1, max_retries + 1):
        primary_conn = None
        fts_conn = None
        try:
            # Connect to primary (read-only)
            primary_conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True
            )
            primary_conn.execute("PRAGMA busy_timeout=10000")
            primary_conn.row_factory = sqlite3.Row

            # Connect to FTS
            fts_conn = sqlite3.connect(fts_path, check_same_thread=False)
            fts_conn.execute("PRAGMA journal_mode=WAL")
            _ensure_fts_schema(fts_conn)

            # 1. Delete this server's existing rows (partial cleanup)
            deleted = fts_conn.execute(
                "DELETE FROM part_fts WHERE server = ?", (server,)
            ).rowcount
            logger.info("FTS init [%s]: deleted %d stale rows.", server, deleted)

            # 2. Scan all rows from primary DB
            cur = primary_conn.cursor()
            cur.execute(
                """
                SELECT p.rowid, p.data, p.session_id, p.message_id,
                       p.id              AS part_id,
                       s.directory,
                       s.agent,
                       s.title           AS session_title,
                       s.slug            AS session_slug,
                       s.time_created    AS session_time_created,
                       p.time_created    AS part_time_created,
                       json_extract(p.data, '$.tool') AS part_tool,
                       json_extract(p.data, '$.state.title') AS part_title
                FROM part p
                JOIN session s ON p.session_id = s.id
                WHERE json_valid(p.data) = 1
                  AND json_extract(p.data, '$.type') NOT IN ('step-start', 'step-finish')
                  AND (
                      json_extract(p.data, '$.text') IS NOT NULL
                      OR json_extract(p.data, '$.content') IS NOT NULL
                      OR json_extract(p.data, '$.value') IS NOT NULL
                      OR json_extract(p.data, '$.state.output') IS NOT NULL
                  )
                ORDER BY p.rowid ASC
                """
            )
            rows = cur.fetchall()
            count = 0

            # 3. Insert all rows tagged with this server
            for row in rows:
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

                from formatters import parse_part_data
                parsed = parse_part_data(data)
                text = parsed.get("text", "")
                part_type = parsed.get("type", "unknown")

                fts_conn.execute(
                    """INSERT INTO part_fts
                       (rowid, text, session_id, message_id, part_id, server,
                        part_type, directory, agent, session_title, session_slug,
                        session_time_created, part_time_created, tool, title)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["rowid"], text, session_id, message_id, part_id, server,
                     part_type, directory, agent, session_title, session_slug,
                     session_time_created, part_time_created,
                     row["part_tool"], row["part_title"]),
                )
                count += 1

            # 4. Reset watermark for this server
            fts_conn.execute(
                "INSERT OR REPLACE INTO part_fts_meta (key, value) VALUES (?, ?)",
                (f"last_sync_ts:{server}", '0'),
            )
            fts_conn.execute(
                "INSERT OR REPLACE INTO part_fts_meta (key, value) VALUES (?, ?)",
                (f"last_sync_rowid:{server}", '0'),
            )
            fts_conn.commit()

            logger.info("FTS full resync [%s]: %d parts indexed.", server, count)

            if count == 0:
                logger.info(
                    "FTS DB initialized for server '%s' but no part rows found.",
                    server,
                )
                _fts_available = False
            else:
                _fts_available = True

            return  # success

        except (sqlite3.OperationalError, IOError) as exc:
            msg = str(exc).lower()
            if "database is locked" in msg and attempt < max_retries:
                logger.debug(
                    "FTS init for '%s' attempt %d/%d (db locked), retrying…",
                    server, attempt, max_retries,
                )
            else:
                logger.warning(
                    "FTS init failed for server '%s': %s. "
                    "Read tools work; recall_session may be partially available.",
                    server, exc,
                )
                _fts_available = False

        finally:
            if primary_conn:
                primary_conn.close()
            if fts_conn:
                fts_conn.close()

        if attempt < max_retries:
            time.sleep(retry_delay)


# ── FTS connection pool ──────────────────────────────────────────────────────

class _FtsConnectionPool:
    """Per-thread SQLite connection pool for the **FTS** database.

    The FTS database is a **separate** file that may be shared across
    multiple MCP instances.  Schema is lazily initialized on first
    connection via ``CREATE TABLE IF NOT EXISTS``.
    """

    @classmethod
    def get(cls) -> sqlite3.Connection:
        """Return a thread-local connection to the FTS database."""
        thread_id = threading.current_thread().ident
        if thread_id is None:
            raise RuntimeError("threading.current_thread().ident is None")

        with _fts_lock:
            if thread_id not in _fts_connections:
                fts_path = resolve_fts_db_path()

                parent = os.path.dirname(fts_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)

                conn = sqlite3.connect(fts_path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.row_factory = sqlite3.Row

                _ensure_fts_schema(conn)
                conn.commit()

                _fts_connections[thread_id] = conn
            return _fts_connections[thread_id]

    @classmethod
    def close_all(cls) -> None:
        """Close every tracked FTS connection and clear the pool."""
        with _fts_lock:
            for conn in _fts_connections.values():
                try:
                    conn.close()
                except sqlite3.ProgrammingError:
                    pass
            _fts_connections.clear()


# ── Public API ────────────────────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    """Get or create a thread-local connection to the primary database."""
    return _PrimaryConnectionPool.get()


def get_fts_db() -> sqlite3.Connection:
    """Get or create a thread-local connection to the FTS database."""
    return _FtsConnectionPool.get()


def is_fts_available() -> bool:
    """Return whether the FTS5 full-text search index is ready.

    When ``_fts_available`` is ``True``, returns ``True`` immediately.

    When ``_fts_available`` is ``False``, attempts a live check of the FTS
    database.  If the table exists **and** contains rows, returns ``True``.
    This handles the scenario where the database was empty on first install
    (setting ``_fts_available = False``) but the background syncer has since
    populated the index with data.

    Returns ``False`` if the FTS database cannot be accessed.
    """
    if _fts_available:
        return True

    # _fts_available is False — try to verify the FTS table actually has data.
    # This covers the case where init ran on an empty database (setting the
    # flag to False) but the syncer has since indexed rows.
    try:
        conn = get_fts_db()
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='part_fts'"
        ).fetchone()
        if row is None:
            return False

        # Table exists — check that it has at least one row.
        # If data is present, FTS is functional even if the init flag was
        # set to False because the database was empty at startup time.
        count = conn.execute(
            "SELECT COUNT(*) FROM part_fts"
        ).fetchone()[0]
        return count > 0
    except Exception:
        return False


def close_all() -> None:
    """Close every tracked connection (both primary and FTS) and clear pools."""
    _PrimaryConnectionPool.close_all()
    _FtsConnectionPool.close_all()
