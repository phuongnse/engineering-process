"""Windows Job Object backend for foreground-task supervision."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Mapping

from .helper_launch import isolated_helper_command
from .supervision import CleanupOutcome


_UNSUPPORTED_SHELL_SUFFIXES = {".bat", ".cmd"}


def resolve_windows_application(
    command: str,
    *,
    working_directory: Path,
    environment: Mapping[str, str],
    path_separator: str | None = None,
) -> Path:
    """Resolve a native executable without CreateProcessW's CWD-first search.

    Relative PATH entries are ignored.  A project executable is therefore selected
    only when the command explicitly contains a path, never merely because a
    same-named file exists in the checkout.
    """

    if not command or "\x00" in command:
        raise OSError("command executable is invalid")
    separator = os.pathsep if path_separator is None else path_separator
    supplied = Path(command)
    explicit_path = supplied.is_absolute() or supplied.parent != Path(".")
    suffix = supplied.suffix.casefold()
    if suffix in _UNSUPPORTED_SHELL_SUFFIXES:
        raise OSError(
            f"Windows batch command {command} requires a shell; only native .exe commands are supported"
        )

    candidates: list[Path] = []
    names = (command,) if suffix else (f"{command}.exe",)
    if explicit_path:
        base = supplied if supplied.is_absolute() else working_directory / supplied
        candidates.append(base if suffix else Path(f"{base}.exe"))
    else:
        for raw_directory in environment.get("PATH", "").split(separator):
            if not raw_directory:
                continue
            directory = Path(raw_directory)
            if not directory.is_absolute():
                continue
            candidates.extend(directory / name for name in names)

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.suffix.casefold() == ".exe":
            return resolved
    raise OSError(f"native Windows executable is unavailable: {command}")


class WindowsProcessSupervisor:
    def resolve_application(
        self,
        command: str,
        *,
        working_directory: Path,
        environment: Mapping[str, str],
    ) -> Path:
        return resolve_windows_application(
            command,
            working_directory=working_directory,
            environment=environment,
        )

    def spawn(
        self,
        command: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
    ) -> subprocess.Popen[bytes]:
        application = self.resolve_application(
            command[0],
            working_directory=working_directory,
            environment=environment,
        )
        wrapper = isolated_helper_command(
            "engineering_process._windows_job",
            "--application",
            str(application),
            "--",
            *command,
        )
        return subprocess.Popen(
            wrapper,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=dict(environment),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        if process.poll() is not None:
            return CleanupOutcome(bounded=True)
        try:
            process.terminate()
            process.wait(timeout=grace_seconds)
            return CleanupOutcome(bounded=True)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                return CleanupOutcome(
                    bounded=False,
                    error="Windows Job Object wrapper survived bounded termination",
                )
        return CleanupOutcome(bounded=True)

    def finalize(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        del process, grace_seconds
        # The helper does not return until the target exits and its Job Object has
        # no live descendants.  Cleanup failures are represented by its exit code.
        return CleanupOutcome(bounded=True)


WINDOWS_SUPERVISOR = WindowsProcessSupervisor()
