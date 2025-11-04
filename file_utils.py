# -*- coding: utf-8 -*-
"""Utility functions for manipulating files in a safe, UTF-8 friendly manner."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable


def ensure_directory(path: Path) -> None:
    """Ensure the directory exists."""
    path.mkdir(parents=True, exist_ok=True)


def create_temp_file(directory: Path, prefix: str = "tmp_", suffix: str = "") -> Path:
    """Create an empty temporary file in the specified directory."""
    ensure_directory(directory)
    descriptor, filename = tempfile.mkstemp(
        prefix=prefix, suffix=suffix, dir=str(directory)
    )
    os.close(descriptor)
    return Path(filename)


def _resolve_backup_path(original: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = original.with_name(f"{original.name}.{timestamp}.bak")
    counter = 1
    while candidate.exists():
        candidate = original.with_name(f"{original.name}.{timestamp}.{counter}.bak")
        counter += 1
    return candidate


def write_text_with_backup(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    make_backup: bool = True,
) -> Path:
    """Write text atomically, creating a timestamped backup if requested."""
    path = path.resolve()
    temp_path = create_temp_file(path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path.write_text(content, encoding=encoding)

    if path.exists() and make_backup:
        backup_path = _resolve_backup_path(path)
        shutil.copy2(path, backup_path)
    temp_path.replace(path)
    return path


def replace_text(
    path: Path,
    transform: Callable[[str], str],
    *,
    encoding: str = "utf-8",
    make_backup: bool = True,
) -> None:
    """Apply a transformation to a file and write the result atomically."""
    original_text = ""
    if path.exists():
        original_text = path.read_text(encoding=encoding)
    updated_text = transform(original_text)
    write_text_with_backup(
        path, updated_text, encoding=encoding, make_backup=make_backup
    )
