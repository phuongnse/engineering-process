"""POSIX process-group backend for foreground-task supervision."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import Mapping

from .supervision import CleanupOutcome, NATURAL_DRAIN_GRACE_MILLISECONDS


PR_SET_CHILD_SUBREAPER = 36
RUN_ID_ENVIRONMENT_VARIABLE = "ENGINEERING_PROCESS_RUN_ID"
_SUBREAPER_ENABLED = False


def _enable_subreaper() -> None:
    global _SUBREAPER_ENABLED
    if _SUBREAPER_ENABLED or not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "cannot enable Linux child subreaper containment")
    _SUBREAPER_ENABLED = True


def _process_table() -> dict[int, int]:
    table: dict[int, int] = {}
    if Path("/proc").is_dir():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                for line in (entry / "status").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if line.startswith("PPid:"):
                        table[int(entry.name)] = int(line.split()[1])
                        break
            except (OSError, ValueError, IndexError):
                continue
        return table
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            try:
                pid, parent = (int(value) for value in line.split())
            except (ValueError, TypeError):
                continue
            table[pid] = parent
    return table


def _descendants(root: int, table: Mapping[int, int]) -> set[int]:
    found: set[int] = set()
    frontier = {root}
    while frontier:
        children = {
            pid for pid, parent in table.items() if parent in frontier and pid not in found
        }
        found.update(children)
        frontier = children
    return found


def _has_run_id(pid: int, run_id: str) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    marker = f"{RUN_ID_ENVIRONMENT_VARIABLE}={run_id}".encode("utf-8")
    try:
        return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/stat")
    if status.is_file():
        try:
            state = status.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
            if state == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except OSError:
                    pass
                return False
        except (OSError, IndexError):
            pass
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        while os.waitpid(-process_group, os.WNOHANG)[0]:
            pass
    except OSError:
        pass
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_processes(processes: set[int], signal_number: int) -> None:
    for pid in processes:
        try:
            os.kill(pid, signal_number)
        except OSError:
            pass


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
        error=None if bounded else "command process group exceeded its termination grace period",
    )


class PosixProcessSupervisor:
    def __init__(self) -> None:
        self._known_descendants: dict[int, set[int]] = {}
        self._run_ids: dict[int, str] = {}

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
        _enable_subreaper()
        application = self.resolve_application(
            command[0],
            working_directory=working_directory,
            environment=environment,
        )
        run_id = secrets.token_hex(32)
        child_environment = dict(environment)
        child_environment[RUN_ID_ENVIRONMENT_VARIABLE] = run_id
        process = subprocess.Popen(
            command,
            executable=application,
            cwd=working_directory,
            stdin=subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=child_environment,
            start_new_session=True,
        )
        self._known_descendants[process.pid] = set()
        self._run_ids[process.pid] = run_id
        self.observe(process)
        return process

    def observe(self, process: subprocess.Popen[bytes]) -> None:
        table = _process_table()
        known = self._known_descendants.setdefault(process.pid, set())
        known.update(_descendants(process.pid, table))
        run_id = self._run_ids.get(process.pid)
        if sys.platform.startswith("linux") and run_id is not None:
            adopted = {
                pid
                for pid, parent in table.items()
                if (
                    parent == os.getpid()
                    and pid != process.pid
                    and _has_run_id(pid, run_id)
                )
            }
            known.update(adopted)

    def _terminate_detached(
        self,
        process: subprocess.Popen[bytes],
        grace_seconds: float,
    ) -> CleanupOutcome:
        self.observe(process)
        candidates = self._known_descendants.pop(process.pid, set())
        self._run_ids.pop(process.pid, None)
        live = {pid for pid in candidates if _pid_alive(pid)}
        if not live:
            return CleanupOutcome(bounded=True)
        _signal_processes(live, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in live):
            time.sleep(0.02)
        survivors = {pid for pid in live if _pid_alive(pid)}
        _signal_processes(survivors, signal.SIGKILL)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and any(
            _pid_alive(pid) for pid in survivors
        ):
            time.sleep(0.02)
        for pid in live:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        bounded = not any(_pid_alive(pid) for pid in live)
        return CleanupOutcome(
            bounded=bounded,
            descendants_found=True,
            error=None if bounded else "detached descendants survived bounded termination",
        )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        self.observe(process)
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
                detached = self._terminate_detached(process, grace_seconds)
                return CleanupOutcome(
                    bounded=False,
                    descendants_found=detached.descendants_found,
                    error=(
                        "command root process survived bounded process-group termination"
                        + (f"; {detached.error}" if detached.error else "")
                    ),
                )
        descendants = _terminate_group(process.pid, grace_seconds)
        detached = self._terminate_detached(process, grace_seconds)
        return CleanupOutcome(
            bounded=descendants.bounded and detached.bounded,
            descendants_found=(
                descendants.descendants_found or detached.descendants_found
            ),
            error=descendants.error or detached.error,
        )

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
        self.observe(process)
        if not _process_group_exists(process.pid):
            return self._terminate_detached(process, grace_seconds)
        natural_drain_seconds = min(
            grace_seconds,
            NATURAL_DRAIN_GRACE_MILLISECONDS / 1000,
        )
        if natural_drain_seconds > 0 and _wait_for_process_group(
            process.pid,
            natural_drain_seconds,
        ):
            return self._terminate_detached(process, grace_seconds)
        grouped = _terminate_group(process.pid, grace_seconds)
        detached = self._terminate_detached(process, grace_seconds)
        return CleanupOutcome(
            bounded=grouped.bounded and detached.bounded,
            descendants_found=grouped.descendants_found or detached.descendants_found,
            error=grouped.error or detached.error,
        )


POSIX_SUPERVISOR = PosixProcessSupervisor()
