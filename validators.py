#!/usr/bin/env python3
"""
Error-handling decorator for MCP tool functions.

Wraps each tool function in a try/except that returns a user-friendly
error string instead of letting exceptions propagate to the MCP client.
"""

from __future__ import annotations

import logging

from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)


def tool_error_handler(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that catches unexpected errors in tool functions."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.exception("Tool %s failed: %s", func.__name__, exc)
            return "An internal error occurred. Please try again."

    return wrapper
