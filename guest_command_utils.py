# -*- coding: utf-8 -*-
"""Utility helpers for executing commands inside the guest VM."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

GuestCommandExecutor = Callable[..., Tuple[int, str, str]]


@dataclass
class GuestCommandResult:
    """Result details from running a guest command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    attempt: int
    retries: int

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def run_guest_command(
    executor: GuestCommandExecutor,
    command: str,
    *,
    retries: int = 0,
    retry_delay: float = 1.0,
    check_exit_code: bool = False,
) -> GuestCommandResult:
    """Run a command inside the guest with optional retries."""

    attempt = 0
    while True:
        exit_code, stdout, stderr = executor(command, check_exit_code=check_exit_code)
        result = GuestCommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            attempt=attempt,
            retries=retries,
        )
        if exit_code == 0 or attempt >= retries:
            return result
        attempt += 1
        time.sleep(max(0.0, retry_delay))

