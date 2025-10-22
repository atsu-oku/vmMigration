# -*- coding: utf-8 -*-
"""Thin wrapper around the curl command-line tool."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional


class CurlError(RuntimeError):
    """Raised when curl cannot be executed or returns an error."""


@dataclass
class CurlResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def run_curl(args: List[str], *, timeout: Optional[int] = 30) -> CurlResult:
    """Execute curl with the given arguments."""
    command = ["curl", "--silent", "--show-error", "--location"] + args
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as error:
        raise CurlError("curl executable not found") from error
    except subprocess.TimeoutExpired as error:
        raise CurlError(f"curl timed out after {timeout} seconds") from error

    return CurlResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def head(url: str, *, timeout: Optional[int] = 30) -> CurlResult:
    """Perform an HTTP HEAD request."""
    return run_curl(["--head", url], timeout=timeout)

