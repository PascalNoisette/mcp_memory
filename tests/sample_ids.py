#!/usr/bin/env python3
"""
Shared helpers and fixtures for the e2e test suite.
"""

import asyncio
import json
import os
from typing import Any

# Point at the real database (opencode.db) with real sessions and messages.
os.environ.setdefault("DATABASE_PATH", os.path.expanduser(
    "~/.local/share/opencode/opencode.db",
))


def _run_async(coro):
    """Run an async tool call inside a unittest method."""
    return asyncio.run(coro)


# ── Sample directories extracted from the live database ────────────────────────
# These are hard-coded once we know the DB contents. If the DB changes,
# regenerate with: python3 -c "from tests.sample_ids import _refresh; _refresh()"

# The first (newest) directory
DIRECTORY = "/home/opencode/workspace/netpascal_mcp_memory"
DIRECTORY_SECOND = "/home/opencode/workspace/opencode"

# A session with many parts in the second directory (the biggest one)
# We'll discover this at runtime instead.
