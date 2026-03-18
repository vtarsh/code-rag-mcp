"""Shared fixtures for the test suite."""

import os
import sys
from pathlib import Path

# Set CODE_RAG_HOME to the project directory so config.py finds the right data.
# This must happen before any src imports (config.py reads env at import time).
_project_root = str(Path(__file__).resolve().parent.parent)
os.environ.setdefault("CODE_RAG_HOME", _project_root)

# Ensure src imports work
sys.path.insert(0, _project_root)
