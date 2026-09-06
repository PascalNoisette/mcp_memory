#!/usr/bin/env python3
"""
Shared constants used across the application.
"""

from __future__ import annotations

# Text truncation / preview sizes
TEXT_TRUNCATION_LIMIT: int = 2000       # max chars returned per message text
SNIPPET_PREVIEW_LENGTH: int = 500       # chars for text previews
HIGHLIGHT_PREVIEW_CHARS: int = 150      # max chars for highlight preview
HIGHLIGHT_CONTEXT_CHARS: int = 30       # chars before/after matched keyword

# ID display
ID_TRUNCATE_LENGTH: int = 12            # chars shown when abbreviating UUIDs
