from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError
from .supervision import process_supervisor


GIT_STDERR_LIMIT = 16_384
GIT_TERMINATION_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _drain(
    stream,
    capture: dict[str, object],
    *,
    limit: int,
) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            data = capture["data"]
            assert isinstance(data, bytearray)
            remaining = limit - len(data)
            if remaining > 0:
                data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture["overflow"] = True
    except (OSError, ValueError):
        capture["streamError"] = True
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_git(
    root: Path,
    arguments: list[str],
    *,
    label: str,
    timeout_seconds: float,
    max_stdout_bytes: int,
) -> GitResult:
    if timeout_seconds <= 0:
        raise ContractError(f"{label}: Git time budget is exhausted")
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    supervisor = process_supervisor()
    try:
        process = supervisor.spawn(
            ("git", *arguments),
            working_directory=root,
            environment=environment,
        )
    except (OSError, ValueError) as error:
        raise ContractError(f"{label}: cannot start Git: {error}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture: dict[str, object] = {
        "data": bytearray(),
        "overflow": False,
        "streamError": False,
    }
    stderr_capture: dict[str, object] = {
        "data": bytearray(),
        "overflow": False,
        "streamError": False,
    }
    threads = (
        threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_capture),
            kwargs={"limit": max_stdout_bytes},
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_capture),
            kwargs={"limit": GIT_STDERR_LIMIT},
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()
    timed_out = False
    cleanup_error: str | None = None
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup = supervisor.terminate(
            process, grace_seconds=GIT_TERMINATION_GRACE_SECONDS
        )
        if not cleanup.bounded or cleanup.error is not None:
            cleanup_error = cleanup.error or "Git process tree cleanup was not bounded"
    finally:
        if process.poll() is not None:
            cleanup = supervisor.finalize(
                process, grace_seconds=GIT_TERMINATION_GRACE_SECONDS
            )
            if not cleanup.bounded or cleanup.error is not None:
                cleanup_error = cleanup.error or "Git descendant cleanup was not bounded"
        for thread in threads:
            thread.join(timeout=GIT_TERMINATION_GRACE_SECONDS)
    if any(thread.is_alive() for thread in threads):
        raise ContractError(f"{label}: Git retained output streams after cleanup")
    if cleanup_error is not None:
        raise ContractError(f"{label}: {cleanup_error}")
    if timed_out:
        raise ContractError(f"{label}: Git exceeded {timeout_seconds:.3f} seconds")
    if stdout_capture["streamError"] or stderr_capture["streamError"]:
        raise ContractError(f"{label}: could not read bounded Git output")
    if stdout_capture["overflow"]:
        raise ContractError(
            f"{label}: Git stdout exceeds {max_stdout_bytes} bytes"
        )
    if stderr_capture["overflow"]:
        raise ContractError(f"{label}: Git stderr exceeds {GIT_STDERR_LIMIT} bytes")
    if process.returncode is None:
        raise ContractError(f"{label}: Git did not report an exit code")
    return GitResult(
        returncode=process.returncode,
        stdout=bytes(stdout_capture["data"]),
        stderr=bytes(stderr_capture["data"]),
    )


def remaining_seconds(deadline: float, *, label: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError(f"{label}: time budget is exhausted")
    return remaining
