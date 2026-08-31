"""POSIX process-group backend for foreground-task supervision."""

from __future__ import annotations

from contextlib import suppress
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
PROCESS_TABLE_TIMEOUT_SECONDS = 10
PROCESS_TABLE_OBSERVATION_INTERVAL_SECONDS = 0.05
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


def _ps_process_table() -> tuple[dict[int, int], str | None]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PROCESS_TABLE_TIMEOUT_SECONDS, check=False, text=True,
        )
    except subprocess.TimeoutExpired:
        return {}, f"process table snapshot timed out after {PROCESS_TABLE_TIMEOUT_SECONDS} seconds"
    except OSError:
        return {}, "process table snapshot could not start"
    if result.returncode != 0:
        return {}, f"process table snapshot exited with status {result.returncode}"
    try:
        rows = (line.split() for line in result.stdout.splitlines())
        table = {int(pid): int(parent) for pid, parent in rows}
    except (TypeError, ValueError):
        return {}, "process table snapshot was malformed"
    if os.getpid() not in table:
        return {}, "process table snapshot was incomplete"
    return table, None


def _process_table() -> tuple[dict[int, int], str | None]:
    table: dict[int, int] = {}
    if Path("/proc").is_dir():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                for line in (entry / "status").read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("PPid:"):
                        table[int(entry.name)] = int(line.split()[1])
                        break
            except (OSError, ValueError, IndexError):
                continue
        return table, None
    return _ps_process_table()


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
                with suppress(OSError):
                    os.waitpid(pid, os.WNOHANG)
                return False
        except (OSError, IndexError):
            pass
    return True


def _process_group_exists(process_group: int) -> bool:
    with suppress(OSError):
        while os.waitpid(-process_group, os.WNOHANG)[0]:
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
        with suppress(OSError):
            os.kill(pid, signal_number)


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
    with suppress(OSError):
        os.killpg(process_group, signal.SIGTERM)
    if _wait_for_process_group(process_group, grace_seconds):
        return CleanupOutcome(bounded=True, descendants_found=True)
    with suppress(OSError):
        os.killpg(process_group, signal.SIGKILL)
    bounded = _wait_for_process_group(process_group, grace_seconds)
    return CleanupOutcome(
        bounded=bounded,
        descendants_found=True,
        error=None if bounded else "command process group exceeded its termination grace period",
    )


def _terminate_root(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        with suppress(OSError):
            os.killpg(process.pid, signal_number)
        try:
            process.wait(timeout=grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            pass
    return False


class PosixProcessSupervisor:
    def __init__(self) -> None:
        self._known_descendants: dict[int, set[int]] = {}
        self._run_ids: dict[int, str] = {}
        self._last_observation: dict[int, float] = {}
        self._observation_errors: dict[int, str] = {}

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
        self._observation_errors.pop(process.pid, None)
        self._observe(process, force=True)
        return process

    def _observe(self, process: subprocess.Popen[bytes], *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_observation.get(process.pid, 0) < PROCESS_TABLE_OBSERVATION_INTERVAL_SECONDS:
            return
        table, error = _process_table()
        self._last_observation[process.pid] = time.monotonic()
        known = self._known_descendants.setdefault(process.pid, set())
        if error is not None:
            self._observation_errors.setdefault(process.pid, error)
        else:
            known.update(_descendants(process.pid, table))
            run_id = self._run_ids.get(process.pid)
            if sys.platform.startswith("linux") and run_id is not None:
                known.update(
                    pid
                    for pid, parent in table.items()
                    if parent == os.getpid()
                    and pid != process.pid
                    and _has_run_id(pid, run_id)
                )

    def observe(self, process: subprocess.Popen[bytes]) -> None:
        self._observe(process, force=False)

    def _terminate_detached(self, process: subprocess.Popen[bytes], grace_seconds: float) -> CleanupOutcome:
        self._observe(process, force=True)
        candidates = self._known_descendants.pop(process.pid, set())
        self._run_ids.pop(process.pid, None)
        self._last_observation.pop(process.pid, None)
        observation_error = self._observation_errors.pop(process.pid, None)
        live = {pid for pid in candidates if _pid_alive(pid)}
        if not live:
            return CleanupOutcome(bounded=observation_error is None, error=observation_error)
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
        descendants_bounded = not any(_pid_alive(pid) for pid in live)
        error = observation_error or (
            None
            if descendants_bounded
            else "detached descendants survived bounded termination"
        )
        return CleanupOutcome(
            bounded=descendants_bounded and error is None,
            descendants_found=True,
            error=error,
        )

    def terminate(
        self,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> CleanupOutcome:
        self._observe(process, force=True)
        if not _terminate_root(process, grace_seconds):
            detached = self._terminate_detached(process, grace_seconds)
            error = "command root process survived bounded process-group termination"
            return CleanupOutcome(
                bounded=False,
                descendants_found=detached.descendants_found,
                error=error + (f"; {detached.error}" if detached.error else ""),
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
        self._observe(process, force=True)
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
