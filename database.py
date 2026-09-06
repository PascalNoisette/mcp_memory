"""
Database access layer for the Kilo Code MCP Memory Server.

Provides a thread-safe connection pool for the SQLite backend.  Connections
are created lazily per-thread and closed explicitly via ``close_all``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

# ── Database mode tracking (set on first call to resolve_db_path()) ─────────
# ``_db_mode`` is set by ``resolve_db_path()`` on its first invocation based
# on the ``:mode`` suffix in the ``DATABASE_PATH`` environment variable
# (Docker-style volume syntax).
#   * ``""``  → default: no FTS init, no index creation, lenient errors
#   * ``"ro"``→ read-only: open with URI mode=ro, no FTS init
#   * ``"rw"``→ read-write: trigger FTS init on startup
_db_mode: str = ""


def parse_db_path(raw_path: str) -> tuple[str, str]:
    """Parse a ``DATABASE_PATH`` value into (path, mode).

    Supports Docker-style volume syntax where the *mode* is appended after
    a colon (:):

    ====  ==========================================
    Value Path                               Mode
    ====  ==========================================
    ``"/data/db"``          ``/data/db``             ``""``  (default)
    ``"/data/db:ro"``       ``/data/db``             ``"ro"``
    ``"/data/db:rw"``       ``/data/db``             ``"rw"``
    ``"/data/db:"``         ``/data/db``             ``""``  (explicit empty)
    ====  ==========================================

    If the value contains a colon, everything after the **last** colon is
    treated as the mode (lower-cased and stripped).  Otherwise the whole
    string is returned as the path with an empty mode.

    Returns:
        ``(path, mode)`` where *mode* is ``""``, ``"ro"``, or ``"rw"``.
    """
    last_colon = raw_path.rfind(":")
    if last_colon != -1:
        path = raw_path[:last_colon]
        mode = raw_path[last_colon + 1:].strip().lower()
        if mode not in ("ro", "rw", ""):
            # Invalid mode – treat the whole string as a plain path
            return raw_path, ""
        return path, mode
    return raw_path, ""


def resolve_db_path() -> str:
    """Resolve the database path from the environment at runtime.

    Parses the ``DATABASE_PATH`` environment variable using
    ``parse_db_path()`` which supports Docker-style volume syntax
    (e.g. ``"/data/db:ro"``, ``"/data/db:rw"``).  Sets the module-level
    ``_db_mode`` variable based on the parsed mode.

    Returns the file path component (without the ``:mode`` suffix).
    Falls back to the default path when ``DATABASE_PATH`` is not set.
    """
    global _db_mode
    raw = os.environ.get(
        "DATABASE_PATH",
        os.path.expanduser("~/.local/share/opencode/opencode-local.db"),
    )
    path, mode = parse_db_path(raw)
    _db_mode = mode
    return path


def resolve_db_mode() -> str:
    """Return the database access mode parsed from ``DATABASE_PATH``.

    Returns ``""``, ``"ro"``, or ``"rw"``.
    """
    return _db_mode


def is_db_readonly() -> bool:
    """Return ``True`` when the database should be opened read-only."""
    return _db_mode == "ro"


def should_init_fts() -> bool:
    """Return ``True`` when FTS5 init should be triggered.

    FTS init only runs when the mode is explicitly ``"rw"``.
    For all other modes (default/empty or ``"ro"``) the index is
    skipped and queries that depend on it will return a friendly
    error message instead.
    """
    return _db_mode == "rw"


# ── Module-level init flag and lock ──────────────────────────────────────────
# Ensures FTS is initialized exactly once, even if multiple threads call
# ``init_db()`` concurrently before the main thread finishes.
_init_lock = threading.Lock()
_fts_initialized = threading.Event()
_fts_available = False


def _drop_fts(conn: sqlite3.Connection) -> None:
    """Drop the FTS5 virtual table and its sync triggers (if any)."""
    conn.execute("DROP TRIGGER IF EXISTS part_fts_ai")
    conn.execute("DROP TRIGGER IF EXISTS part_fts_au")
    conn.execute("DROP TRIGGER IF EXISTS part_fts_ad")
    conn.execute("DROP TABLE IF EXISTS part_fts")
    conn.execute("DROP TABLE IF EXISTS part_fts_data")


def _init_fts5(conn: sqlite3.Connection) -> None:
    """Create and populate the FTS5 virtual table for full-text search.

    **Important:** This function uses a **standalone** FTS5 table (without
    ``content='part'``) because SQLite < 3.44 does not support generated
    columns inside FTS5.  With ``content=`` the ``text`` column would be
    looked up on the ``part`` table, which has no such column — causing the
    ``no such column: T.text`` runtime error observed in the tests.

    A standalone table requires manual sync via triggers.

    Steps performed:
    1. Drop any existing ``part_fts`` table + triggers (to avoid stale schema).
    2. Create the new standalone ``part_fts`` table.
    3. Populate it from existing ``part`` data.
    4. Install INSERT / UPDATE / DELETE sync triggers.
    5. Rebuild the FTS index for optimal performance.
    """
    # ── 1. Drop old FTS table and triggers ────────────────────────────────
    _drop_fts(conn)

    # ── 2. Create standalone FTS5 virtual table (no content=) ────────────
    conn.execute(
        """
        CREATE VIRTUAL TABLE part_fts USING fts5(
            text,
            session_id,
            message_id,
            tokenize='unicode61'
        )
        """
    )

    # ── 3. Populate from existing data ────────────────────────────────────
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM part")
    total_count = cur.fetchone()[0]

    if total_count > 0:
        conn.execute(
            """
            INSERT INTO part_fts(rowid, text, session_id, message_id)
            SELECT rowid,
                   COALESCE(
                       CASE WHEN json_valid(data) = 1
                            THEN json_extract(data, '$.text') END,
                       CASE WHEN json_valid(data) = 1
                            THEN json_extract(data, '$.content') END,
                       CASE WHEN json_valid(data) = 1
                            THEN json_extract(data, '$.value') END,
                       ''
                   ),
                   session_id,
                   message_id
            FROM part
            WHERE json_valid(data) = 1
              AND (
                  json_extract(data, '$.text') IS NOT NULL
                  OR json_extract(data, '$.content') IS NOT NULL
                  OR json_extract(data, '$.value') IS NOT NULL
              )
            """
        )

    # ── 4. Install sync triggers ──────────────────────────────────────────
    conn.execute("DROP TRIGGER IF EXISTS part_fts_ai")
    conn.execute(
        """
        CREATE TRIGGER part_fts_ai AFTER INSERT ON part
        WHEN json_valid(new.data) = 1
        BEGIN
            INSERT INTO part_fts(rowid, text, session_id, message_id)
            VALUES (
                new.rowid,
                COALESCE(
                    json_extract(new.data, '$.text'),
                    json_extract(new.data, '$.content'),
                    json_extract(new.data, '$.value'),
                    ''
                ),
                new.session_id,
                new.message_id
            );
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS part_fts_au")
    conn.execute(
        """
        CREATE TRIGGER part_fts_au AFTER UPDATE ON part
        WHEN json_valid(new.data) = 1
        BEGIN
            UPDATE part_fts
            SET text = COALESCE(
                    json_extract(new.data, '$.text'),
                    json_extract(new.data, '$.content'),
                    json_extract(new.data, '$.value'),
                    ''
                ),
                session_id = new.session_id,
                message_id = new.message_id
            WHERE rowid = new.rowid;
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS part_fts_ad")
    conn.execute(
        """
        CREATE TRIGGER part_fts_ad AFTER DELETE ON part
        BEGIN
            DELETE FROM part_fts WHERE rowid = old.rowid;
        END
        """
    )

    # ── 5. Optimize the FTS index ─────────────────────────────────────────
    try:
        conn.execute("INSERT INTO part_fts(part_fts, rank) VALUES('rebuild')")
    except sqlite3.OperationalError:
        # Index may already be optimized or not yet populated enough
        pass


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Initialise the FTS5 full-text search index.

    This function is **idempotent** — it may be called multiple times safely.
    It drops any existing ``part_fts`` table + triggers, recreates them,
    and repopulates from the ``part`` table.

    **Mode-aware**: FTS init is only triggered when ``DATABASE_PATH`` ends
    with ``:rw`` (see ``should_init_fts()``).  For all other modes (plain
    path or ``:ro``) the function returns early without creating any
    indexes or triggers.

    Use a module-level lock to prevent race conditions when multiple threads
    call this function concurrently during startup.

    The caller may pass an existing connection; if *None*, a short-lived
    connection is opened, used, and closed.

    .. versionchanged:: 0.1.0
       Switched from ``content='part'`` to a standalone FTS5 table to avoid
       the ``no such column: T.text`` error on SQLite < 3.44.
    .. versionchanged:: 0.2.0
       Added mode-awareness: FTS init only runs when mode is ``"rw"``.
    """
    global _fts_initialized, _fts_available

    # Fast-path: already initialised
    if _fts_initialized.is_set():
        return

    with _init_lock:
        # Double-check after acquiring the lock
        if _fts_initialized.is_set():
            return

        # Ensure _db_mode is set even if config.py was never imported.
        # Without this, _db_mode stays "" and should_init_fts() always
        # returns False — the FTS path is silently skipped.
        resolve_db_path()

        # Skip FTS init unless mode is explicitly "rw"
        if not should_init_fts():
            logger.info(
                "FTS init skipped — mode is %r (only '%s' triggers FTS). "
                "Read tools work fine; recall_session requires :rw mode.",
                _db_mode,
                "rw",
            )
            _fts_available = False
            _fts_initialized.set()
            return

        if conn is None:
            # Open a short-lived connection just for init
            try:
                if is_db_readonly():
                    conn = sqlite3.connect(
                        f"file:{resolve_db_path()}?mode=ro", uri=True
                    )
                else:
                    conn = sqlite3.connect(resolve_db_path())
                _init_fts5(conn)
                conn.commit()
                _fts_available = True
                logger.info("FTS5 index initialised successfully.")
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "read-only" in msg or "cannot" in msg:
                    logger.warning(
                        "FTS init skipped — database may be read-only: %s. "
                        "The server will start normally; read tools work fine but "
                        "recall_session (full-text search) will be unavailable.",
                        exc,
                    )
                    _fts_available = False
                else:
                    logger.warning("FTS init failed: %s", exc)
                    _fts_available = False
            except (OSError, IOError) as exc:
                logger.warning(
                    "FTS init skipped — database file not accessible: %s. "
                    "Read tools work fine; recall_session requires a writable DB.",
                    exc,
                )
                _fts_available = False
            finally:
                if conn is not None:
                    conn.close()
        else:
            try:
                _init_fts5(conn)
                conn.commit()
                _fts_available = True
                logger.info("FTS5 index initialised successfully.")
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "read-only" in msg or "cannot" in msg:
                    logger.warning(
                        "FTS init skipped — database may be read-only: %s. "
                        "The server will start normally; read tools work fine but "
                        "recall_session (full-text search) will be unavailable.",
                        exc,
                    )
                    _fts_available = False
                else:
                    logger.warning("FTS init failed: %s", exc)
                    _fts_available = False

        _fts_initialized.set()


class _ConnectionPool:
    """Per-thread SQLite connection pool.

    Creates one connection per operating-system thread and stores it in a
    thread-local dict keyed by ``thread.ident``.  Connections are never
    closed automatically — the caller must invoke ``close_all()`` during
    graceful shutdown.

    The FTS5 full-text search index is initialised **once** at startup
    via ``init_db()`` (not here).  Triggers installed by ``init_db()``
    are visible to all connections in the same database file, so every
    new thread-local connection automatically benefits from FTS sync.
    """

    _connections: dict[int, sqlite3.Connection] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> sqlite3.Connection:
        """Return a thread-local database connection.

        When the database mode is ``"ro"`` the connection is opened via
        SQLite's URI mechanism with ``mode=ro`` so that write attempts
        raise an error.

        FTS5 is initialised once at startup via ``init_db()`` — this method
        does **not** call ``_init_fts5()``.
        """
        thread_id = threading.current_thread().ident
        if thread_id is None:
            raise RuntimeError("threading.current_thread().ident is None")

        with cls._lock:
            if thread_id not in cls._connections:
                db_path = resolve_db_path()
                if is_db_readonly():
                    # Open as read-only via SQLite URI.
                    conn = sqlite3.connect(
                        f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
                    )
                else:
                    conn = sqlite3.connect(db_path, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.row_factory = sqlite3.Row
                cls._connections[thread_id] = conn
            return cls._connections[thread_id]

    @classmethod
    def close_all(cls) -> None:
        """Close every tracked connection and clear the pool."""
        with cls._lock:
            for conn in cls._connections.values():
                try:
                    conn.close()
                except sqlite3.ProgrammingError:
                    pass  # already closed
            cls._connections.clear()


# Public alias matching the original function signature.
def get_db() -> sqlite3.Connection:
    """Get or create a thread-local database connection."""
    return _ConnectionPool.get()


def is_fts_available() -> bool:
    """Return whether the FTS5 full-text search index is ready.

    This checks the module-level flag set during init *and* verifies that the
    ``part_fts`` table actually exists in the database.  Use this at query
    time to detect stale state (e.g. init ran on a writable connection but
    later the file became read-only).
    """
    if not _fts_available:
        return False
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='part_fts'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


