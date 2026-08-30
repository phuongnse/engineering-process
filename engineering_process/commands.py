"""Bounded foreground execution for consumer-owned verification commands."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any, BinaryIO

from .contracts import ProcessError
from .supervision import process_supervisor


DEFAULT_OUTPUT_BYTES = 1_000_000
TERMINATION_SECONDS = 2
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "PRIVATE_KEY")
_EXECUTION_LOCK = threading.Lock()


def _child_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if name in {"PYTHONHOME", "PYTHONPATH"}:
            continue
        if any(marker in upper for marker in SECRET_MARKERS):
            continue
        environment[name] = value
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


class _OutputBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.total = 0
        self.exceeded = threading.Event()
        self.lock = threading.Lock()

    def add(self, size: int) -> int:
        with self.lock:
            remaining = max(0, self.maximum - self.total)
            self.total += size
            if self.total > self.maximum:
                self.exceeded.set()
            return min(size, remaining)


class _StreamDigest:
    def __init__(self) -> None:
        self.bytes = 0
        self.digest = hashlib.sha256()
        self.error: OSError | ValueError | None = None
        self.truncated = False

    def consume(self, stream: BinaryIO, budget: _OutputBudget) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                admitted = budget.add(len(chunk))
                self.bytes += admitted
                self.digest.update(chunk[:admitted])
                self.truncated = self.truncated or admitted < len(chunk)
        except (OSError, ValueError) as error:
            self.error = error
        finally:
            stream.close()

    def summary(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "sha256": "sha256:" + self.digest.hexdigest(),
            "truncated": self.truncated,
        }


def _run_check(project_root: Path, check: dict[str, Any]) -> dict[str, Any]:
    command = check["run"]
    if not isinstance(command, list) or not command:
        raise ProcessError(f"check {check.get('id')!r} has no command")
    working_directory = project_root / check.get("cwd", ".")
    try:
        working_directory = working_directory.resolve(strict=True)
        working_directory.relative_to(project_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ProcessError(
            f"check {check['id']}: working directory escapes project root"
        ) from error

    timeout = check.get("timeoutSeconds", 300)
    output_limit = check.get("maxOutputBytes", DEFAULT_OUTPUT_BYTES)
    started = time.monotonic()
    supervisor = process_supervisor()
    try:
        process = supervisor.spawn(
            tuple(command),
            working_directory=working_directory,
            environment=_child_environment(),
        )
    except OSError as error:
        raise ProcessError(f"check {check['id']}: cannot start command: {error}") from error
    assert process.stdout is not None and process.stderr is not None

    budget = _OutputBudget(output_limit)
    stdout_digest = _StreamDigest()
    stderr_digest = _StreamDigest()
    readers = [
        threading.Thread(
            target=stdout_digest.consume,
            args=(process.stdout, budget),
            daemon=True,
        ),
        threading.Thread(
            target=stderr_digest.consume,
            args=(process.stderr, budget),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    output_exceeded = False
    descendants = False
    cleanup_error: str | None = None
    deadline = started + timeout
    try:
        while process.poll() is None:
            supervisor.observe(process)
            if budget.exceeded.is_set():
                output_exceeded = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_SECONDS
                )
                descendants = descendants or cleanup.descendants_found
                cleanup_error = cleanup.error
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_SECONDS
                )
                descendants = descendants or cleanup.descendants_found
                cleanup_error = cleanup.error
                break
            time.sleep(0.01)
    except BaseException:
        supervisor.terminate(process, grace_seconds=TERMINATION_SECONDS)
        raise
    finally:
        if process.poll() is not None:
            cleanup = supervisor.finalize(
                process, grace_seconds=TERMINATION_SECONDS
            )
            descendants = descendants or cleanup.descendants_found
            cleanup_error = cleanup_error or cleanup.error
    exit_code = process.returncode if process.returncode is not None else -1

    for reader in readers:
        reader.join(timeout=TERMINATION_SECONDS)
    if any(reader.is_alive() for reader in readers):
        process.stdout.close()
        process.stderr.close()
        for reader in readers:
            reader.join(timeout=1)
    output_exceeded = output_exceeded or budget.exceeded.is_set()
    stream_failed = any(reader.is_alive() for reader in readers)
    stream_failed = stream_failed or stdout_digest.error is not None or stderr_digest.error is not None
    stream_failed = stream_failed or cleanup_error is not None

    passed = (
        exit_code == 0
        and not timed_out
        and not output_exceeded
        and not descendants
        and not stream_failed
    )
    return {
        "id": check["id"],
        "status": "passed" if passed else "failed",
        "exitCode": exit_code,
        "timedOut": timed_out,
        "outputExceeded": output_exceeded,
        "descendantsTerminated": descendants,
        "streamFailed": stream_failed,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": stdout_digest.summary(),
        "stderr": stderr_digest.summary(),
    }


def run_check(project_root: Path, check: dict[str, Any]) -> dict[str, Any]:
    with _EXECUTION_LOCK:
        return _run_check(project_root, check)


def run_profile(
    project_root: Path,
    project: dict[str, Any],
    profile: str,
) -> dict[str, Any]:
    checks = project["profiles"].get(profile)
    if checks is None:
        raise ProcessError(f"unknown verification profile: {profile}")
    reports: list[dict[str, Any]] = []
    for check in checks:
        report = run_check(project_root, check)
        reports.append(report)
        if report["status"] != "passed":
            break
    return {
        "profile": profile,
        "status": "passed" if len(reports) == len(checks) and all(
            report["status"] == "passed" for report in reports
        ) else "failed",
        "checks": reports,
    }
