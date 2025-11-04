# -*- coding: utf-8 -*-
"""Ensure local modules can be imported when running lint tools directly."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def ensure_project_root() -> Path:
    """Ensure the repository root is on sys.path for local imports."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    return PROJECT_ROOT


ensure_project_root()
