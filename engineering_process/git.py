from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import ContractError
from .supervision import process_supervisor


GIT_STDERR_LIMIT = 16_384
GIT_STDIN_LIMIT = 1_000_000
GIT_TERMINATION_GRACE_SECONDS = 2.0
MAX_PORTABLE_PATH_LENGTH = 1_024
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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


def _feed_input(stream, content: bytes, capture: dict[str, object]) -> None:
    try:
        remaining = memoryview(content)
        while remaining:
            written = stream.write(remaining[: 64 * 1024])
            if written is None or written <= 0:
                raise OSError("Git stdin accepted no bytes")
            remaining = remaining[written:]
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as error:
        capture["error"] = str(error)
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
    input_bytes: bytes | None = None,
) -> GitResult:
    if timeout_seconds <= 0:
        raise ContractError(f"{label}: Git time budget is exhausted")
    if input_bytes is not None and len(input_bytes) > GIT_STDIN_LIMIT:
        raise ContractError(f"{label}: Git stdin exceeds {GIT_STDIN_LIMIT} bytes")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    supervisor = process_supervisor()
    try:
        process = supervisor.spawn(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *arguments,
            ),
            working_directory=root,
            environment=environment,
            pipe_stdin=input_bytes is not None,
        )
    except (OSError, ValueError) as error:
        raise ContractError(f"{label}: cannot start Git: {error}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    if input_bytes is not None:
        assert process.stdin is not None
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
    threads = [
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
    ]
    input_capture: dict[str, object] = {"error": None}
    if input_bytes is not None:
        threads.append(
            threading.Thread(
                target=_feed_input,
                args=(process.stdin, input_bytes, input_capture),
                daemon=True,
            )
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
        raise ContractError(f"{label}: Git retained I/O streams after cleanup")
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
    if input_capture["error"] is not None and process.returncode == 0:
        raise ContractError(f"{label}: could not write bounded Git input")
    return GitResult(
        returncode=process.returncode,
        stdout=bytes(stdout_capture["data"]),
        stderr=bytes(stderr_capture["data"]),
    )


def portable_git_path(encoded: bytes, *, label: str) -> str:
    try:
        path = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{label}: Git paths must use UTF-8") from error
    candidate = PurePosixPath(path)
    segments = path.split("/")
    if (
        not path
        or len(path) > MAX_PORTABLE_PATH_LENGTH
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or any(character in '<>:"|?*' for character in path)
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != path
        or any(not segment or segment.endswith((" ", ".")) for segment in segments)
        or any(
            segment.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for segment in segments
        )
    ):
        raise ContractError(f"{label}: Git returned a non-portable path: {path!r}")
    return path


def tracked_index_paths(
    root: Path,
    *,
    label: str,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_paths: int,
) -> list[bytes]:
    result = run_git(
        root,
        ["ls-files", "-v", "-z", "--cached", "--"],
        label=label,
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=max_stdout_bytes,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"{label}: git ls-files failed" + (f": {detail}" if detail else "")
        )
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) > max_paths:
        raise ContractError(f"{label}: tracked path count exceeds {max_paths}")
    paths: list[bytes] = []
    for record in records:
        if len(record) < 3 or record[1:2] != b" ":
            raise ContractError(f"{label}: Git returned an invalid index record")
        tag = record[:1]
        encoded_path = record[2:]
        path = portable_git_path(encoded_path, label=label)
        if tag == b"S" or (b"a" <= tag <= b"z"):
            raise ContractError(
                f"{label}: tracked path uses a hidden index flag; clear "
                f"skip-worktree/assume-unchanged before continuing: {path}"
            )
        paths.append(encoded_path)
    return paths


def remaining_seconds(deadline: float, *, label: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError(f"{label}: time budget is exhausted")
    return remaining
