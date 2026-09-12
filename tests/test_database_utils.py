#!/usr/bin/env python3
"""
Unit tests for database utility functions.

Tests:
  - _parse_db_spec() edge cases (multiple colons, Windows paths, invalid mode, etc.)
  - is_db_readonly() and FTS availability mode resolution
  - resolve_db_mode() return values

In the new architecture (v0.3.0+):
  - ``:ro`` mode → FTS disabled (no sync, no search)
  - default/``:rw`` mode → FTS enabled (separate DB, background syncer)
  - The primary database is always opened read-only.
"""

import os
import unittest
from unittest.mock import patch

# Ensure FTS_DB_PATH is set so that when tearDown reloads the database module,
# the isolated test FTS DB path is still available.
os.environ.setdefault(
    "FTS_DB_PATH",
    os.path.expanduser("~/.local/share/opencode/opencode.db") + "_test-fts.db",
)

from database import _parse_db_spec, is_db_readonly, resolve_db_mode


class TestParseDbPath(unittest.TestCase):
    """Test _parse_db_spec() edge cases."""

    def test_plain_path(self):
        """A path without colon should return empty mode."""
        path, mode = _parse_db_spec("/data/db")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "")

    def test_readonly_mode(self):
        """:ro suffix should be parsed correctly."""
        path, mode = _parse_db_spec("/data/db:ro")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "ro")

    def test_readwrite_mode(self):
        """:rw suffix should be parsed correctly."""
        path, mode = _parse_db_spec("/data/db:rw")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_empty_mode_suffix(self):
        """: with nothing after should return empty mode."""
        path, mode = _parse_db_spec("/data/db:")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "")

    def test_uppercase_ro(self):
        """Uppercase :RO should be lowercased to 'ro'."""
        path, mode = _parse_db_spec("/data/db:RO")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "ro")

    def test_uppercase_rw(self):
        """Uppercase :RW should be lowercased to 'rw'."""
        path, mode = _parse_db_spec("/data/db:RW")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_invalid_mode_returns_plain_path(self):
        """An invalid mode like :xyz should treat the whole string as path."""
        path, mode = _parse_db_spec("/data/db:xyz")
        self.assertEqual(path, "/data/db:xyz")
        self.assertEqual(mode, "")

    def test_whitespace_in_mode(self):
        """Whitespace around mode should be stripped."""
        path, mode = _parse_db_spec("/data/db: rw ")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_windows_drive_letter(self):
        """Windows paths like C:\\data\\db should work (fallback for invalid mode)."""
        path, mode = _parse_db_spec(r"C:\data\db")
        self.assertEqual(path, r"C:\data\db")
        self.assertEqual(mode, "")

    def test_windows_drive_with_mode(self):
        """Windows path with mode suffix should work."""
        path, mode = _parse_db_spec(r"C:\data\db:ro")
        self.assertEqual(path, r"C:\data\db")
        self.assertEqual(mode, "ro")

    def test_only_mode(self):
        """:ro alone should return empty path and 'ro' mode."""
        path, mode = _parse_db_spec(":ro")
        self.assertEqual(path, "")
        self.assertEqual(mode, "ro")

    def test_multiple_colons(self):
        """Only the LAST colon determines the mode."""
        path, mode = _parse_db_spec("http://host:8080/db:ro")
        self.assertEqual(path, "http://host:8080/db")
        self.assertEqual(mode, "ro")

    def test_multiple_colons_invalid_mode(self):
        """If last segment after colon is invalid mode, whole string is path."""
        path, mode = _parse_db_spec("http://host:8080/db:invalid")
        self.assertEqual(path, "http://host:8080/db:invalid")
        self.assertEqual(mode, "")


class TestDbModeFunctions(unittest.TestCase):
    """Test is_db_readonly(), resolve_db_mode().

    In the new architecture (v0.3.0+):
      - ``:ro`` mode → FTS disabled, primary opened read-only
      - default/``:rw`` mode → FTS enabled (separate DB), primary opened read-only
      - is_db_readonly() returns True only for ``:ro`` mode (indicating FTS is disabled)
      - resolve_db_mode() always returns the parsed mode.

    These functions depend on the module-level _db_mode variable,
    which is set by resolve_db_path(). We temporarily override
    DATABASE_PATH for each test and restore it afterwards.
    """

    def setUp(self):
        # Save the original DATABASE_PATH
        self._original_db_path = os.environ.get("DATABASE_PATH")

    def tearDown(self):
        # Restore the original DATABASE_PATH
        if self._original_db_path is not None:
            os.environ["DATABASE_PATH"] = self._original_db_path
        elif "DATABASE_PATH" in os.environ:
            del os.environ["DATABASE_PATH"]
        # Reset module state by reloading the database module
        import importlib
        import database
        importlib.reload(database)
        # Re-initialize after reload since _fts_available is reset
        database.init_db()
        # Re-import the functions we need
        from database import is_db_readonly, resolve_db_mode

        self.is_db_readonly = is_db_readonly
        self.resolve_db_mode = resolve_db_mode

    def _reset_fts_init_flag(self):
        """Reset the module-level FTS init flag.

        This is needed when a test wants to call ``init_db()`` with a
        different ``FTS_DB_PATH`` — the flag must be cleared first,
        otherwise ``init_db()`` returns immediately (idempotent guard).
        """
        import database

        database._fts_initialized.clear()
        database._fts_available = False

    def test_default_mode_empty(self):
        """Default (no DATABASE_PATH) should give empty mode and FTS enabled."""
        # Remove DATABASE_PATH to use default
        old = os.environ.pop("DATABASE_PATH", None)
        try:
            import importlib
            import database
            importlib.reload(database)
            # Trigger resolve_db_path() to reset _db_mode to default
            database.resolve_db_path()
            self.assertEqual(database.resolve_db_mode(), "")
            # In new model: primary is always opened read-only,
            # but is_db_readonly() indicates FTS status
            self.assertFalse(database.is_db_readonly())
        finally:
            if old is not None:
                os.environ["DATABASE_PATH"] = old

    def test_ro_mode(self):
        """:ro mode should set is_db_readonly=True (FTS disabled)."""
        os.environ["DATABASE_PATH"] = "/tmp/db:ro"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        self.assertTrue(database.is_db_readonly())
        self.assertEqual(database.resolve_db_mode(), "ro")

    def test_rw_mode(self):
        """:rw mode should be treated as default (FTS enabled)."""
        os.environ["DATABASE_PATH"] = "/tmp/db:rw"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        # :rw is treated same as default — FTS is enabled
        self.assertFalse(database.is_db_readonly())
        self.assertEqual(database.resolve_db_mode(), "rw")

    def test_plain_path_mode(self):
        """Plain path (no mode suffix) should give empty mode and FTS enabled."""
        os.environ["DATABASE_PATH"] = "/tmp/db"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        self.assertFalse(database.is_db_readonly())
        self.assertEqual(database.resolve_db_mode(), "")


if __name__ == "__main__":
    unittest.main()
