#!/usr/bin/env python3
"""Thin entry point — delegates to :mod:`src.index.builders`.

Historical names (``chunk_code``, ``chunk_markdown``, ``MAX_CHUNK``, ...) are
re-exported so that ``tests/test_chunking.py`` and the legacy import surface
keep working unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``src/`` importable when this script is invoked directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.index.builders import *  # noqa: F403
from src.index.builders import build_index

if __name__ == "__main__":
    build_index()
