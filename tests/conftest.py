"""Pytest configuration.

Ensures the repo root is on sys.path so test modules that import from
sibling top-level packages (e.g. ``kindle.client.interactive``) work even
when the package is installed in editable mode without putting those
sibling packages on sys.path.

The CI workflow installs ``.[dev]`` which only installs
``hermes_eink_dashboard`` (under ``src/``) — the ``kindle/`` package is
not yet a Python distribution, so tests need the repo root to import
modules from it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))