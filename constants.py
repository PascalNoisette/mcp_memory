#!/usr/bin/env python3
"""
Shared constants used across the application.
"""

from __future__ import annotations

# Text truncation / preview sizes
# (none — full text is always returned to the user)
HIGHLIGHT_PREVIEW_CHARS: int = 150      # max chars for highlight preview
HIGHLIGHT_CONTEXT_CHARS: int = 30       # chars before/after matched keyword

# ID display
ID_TRUNCATE_LENGTH: int = 12            # chars shown when abbreviating UUIDs

# FTS sync (seconds between background sync cycles)
INTERVAL_SYNC: int = 30                 # seconds between FTS incremental syncs
