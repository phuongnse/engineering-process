"""Windows Job Object backend for foreground-task supervision."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Mapping

from .helper_launch import isolated_helper_command
from .supervision import CleanupOutcome


_UNSUPPORTED_SHELL_SUFFIXES = {".bat", ".cmd"}
MAX_STATUS_BYTES = 4096
MAX_STATUS_ERROR_CHARACTERS = 1024


def decode_windows_job_status(content: bytes) -> CleanupOutcome:
    if not content or len(content) > MAX_STATUS_BYTES:
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object status is missing or oversized",
        )
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object status is not valid UTF-8 JSON",
        )
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "descendantsFound",
        "cleanupError",
    }:
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object status has an unexpected contract",
        )
    if document["schemaVersion"] != 1 or not isinstance(
        document["descendantsFound"], bool
    ):
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object status fields are invalid",
        )
    cleanup_error = document["cleanupError"]
    if cleanup_error is not None and (
        not isinstance(cleanup_error, str)
        or not cleanup_error
        or cleanup_error != cleanup_error.strip()
        or len(cleanup_error) > MAX_STATUS_ERROR_CHARACTERS
        or "\x00" in cleanup_error
    ):
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object cleanup error is invalid",
        )
    canonical = (
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if content != canonical:
        return CleanupOutcome(
            bounded=False,
            error="Windows Job Object status is not canonical",
        )
    if cleanup_error is not None:
        return CleanupOutcome(
            bounded=False,
            descendants_found=document["descendantsFound"],
            error="Windows Job Object wrapper failed: " + cleanup_error,
        )
    return CleanupOutcome(
        bounded=True,
        descendants_found=document["descendantsFound"],
    )


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
    def __init__(self) -> None:
        self._status_readers: dict[int, int] = {}
        self._forced_termination: set[int] = set()
        self._status_lock = threading.Lock()

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
        pipe_stdin: bool = False,
    ) -> subprocess.Popen[bytes]:
        application = self.resolve_application(
            command[0],
            working_directory=working_directory,
            environment=environment,
        )
        wrapper = isolated_helper_command(
            "engineering_process._windows_job",
            "--status-handle",
            "{status_handle}",
            "--application",
            str(application),
            "--",
            *command,
        )
        read_fd, write_fd = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            import msvcrt

            os.set_inheritable(read_fd, False)
            os.set_inheritable(write_fd, True)
            status_handle = msvcrt.get_osfhandle(write_fd)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [status_handle]}
            wrapper = tuple(
                str(status_handle) if item == "{status_handle}" else item
                for item in wrapper
            )
            process = subprocess.Popen(
                wrapper,
                cwd=working_directory,
                stdin=subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=dict(environment),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                startupinfo=startup,
            )
        finally:
            os.close(write_fd)
            if process is None:
                os.close(read_fd)
        with self._status_lock:
            self._status_readers[process.pid] = read_fd
        return process

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        if process.poll() is not None:
            return CleanupOutcome(bounded=True)
        with self._status_lock:
            self._forced_termination.add(process.pid)
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
        del grace_seconds
        with self._status_lock:
            read_fd = self._status_readers.pop(process.pid, None)
            forced = process.pid in self._forced_termination
            self._forced_termination.discard(process.pid)
        if read_fd is None:
            return CleanupOutcome(
                bounded=False,
                error="Windows Job Object status pipe is unavailable",
            )
        content = bytearray()
        try:
            while True:
                chunk = os.read(read_fd, MAX_STATUS_BYTES + 1 - len(content))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_STATUS_BYTES:
                    break
        except OSError as error:
            return CleanupOutcome(
                bounded=False,
                error=f"Windows Job Object status pipe read failed: {error}",
            )
        finally:
            os.close(read_fd)
        if not content and forced:
            return CleanupOutcome(bounded=True)
        return decode_windows_job_status(bytes(content))


WINDOWS_SUPERVISOR = WindowsProcessSupervisor()
