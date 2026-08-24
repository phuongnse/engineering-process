from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.diagnostics import (
    classify_diagnostics,
    diagnostic_failure_message,
)
from engineering_process.bounded_process import run_bounded_process


PUBLIC_INDEX = "https://pypi.org/simple"
MAX_LOCK_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 1_000_000
ATTEMPT_TIMEOUT_SECONDS = 300
ATTEMPT_POLL_SECONDS = 0.05
TERMINATION_TIMEOUT_SECONDS = 5.0
READ_CHUNK_BYTES = 64 * 1024
BACKOFF_SECONDS = (10, 20, 40, 80, 160)
PIN_PATTERN = re.compile(
    r"(?m)^engineering-process=="
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.-]+)?)(?:[ \t]+\\)?$"
)


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attempt:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False
    descendants_terminated: bool = False


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._parts: dict[str, bytearray] = {
            "stdout": bytearray(),
            "stderr": bytearray(),
        }
        self.exceeded = threading.Event()

    def add(self, stream: str, content: bytes) -> None:
        with self._lock:
            current = sum(len(part) for part in self._parts.values())
            remaining = max(0, self._limit - current)
            if remaining:
                self._parts[stream].extend(content[:remaining])
            if len(content) > remaining:
                self.exceeded.set()

    def value(self, stream: str) -> bytes:
        with self._lock:
            return bytes(self._parts[stream])


AttemptRunner = Callable[[Sequence[str], Path, dict[str, str]], Attempt]


def _contained_lock(project_root: Path, requirements_lock: Path) -> Path:
    lexical_root = Path(os.path.abspath(project_root))
    root = project_root.resolve(strict=True)
    candidate = requirements_lock
    if not candidate.is_absolute():
        candidate = lexical_root / candidate
    else:
        candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(lexical_root)
    except ValueError as error:
        raise InstallError("requirements lock escapes the project root") from error
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InstallError(f"requirements lock path must not contain symlinks: {current}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise InstallError(f"cannot resolve requirements lock: {error}") from error
    if not resolved.is_file():
        raise InstallError("requirements lock must be a regular file")
    return resolved


def _read_pin(lock_path: Path) -> str:
    try:
        if lock_path.stat().st_size > MAX_LOCK_BYTES:
            raise InstallError("requirements lock exceeds the size limit")
        content = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"cannot read requirements lock: {error}") from error
    matches = list(PIN_PATTERN.finditer(content))
    if len(matches) != 1:
        raise InstallError(
            "requirements lock must contain exactly one exact engineering-process pin"
        )
    if "--only-binary :all:" not in content:
        raise InstallError("requirements lock must enforce --only-binary :all:")
    return matches[0].group("version")


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PIP_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    return environment


def pip_command(
    lock_path: Path, python_executable: str | Path = sys.executable
) -> tuple[str, ...]:
    return (
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--index-url",
        PUBLIC_INDEX,
        "--require-hashes",
        "-r",
        str(lock_path),
    )


def _drain(
    stream: object,
    label: str,
    capture: _BoundedOutput,
) -> None:
    try:
        while chunk := stream.read(READ_CHUNK_BYTES):  # type: ignore[attr-defined]
            capture.add(label, chunk)
    finally:
        stream.close()  # type: ignore[attr-defined]


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


def _terminate_posix_group(process_group: int) -> bool:
    if not _process_group_exists(process_group):
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as error:
        if _wait_for_process_group(process_group, TERMINATION_TIMEOUT_SECONDS):
            return True
        raise InstallError(
            "pip process group could not be signaled during bounded termination"
        ) from error
    if _wait_for_process_group(process_group, TERMINATION_TIMEOUT_SECONDS):
        return True
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError as error:
        if _wait_for_process_group(process_group, TERMINATION_TIMEOUT_SECONDS):
            return True
        raise InstallError(
            "pip process group could not be killed during bounded termination"
        ) from error
    if not _wait_for_process_group(process_group, TERMINATION_TIMEOUT_SECONDS):
        raise InstallError("pip process group survived bounded termination")
    return True


def _terminate_tree(process: subprocess.Popen[bytes]) -> bool:
    if os.name == "nt":
        if process.poll() is not None:
            return False
        process.terminate()
        try:
            process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
        return True

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=TERMINATION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise InstallError(
                    "pip root process survived bounded termination"
                ) from error
    return _terminate_posix_group(process.pid)


def _run_attempt(
    command: Sequence[str], working_directory: Path, environment: dict[str, str]
) -> Attempt:
    if os.name == "nt":
        try:
            result = run_bounded_process(
                command,
                working_directory=working_directory,
                environment=environment,
                timeout_seconds=ATTEMPT_TIMEOUT_SECONDS,
                max_stream_bytes=MAX_OUTPUT_BYTES,
                max_total_bytes=MAX_OUTPUT_BYTES,
            )
        except (OSError, ValueError) as error:
            raise InstallError(f"cannot execute bounded Windows pip: {error}") from error
        if result.cleanup_error is not None:
            raise InstallError(f"Windows pip cleanup failed: {result.cleanup_error}")
        if result.input_error:
            raise InstallError("Windows pip reported an unexpected input failure")
        return Attempt(
            result.returncode if result.returncode is not None else -1,
            result.stdout,
            result.stderr,
            timed_out=result.timed_out,
            output_exceeded=result.output_exceeded,
            descendants_terminated=result.descendants_found,
        )

    capture = _BoundedOutput(MAX_OUTPUT_BYTES)
    executable = list(command)
    options: dict[str, object] = {
        "cwd": working_directory,
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    options["start_new_session"] = True
    try:
        process = subprocess.Popen(executable, **options)
    except OSError as error:
        raise InstallError(f"cannot execute pip: {error}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=_drain,
        args=(process.stdout, "stdout", capture),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(process.stderr, "stderr", capture),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + ATTEMPT_TIMEOUT_SECONDS
    timed_out = False
    output_exceeded = False
    try:
        while process.poll() is None:
            if capture.exceeded.is_set():
                output_exceeded = True
                _terminate_tree(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_tree(process)
                break
            try:
                process.wait(timeout=min(ATTEMPT_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_tree(process)
        raise
    descendants_terminated = _terminate_tree(process)
    stdout_thread.join(timeout=TERMINATION_TIMEOUT_SECONDS)
    stderr_thread.join(timeout=TERMINATION_TIMEOUT_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_tree(process)
        raise InstallError("pip output readers did not terminate")
    return Attempt(
        process.returncode if process.returncode is not None else -1,
        capture.value("stdout"),
        capture.value("stderr"),
        timed_out=timed_out,
        output_exceeded=output_exceeded or capture.exceeded.is_set(),
        descendants_terminated=descendants_terminated,
    )


def retryable_exact_version_absence(attempt: Attempt, version: str) -> bool:
    if (
        attempt.returncode == 0
        or attempt.timed_out
        or attempt.output_exceeded
    ):
        return False
    output = (attempt.stdout + b"\n" + attempt.stderr).decode(
        "utf-8", errors="replace"
    )
    requirement = f"engineering-process=={version}"
    return (
        f"Could not find a version that satisfies the requirement {requirement}"
        in output
        and f"No matching distribution found for {requirement}" in output
    )


def install_process_runtime(
    project_root: Path,
    requirements_lock: Path,
    *,
    python_executable: str | Path = sys.executable,
    runner: AttemptRunner = _run_attempt,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    root = project_root.resolve(strict=True)
    lock_path = _contained_lock(project_root, requirements_lock)
    version = _read_pin(lock_path)
    executable = Path(python_executable)
    if not executable.is_file():
        raise InstallError("Python executable must be an existing file")
    command = pip_command(lock_path, executable)
    environment = _environment()
    total_attempts = len(BACKOFF_SECONDS) + 1
    for index in range(total_attempts):
        attempt_number = index + 1
        print(
            f"Installing engineering-process {version} "
            f"(attempt {attempt_number}/{total_attempts})",
            flush=True,
        )
        attempt = runner(command, root, environment)
        if attempt.stdout:
            sys.stdout.buffer.write(attempt.stdout)
            sys.stdout.buffer.flush()
        if attempt.stderr:
            sys.stderr.buffer.write(attempt.stderr)
            sys.stderr.buffer.flush()
        if attempt.timed_out:
            raise InstallError(
                f"pip attempt exceeded {ATTEMPT_TIMEOUT_SECONDS} seconds"
            )
        if attempt.output_exceeded:
            raise InstallError(
                f"pip attempt output exceeded {MAX_OUTPUT_BYTES} bytes"
            )
        if attempt.descendants_terminated:
            raise InstallError(
                "pip attempt left descendant processes; they were terminated"
            )
        if attempt.returncode == 0:
            diagnostics = classify_diagnostics(
                stdout=attempt.stdout,
                stderr=attempt.stderr,
            )
            diagnostic_error = diagnostic_failure_message(
                diagnostics, subject="pip install"
            )
            if diagnostic_error is not None:
                raise InstallError(diagnostic_error)
            return
        if not retryable_exact_version_absence(attempt, version):
            raise InstallError(
                f"pip failed with non-retryable exit code {attempt.returncode}"
            )
        if index == len(BACKOFF_SECONDS):
            raise InstallError(
                f"engineering-process {version} did not become visible "
                f"after {total_attempts} attempts"
            )
        delay = BACKOFF_SECONDS[index]
        print(
            f"Exact public version is not visible yet; retrying in {delay} seconds.",
            flush=True,
        )
        sleeper(delay)
    raise AssertionError("unreachable install loop")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--requirements-lock",
        type=Path,
        default=Path("requirements/process.txt"),
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    arguments = parser.parse_args()
    try:
        install_process_runtime(
            arguments.project_root,
            arguments.requirements_lock,
            python_executable=arguments.python,
        )
    except (InstallError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
