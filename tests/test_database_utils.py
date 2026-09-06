#!/usr/bin/env python3
"""
Unit tests for database utility functions.

Tests:
  - parse_db_path() edge cases (multiple colons, Windows paths, invalid mode, etc.)
  - is_db_readonly() and should_init_fts() mode resolution
  - resolve_db_mode() return values
"""

import os
import unittest
from unittest.mock import patch

from database import parse_db_path, is_db_readonly, should_init_fts, resolve_db_mode


class TestParseDbPath(unittest.TestCase):
    """Test parse_db_path() edge cases."""

    def test_plain_path(self):
        """A path without colon should return empty mode."""
        path, mode = parse_db_path("/data/db")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "")

    def test_readonly_mode(self):
        """:ro suffix should be parsed correctly."""
        path, mode = parse_db_path("/data/db:ro")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "ro")

    def test_readwrite_mode(self):
        """:rw suffix should be parsed correctly."""
        path, mode = parse_db_path("/data/db:rw")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_empty_mode_suffix(self):
        """: with nothing after should return empty mode."""
        path, mode = parse_db_path("/data/db:")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "")

    def test_uppercase_ro(self):
        """Uppercase :RO should be lowercased to 'ro'."""
        path, mode = parse_db_path("/data/db:RO")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "ro")

    def test_uppercase_rw(self):
        """Uppercase :RW should be lowercased to 'rw'."""
        path, mode = parse_db_path("/data/db:RW")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_invalid_mode_returns_plain_path(self):
        """An invalid mode like :xyz should treat the whole string as path."""
        path, mode = parse_db_path("/data/db:xyz")
        self.assertEqual(path, "/data/db:xyz")
        self.assertEqual(mode, "")

    def test_whitespace_in_mode(self):
        """Whitespace around mode should be stripped."""
        path, mode = parse_db_path("/data/db: rw ")
        self.assertEqual(path, "/data/db")
        self.assertEqual(mode, "rw")

    def test_windows_drive_letter(self):
        """Windows paths like C:\\data\\db should work (fallback for invalid mode)."""
        path, mode = parse_db_path(r"C:\data\db")
        self.assertEqual(path, r"C:\data\db")
        self.assertEqual(mode, "")

    def test_windows_drive_with_mode(self):
        """Windows path with mode suffix should work."""
        path, mode = parse_db_path(r"C:\data\db:ro")
        self.assertEqual(path, r"C:\data\db")
        self.assertEqual(mode, "ro")

    def test_only_mode(self):
        """:ro alone should return empty path and 'ro' mode."""
        path, mode = parse_db_path(":ro")
        self.assertEqual(path, "")
        self.assertEqual(mode, "ro")

    def test_multiple_colons(self):
        """Only the LAST colon determines the mode."""
        path, mode = parse_db_path("http://host:8080/db:ro")
        self.assertEqual(path, "http://host:8080/db")
        self.assertEqual(mode, "ro")

    def test_multiple_colons_invalid_mode(self):
        """If last segment after colon is invalid mode, whole string is path."""
        path, mode = parse_db_path("http://host:8080/db:invalid")
        self.assertEqual(path, "http://host:8080/db:invalid")
        self.assertEqual(mode, "")


class TestDbModeFunctions(unittest.TestCase):
    """Test is_db_readonly(), should_init_fts(), resolve_db_mode().

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
        # Re-initialize FTS after reload since _fts_available is reset
        # to False by the reload. This ensures subsequent tests (recall_session, etc.)
        # still have FTS available.
        database.init_db()
        # Re-import the functions we need
        from database import is_db_readonly, should_init_fts, resolve_db_mode
        self.is_db_readonly = is_db_readonly
        self.should_init_fts = should_init_fts
        self.resolve_db_mode = resolve_db_mode

    def test_default_mode_empty(self):
        """Default (no DATABASE_PATH) should give empty mode."""
        # Remove DATABASE_PATH to use default
        old = os.environ.pop("DATABASE_PATH", None)
        try:
            import importlib
            import database
            importlib.reload(database)
            # Trigger resolve_db_path() to reset _db_mode to default
            database.resolve_db_path()
            self.assertEqual(database.resolve_db_mode(), "")
            self.assertFalse(database.is_db_readonly())
            self.assertFalse(database.should_init_fts())
        finally:
            if old is not None:
                os.environ["DATABASE_PATH"] = old

    def test_ro_mode(self):
        """:ro mode should set is_db_readonly=True and should_init_fts=False."""
        os.environ["DATABASE_PATH"] = "/tmp/db:ro"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        self.assertTrue(database.is_db_readonly())
        self.assertFalse(database.should_init_fts())
        self.assertEqual(database.resolve_db_mode(), "ro")

    def test_rw_mode(self):
        """:rw mode should set is_db_readonly=False and should_init_fts=True."""
        os.environ["DATABASE_PATH"] = "/tmp/db:rw"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        self.assertFalse(database.is_db_readonly())
        self.assertTrue(database.should_init_fts())
        self.assertEqual(database.resolve_db_mode(), "rw")

    def test_plain_path_mode(self):
        """Plain path (no mode suffix) should give empty mode."""
        os.environ["DATABASE_PATH"] = "/tmp/db"
        import importlib
        import database
        importlib.reload(database)
        # Trigger resolve_db_path() to set _db_mode from the new env var
        database.resolve_db_path()
        self.assertFalse(database.is_db_readonly())
        self.assertFalse(database.should_init_fts())
        self.assertEqual(database.resolve_db_mode(), "")


if __name__ == "__main__":
    unittest.main()
