from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .supervision import process_supervisor


READ_CHUNK_BYTES = 64 * 1024
POLL_SECONDS = 0.05
TERMINATION_GRACE_SECONDS = 1.0
MAX_INPUT_BYTES = 1_000_000


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_exceeded: bool
    descendants_found: bool
    input_error: bool
    cleanup_error: str | None


def _drain(
    stream,
    capture: dict[str, object],
    *,
    stream_limit: int,
    budget: dict[str, object],
    abort: threading.Event,
) -> None:
    try:
        while chunk := stream.read(READ_CHUNK_BYTES):
            data = capture["data"]
            assert isinstance(data, bytearray)
            lock = budget["lock"]
            assert hasattr(lock, "__enter__")
            with lock:
                stream_remaining = max(0, stream_limit - len(data))
                captured = budget["captured"]
                total_limit = budget["limit"]
                assert isinstance(captured, int)
                assert isinstance(total_limit, int)
                total_remaining = max(0, total_limit - captured)
                admitted = chunk[: min(stream_remaining, total_remaining)]
                data.extend(admitted)
                budget["captured"] = captured + len(admitted)
                count = capture["count"]
                total = budget["count"]
                assert isinstance(count, int)
                assert isinstance(total, int)
                capture["count"] = count + len(chunk)
                budget["count"] = total + len(chunk)
                if (
                    count + len(chunk) > stream_limit
                    or total + len(chunk) > total_limit
                ):
                    abort.set()
                    break
    except (OSError, ValueError) as error:
        capture["error"] = str(error)
        abort.set()
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _feed(stream, content: bytes, capture: dict[str, object]) -> None:
    try:
        remaining = memoryview(content)
        while remaining:
            written = stream.write(remaining[:READ_CHUNK_BYTES])
            if written is None or written <= 0:
                raise OSError("command stdin accepted no bytes")
            remaining = remaining[written:]
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as error:
        capture["error"] = str(error)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_bounded_process(
    command: Sequence[str],
    *,
    working_directory: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_stream_bytes: int,
    max_total_bytes: int,
    input_bytes: bytes | None = None,
) -> BoundedProcessResult:
    if timeout_seconds <= 0:
        raise ValueError("command timeout must be positive")
    if max_stream_bytes < 1 or max_total_bytes < max_stream_bytes:
        raise ValueError("command output limits are invalid")
    if input_bytes is not None and len(input_bytes) > MAX_INPUT_BYTES:
        raise ValueError(f"command stdin exceeds {MAX_INPUT_BYTES} bytes")

    supervisor = process_supervisor()
    process = supervisor.spawn(
        tuple(command),
        working_directory=working_directory,
        environment=environment,
        pipe_stdin=input_bytes is not None,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    if input_bytes is not None:
        assert process.stdin is not None

    stdout_capture: dict[str, object] = {
        "data": bytearray(),
        "count": 0,
        "error": None,
    }
    stderr_capture: dict[str, object] = {
        "data": bytearray(),
        "count": 0,
        "error": None,
    }
    budget: dict[str, object] = {
        "captured": 0,
        "count": 0,
        "limit": max_total_bytes,
        "lock": threading.Lock(),
    }
    abort = threading.Event()
    threads = [
        threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_capture),
            kwargs={
                "stream_limit": max_stream_bytes,
                "budget": budget,
                "abort": abort,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_capture),
            kwargs={
                "stream_limit": max_stream_bytes,
                "budget": budget,
                "abort": abort,
            },
            daemon=True,
        ),
    ]
    input_capture: dict[str, object] = {"error": None}
    if input_bytes is not None:
        threads.append(
            threading.Thread(
                target=_feed,
                args=(process.stdin, input_bytes, input_capture),
                daemon=True,
            )
        )
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    output_exceeded = False
    descendants_found = False
    cleanup_error: str | None = None
    try:
        while process.poll() is None:
            if abort.is_set():
                output_exceeded = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_GRACE_SECONDS
                )
                descendants_found = (
                    descendants_found or cleanup.descendants_found
                )
                if not cleanup.bounded or cleanup.error is not None:
                    cleanup_error = cleanup.error or "command cleanup was not bounded"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_GRACE_SECONDS
                )
                descendants_found = (
                    descendants_found or cleanup.descendants_found
                )
                if not cleanup.bounded or cleanup.error is not None:
                    cleanup_error = cleanup.error or "command cleanup was not bounded"
                break
            try:
                process.wait(timeout=min(POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        supervisor.terminate(process, grace_seconds=TERMINATION_GRACE_SECONDS)
        raise
    finally:
        if process.poll() is not None:
            cleanup = supervisor.finalize(
                process, grace_seconds=TERMINATION_GRACE_SECONDS
            )
            descendants_found = descendants_found or cleanup.descendants_found
            if not cleanup.bounded or cleanup.error is not None:
                cleanup_error = cleanup.error or "command cleanup was not bounded"
        for thread in threads:
            thread.join(timeout=TERMINATION_GRACE_SECONDS)
        if any(thread.is_alive() for thread in threads):
            cleanup_error = "command retained I/O streams after bounded cleanup"

    if stdout_capture["error"] is not None or stderr_capture["error"] is not None:
        cleanup_error = cleanup_error or "command output capture failed"
    return BoundedProcessResult(
        returncode=process.returncode,
        stdout=bytes(stdout_capture["data"]),
        stderr=bytes(stderr_capture["data"]),
        timed_out=timed_out,
        output_exceeded=output_exceeded or abort.is_set(),
        descendants_found=descendants_found,
        input_error=input_capture["error"] is not None,
        cleanup_error=cleanup_error,
    )
