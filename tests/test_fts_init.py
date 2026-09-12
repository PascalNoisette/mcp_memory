#!/usr/bin/env python3
"""
Unit tests for FTS (Full-Text Search) index creation.

Verifies that when the FTS database does not exist, ``init_db()``
creates it and populates it from the primary database.

This test uses a **temporary** FTS path so it never touches the live
``opencode_fts.db`` index.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

# Ensure FTS_DB_PATH points to a temporary file.
_temp_db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
os.environ.setdefault(
    "DATABASE_PATH",
    _temp_db_path + ":rw",
)
os.environ.setdefault(
    "FTS_DB_PATH",
    _temp_db_path + "_test-fts.db",
)
os.environ.setdefault("SERVER_NAME", "memory")


class TestFtsInitWhenMissing(unittest.TestCase):
    """Test that init_db() creates the FTS index when it is missing."""

    def setUp(self):
        # Save original env vars so tearDown can restore them.
        self._orig_db_path = os.environ.get("DATABASE_PATH")
        self._orig_fts_path = os.environ.get("FTS_DB_PATH")
        self._orig_server_name = os.environ.get("SERVER_NAME")

        # Create a unique temporary FTS DB path for this test run.
        self._temp_dir = tempfile.mkdtemp()
        self._fts_path = os.path.join(self._temp_dir, "fts_test.db")
        os.environ["DATABASE_PATH"] = self._orig_db_path or _temp_db_path + ":rw"
        os.environ["FTS_DB_PATH"] = self._fts_path
        os.environ["SERVER_NAME"] = self._orig_server_name or "memory"

        # Reset the FTS init flag so that init_db() will actually create
        # the FTS database on each test.  This is essential because the
        # flag is set once (module-level) and never reset by the normal
        # reload cycle — without this, subsequent tests would skip FTS
        # creation and fail the "file exists" assertions.
        import importlib
        import database
        importlib.reload(database)
        database._fts_initialized.clear()
        database._fts_available = False

    def tearDown(self):
        # Restore original env vars.
        if self._orig_db_path is not None:
            os.environ["DATABASE_PATH"] = self._orig_db_path
        elif "DATABASE_PATH" in os.environ:
            del os.environ["DATABASE_PATH"]

        if self._orig_fts_path is not None:
            os.environ["FTS_DB_PATH"] = self._orig_fts_path
        elif "FTS_DB_PATH" in os.environ:
            del os.environ["FTS_DB_PATH"]

        if self._orig_server_name is not None:
            os.environ["SERVER_NAME"] = self._orig_server_name
        elif "SERVER_NAME" in os.environ:
            del os.environ["SERVER_NAME"]

        # Force-reload the database module to reset the init flag.
        import importlib
        import database
        importlib.reload(database)

        # Clean up temp directory.
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_fts_created_when_missing(self):
        """When the FTS database file does not exist, init_db() creates it
        with the ``part_fts`` FTS5 table and ``part_fts_meta`` table."""
        import database

        # Ensure the temp FTS file does NOT exist.
        if os.path.exists(self._fts_path):
            os.unlink(self._fts_path)

        # Verify FTS does not exist before init.
        self.assertFalse(os.path.exists(self._fts_path))

        # Call init_db — should create the FTS schema.
        database.init_db()

        # Verify FTS file was created.
        self.assertTrue(os.path.exists(self._fts_path))

        # Verify the tables exist in the FTS DB.
        import sqlite3

        conn = sqlite3.connect(self._fts_path)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r[0] for r in tables}
            self.assertIn("part_fts", table_names)
            self.assertIn("part_fts_meta", table_names)
        finally:
            conn.close()

    def test_fts_populated_with_data(self):
        """When init_db() creates the FTS index, it populates it from the
        primary database."""
        import database

        # Ensure the temp FTS file does NOT exist.
        if os.path.exists(self._fts_path):
            os.unlink(self._fts_path)

        database.init_db()

        # Verify some rows were indexed.
        import sqlite3

        conn = sqlite3.connect(self._fts_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM part_fts WHERE server = 'memory'"
            ).fetchone()[0]
            # The opencode.db has many sessions with parts.
            # At minimum, there should be some indexed rows.
            self.assertGreater(
                count,
                0,
                f"Expected >0 rows in FTS for server 'memory', got {count}",
            )
        finally:
            conn.close()

    def test_fts_not_created_in_ro_mode(self):
        """:ro mode should skip FTS creation entirely."""
        import database

        # Set DATABASE_PATH to :ro mode.
        os.environ["DATABASE_PATH"] = (
            os.path.expanduser("~/.local/share/opencode/opencode.db") + ":ro"
        )
        os.environ["FTS_DB_PATH"] = self._fts_path

        # Ensure the temp FTS file does NOT exist.
        if os.path.exists(self._fts_path):
            os.unlink(self._fts_path)

        database.init_db()

        # FTS should NOT have been created.
        self.assertFalse(os.path.exists(self._fts_path))
        self.assertFalse(database.is_fts_available())

    def test_fts_schema_has_server_column(self):
        """The part_fts table must include the ``server`` column for
        per-instance isolation."""
        import database

        if os.path.exists(self._fts_path):
            os.unlink(self._fts_path)

        database.init_db()

        import sqlite3

        conn = sqlite3.connect(self._fts_path)
        try:
            cols = conn.execute("PRAGMA table_info(part_fts)").fetchall()
            col_names = [c[1] for c in cols]
            self.assertIn("server", col_names)
        finally:
            conn.close()

    def test_fts_rows_tagged_with_server(self):
        """All rows in the FTS table should be tagged with the SERVER_NAME."""
        import database

        if os.path.exists(self._fts_path):
            os.unlink(self._fts_path)

        database.init_db()

        import sqlite3

        conn = sqlite3.connect(self._fts_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT server FROM part_fts"
            ).fetchall()
            servers = {r[0] for r in rows}
            self.assertIn("memory", servers)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
