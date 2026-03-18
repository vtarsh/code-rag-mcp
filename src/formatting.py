"""Snippet formatting utilities.

Shared helpers for cleaning search result snippets before display.
"""

from __future__ import annotations

import re

_REPO_TAG_RE = re.compile(r"\[Repo: [^\]]+\]\s*")


def strip_repo_tag(text: str) -> str:
    """Remove [Repo: ...] tags from snippet text.

    FTS5 snippets include a [Repo: repo_name] prefix added during indexing.
    This tag is useful internally but should be stripped before showing results.
    """
    return _REPO_TAG_RE.sub("", text)
