"""
Pydantic input models for the MCP tools.

Centralizes parameter validation in one place.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

from config import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    MAX_KEYWORD_LENGTH,
)


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# Shared model configuration applied to all input models.
_MODEL_CONFIG = ConfigDict(
    str_strip_whitespace=True,
    validate_assignment=True,
    extra="forbid",
)

_RESPONSE_FORMAT_DESCRIPTION = (
    "Output format: 'markdown' for human-readable or 'json' for machine-readable"
)


class ListProjectsInput(BaseModel):
    """Input parameters for the ``list_projects`` tool.

    Lists projects derived from session directory grouping:
    ``SELECT directory, COUNT(*) FROM session GROUP BY directory``.
    """

    model_config = _MODEL_CONFIG

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description=_RESPONSE_FORMAT_DESCRIPTION,
    )


class ListSessionsInput(BaseModel):
    """Input parameters for the ``list_sessions`` tool."""

    model_config = _MODEL_CONFIG

    directory: str = Field(
        ...,
        description="The directory path to list sessions for",
        min_length=1,
    )
    limit: int = Field(
        default=DEFAULT_LIST_LIMIT,
        description=f"Maximum number of sessions to return (1-{MAX_PAGE_SIZE})",
        ge=1,
        le=MAX_PAGE_SIZE,
    )
    offset: int = Field(
        default=0,
        description="Number of sessions to skip for pagination (>= 0)",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description=_RESPONSE_FORMAT_DESCRIPTION,
    )


class ReadMessagesInput(BaseModel):
    """Input parameters for the ``read_messages`` tool."""

    model_config = _MODEL_CONFIG

    session_id: str = Field(
        ...,
        description="The session UUID to read messages from",
        min_length=1,
    )
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        description=f"Number of messages per page (1-{MAX_PAGE_SIZE}). Default {DEFAULT_PAGE_SIZE}.",
        ge=1,
        le=MAX_PAGE_SIZE,
    )
    offset: int = Field(
        default=0,
        description="Starting offset for pagination (>= 0)",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description=_RESPONSE_FORMAT_DESCRIPTION,
    )


class RecallSessionInput(BaseModel):
    """Input parameters for the ``recall_session`` tool."""

    model_config = _MODEL_CONFIG

    keywords: str = Field(
        ...,
        description=(
            "Space-separated keywords to search for in message content. "
            "Multiple keywords are combined with OR logic (any match counts). "
            f"Example: 'error handling authentication' (max {MAX_KEYWORD_LENGTH} chars)"
        ),
        min_length=1,
        max_length=MAX_KEYWORD_LENGTH,
    )

    @computed_field
    def keywords_list(self) -> list[str]:
        """Parsed list of non-empty keywords."""
        return [kw.strip() for kw in self.keywords.split() if kw.strip()]

    directory: Optional[str] = Field(
        default=None,
        description=(
            "Optional directory path to scope the search to a single project. "
            "Omit to search across all projects."
        ),
    )
    agent: Optional[str] = Field(
        default=None,
        description=(
            "Optional agent type to filter by (e.g. 'explorer', 'general', 'camofox'). "
            "Omit to search across all agents."
        ),
    )
    limit: int = Field(
        default=DEFAULT_SEARCH_LIMIT,
        description=f"Maximum number of matching parts to return (1-{MAX_SEARCH_LIMIT})",
        ge=1,
        le=MAX_SEARCH_LIMIT,
    )
    offset: int = Field(
        default=0,
        description="Number of results to skip for pagination (>= 0)",
        ge=0,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description=_RESPONSE_FORMAT_DESCRIPTION,
    )
