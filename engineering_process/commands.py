"""Bounded foreground execution for consumer-owned verification commands."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Iterator

from .contracts import ProcessError
from .evidence import child_environment, execution_identity as _execution_identity
from .supervision import process_supervisor


DEFAULT_OUTPUT_BYTES = 1_000_000
TERMINATION_SECONDS = 2
OBSERVATION_INTERVAL_SECONDS = 1.0
_EXECUTION_LOCK = threading.Lock()

ProgressCallback = Callable[[dict[str, Any]], None]


class ExecutionError(ProcessError):
    """A bounded command could not produce a check report."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _child_environment() -> dict[str, str]:
    return child_environment(executable=sys.executable)


def execution_identity() -> dict[str, Any]:
    """Return the bounded runtime inputs used to launch consumer checks."""
    return _execution_identity(executable=sys.executable)


_LOCK_OWNERS = threading.local()


@contextmanager
def verification_lock(path: Path) -> Iterator[None]:
    """Prevent concurrent lifecycle requests from dispatching verification twice."""
    lock_path = (
        path.parent / ".verification.lock"
        if os.name == "nt"
        else path.parent
    )
    resolved = lock_path.resolve()
    holders = getattr(_LOCK_OWNERS, "holders", None)
    if holders is None:
        holders = _LOCK_OWNERS.holders = {}

    if resolved in holders:
        holders[resolved] += 1
        try:
            yield
        finally:
            holders[resolved] -= 1
            if holders[resolved] == 0:
                del holders[resolved]
        return

    if os.name == "nt":
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
    else:
        # Lock the run directory itself so replacing a profile lock entry cannot
        # create a second lock inode while this operation is active.
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ProcessError(f"cannot open verification lock: {error}") from error
    locked = False
    try:
        if os.name == "nt":
            opened = os.fstat(descriptor)
            current = os.stat(lock_path)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProcessError("verification lock was replaced while opening")
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        if os.name == "nt":
            current = os.stat(lock_path)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProcessError("verification lock was replaced while acquiring")
    except OSError as error:
        os.close(descriptor)
        raise ProcessError("verification for this profile is already running") from error
    except ProcessError:
        os.close(descriptor)
        raise
    holders[resolved] = 1
    try:
        yield
    finally:
        del holders[resolved]
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


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


def _progress_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _notify_progress(
    callback: ProgressCallback | None,
    event: dict[str, Any],
) -> None:
    """Notify an optional observer without changing command outcome semantics."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        # Observability is deliberately best effort. A broken consumer-side
        # renderer must not turn a command result into a different result.
        return


def _progress_event(
    *,
    check: dict[str, Any],
    started: float,
    timeout: float,
    phase: str,
    status: str,
    runner_active: bool,
    runner_responsive: bool,
    last_observed_at: str | None,
    output_bytes: int,
) -> dict[str, Any]:
    """Return the closed, non-content execution status projection."""
    return {
        "check": check["id"],
        "elapsedMs": max(0, int((time.monotonic() - started) * 1000)),
        "lastObservedAt": last_observed_at or _progress_timestamp(),
        "outputBytes": max(0, output_bytes),
        "phase": phase,
        "progress": "unknown" if phase == "running" else "not-running",
        "runnerActive": runner_active,
        "runnerResponsive": runner_responsive,
        "status": status,
        "timeoutSeconds": timeout,
    }


def _run_check(
    project_root: Path,
    check: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    command = check["run"]
    if not isinstance(command, list) or not command:
        raise ProcessError(f"check {check.get('id')!r} has no command")
    working_directory = project_root / check.get("cwd", ".")
    try:
        working_directory = working_directory.resolve(strict=True)
        working_directory.relative_to(project_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ExecutionError(
            f"check {check['id']}: working directory escapes project root",
            code="invalid-working-directory",
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
        raise ExecutionError(
            f"check {check['id']}: cannot start command: {error}",
            code="spawn-failed",
        ) from error
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
    cleanup_failed = False
    deadline = started + timeout
    next_progress = started
    runner_responsive = False
    last_observed_at: str | None = None
    try:
        while process.poll() is None:
            supervisor.observe(process)
            runner_responsive = True
            last_observed_at = _progress_timestamp()
            now = time.monotonic()
            if now >= next_progress:
                _notify_progress(
                    progress_callback,
                    _progress_event(
                        check=check,
                        started=started,
                        timeout=timeout,
                        phase="running",
                        status="running",
                        runner_active=process.poll() is None,
                        runner_responsive=runner_responsive,
                        last_observed_at=last_observed_at,
                        output_bytes=stdout_digest.bytes + stderr_digest.bytes,
                    ),
                )
                next_progress = now + OBSERVATION_INTERVAL_SECONDS
            if budget.exceeded.is_set():
                output_exceeded = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_SECONDS
                )
                descendants = descendants or cleanup.descendants_found
                cleanup_error = cleanup.error
                cleanup_failed = cleanup_failed or cleanup.error is not None
                break
            if time.monotonic() >= deadline:
                timed_out = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_SECONDS
                )
                descendants = descendants or cleanup.descendants_found
                cleanup_error = cleanup.error
                cleanup_failed = cleanup_failed or cleanup.error is not None
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
            cleanup_failed = cleanup_failed or cleanup.error is not None or not cleanup.bounded
            cleanup_error = cleanup_error or (None if cleanup.bounded else "unbounded cleanup")
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
        and not stream_failed
    )
    result = {
        "id": check["id"],
        "status": "passed" if passed else "failed",
        "exitCode": exit_code,
        "timedOut": timed_out,
        "outputExceeded": output_exceeded,
        "descendantsTerminated": descendants,
        "streamFailed": stream_failed,
        "cleanupFailed": cleanup_failed,
        "durationMs": int((time.monotonic() - started) * 1000),
        "stdout": stdout_digest.summary(),
        "stderr": stderr_digest.summary(),
    }
    _notify_progress(
        progress_callback,
        _progress_event(
            check=check,
            started=started,
            timeout=timeout,
            phase="completed",
            status=result["status"],
            runner_active=False,
            runner_responsive=runner_responsive,
            last_observed_at=last_observed_at,
            output_bytes=stdout_digest.bytes + stderr_digest.bytes,
        ),
    )
    return result


def run_check(
    project_root: Path,
    check: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    with _EXECUTION_LOCK:
        return _run_check(
            project_root,
            check,
            progress_callback=progress_callback,
        )


def run_profile(
    project_root: Path,
    project: dict[str, Any],
    profile: str,
    *,
    check_position: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    configured_checks = project["profiles"].get(profile)
    if configured_checks is None:
        raise ProcessError(f"unknown verification profile: {profile}")
    if check_position is None:
        checks = configured_checks
        positions = range(1, len(checks) + 1)
    else:
        if check_position < 1 or check_position > len(configured_checks):
            raise ProcessError(
                f"profile {profile} has no check at position: {check_position}"
            )
        checks = [configured_checks[check_position - 1]]
        positions = [check_position]
    started = time.monotonic()
    reports: list[dict[str, Any]] = []
    failed_position: int | None = None
    for position, check in zip(positions, checks, strict=True):
        def observe(event: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            progress_event = dict(event)
            progress_event["profile"] = profile
            progress_event["position"] = position
            progress_callback(progress_event)

        if progress_callback is None:
            report = run_check(project_root, check)
        else:
            report = run_check(
                project_root,
                check,
                progress_callback=observe,
            )
        reports.append(report)
        if report["status"] != "passed":
            failed_position = position
            break
    result = {
        "profile": profile,
        "status": "passed" if len(reports) == len(checks) and all(
            report["status"] == "passed" for report in reports
        ) else "failed",
        "durationMs": 0,
        "scope": (
            {"kind": "profile"}
            if check_position is None
            else {
                "kind": "check",
                "check": checks[0]["id"],
                "position": check_position,
            }
        ),
        "checks": reports,
    }
    result["durationMs"] = int((time.monotonic() - started) * 1000)
    failed = next((report for report in reports if report["status"] == "failed"), None)
    if failed is not None:
        assert failed_position is not None
        if (
            failed["timedOut"]
            or failed["outputExceeded"]
            or failed["streamFailed"]
            or failed.get("cleanupFailed", False)
        ):
            failure_kind = "execution-condition"
        elif failed["exitCode"] != 0:
            failure_kind = "command-failure"
        else:
            failure_kind = "unknown"
        result["diagnostic"] = {
            "kind": "selective-check-reproduction",
            "descriptorVersion": 1,
            "profile": profile,
            "check": failed["id"],
            "position": failed_position,
            "command": [
                "processctl",
                "verify",
                "--profile",
                profile,
                "--check-position",
                str(failed_position),
            ],
            "failureKind": failure_kind,
            "failure": dict(failed),
        }
    return result
