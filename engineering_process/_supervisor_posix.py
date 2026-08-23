"""POSIX process-group backend for foreground-task supervision."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Mapping

from .supervision import CleanupOutcome, NATURAL_DRAIN_GRACE_MILLISECONDS


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.02)
    return not _process_group_exists(process_group)


def _terminate_group(process_group: int, grace_seconds: float) -> CleanupOutcome:
    if not _process_group_exists(process_group):
        return CleanupOutcome(bounded=True)
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return CleanupOutcome(bounded=True, descendants_found=True)
    except OSError:
        pass
    if _wait_for_process_group(process_group, grace_seconds):
        return CleanupOutcome(bounded=True, descendants_found=True)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return CleanupOutcome(bounded=True, descendants_found=True)
    except OSError:
        pass
    bounded = _wait_for_process_group(process_group, grace_seconds)
    return CleanupOutcome(
        bounded=bounded,
        descendants_found=True,
        error=(
            None
            if bounded
            else "command process group could not be terminated within the bounded grace period"
        ),
    )


class PosixProcessSupervisor:
    def resolve_application(
        self,
        command: str,
        *,
        working_directory: Path,
        environment: Mapping[str, str],
    ) -> Path:
        if not command or "\x00" in command:
            raise OSError("command executable is invalid")
        supplied = Path(command)
        if supplied.is_absolute() or supplied.parent != Path("."):
            candidate = supplied if supplied.is_absolute() else working_directory / supplied
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise OSError(f"executable is unavailable: {command}") from error
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise OSError(f"executable is unavailable: {command}")
            return resolved
        for raw_directory in environment.get("PATH", "").split(os.pathsep):
            directory = Path(raw_directory or ".")
            if not directory.is_absolute():
                directory = working_directory / directory
            candidate = directory / command
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file() and os.access(resolved, os.X_OK):
                return resolved
        raise OSError(f"executable is unavailable: {command}")

    def spawn(
        self,
        command: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
        pipe_stdin: bool = False,
    ) -> subprocess.Popen[bytes]:
        application = self.resolve_application(
            command[0],
            working_directory=working_directory,
            environment=environment,
        )
        return subprocess.Popen(
            command,
            executable=application,
            cwd=working_directory,
            stdin=subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=dict(environment),
            start_new_session=True,
        )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                return CleanupOutcome(
                    bounded=False,
                    error="command root process survived bounded process-group termination",
                )
        descendants = _terminate_group(process.pid, grace_seconds)
        return CleanupOutcome(bounded=descendants.bounded, error=descendants.error)

    def finalize(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        # The group id remains valid while any descendant from the owned session is
        # alive, even after the original process has exited. Give descendants that
        # were synchronously asked to stop one short bounded interval to disappear
        # naturally before classifying them as abandoned background work.
        if not _process_group_exists(process.pid):
            return CleanupOutcome(bounded=True)
        natural_drain_seconds = min(
            grace_seconds,
            NATURAL_DRAIN_GRACE_MILLISECONDS / 1000,
        )
        if natural_drain_seconds > 0 and _wait_for_process_group(
            process.pid,
            natural_drain_seconds,
        ):
            return CleanupOutcome(bounded=True)
        return _terminate_group(process.pid, grace_seconds)


POSIX_SUPERVISOR = PosixProcessSupervisor()
