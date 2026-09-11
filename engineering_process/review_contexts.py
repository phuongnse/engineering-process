"""Recorded reviewer-context ownership within one Git repository."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterator

from .contracts import (
    MAX_JSON_BYTES,
    ProcessError,
    digest_json,
    parse_json_bytes,
    validate_document,
)
from .distribution import schemas_root


MAX_WORKTREES = 64
MAX_RUN_ENTRIES = 512
MAX_TOTAL_BYTES = 64_000_000
MAX_GIT_BYTES = 256_000
GIT_TIMEOUT_SECONDS = 10
SCAN_TIMEOUT_SECONDS = 30
LOCK_TIMEOUT_SECONDS = 10


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ProcessError("review history exceeds its time limit")


def _git(root: Path, arguments: list[str], *, deadline: float | None = None) -> bytes:
    # These fixed metadata commands cannot invoke a pager or consumer shell command.
    command_deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    if deadline is not None:
        _check_deadline(deadline)
        command_deadline = min(command_deadline, deadline)
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                ["git", "--no-pager", *arguments], cwd=root,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            )
        except OSError as error:
            raise ProcessError(f"cannot inspect review worktrees: {error}") from error
        try:
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > MAX_GIT_BYTES:
                    raise ProcessError("review worktree Git output exceeded its byte limit")
                if time.monotonic() >= command_deadline:
                    raise ProcessError("review worktree Git command timed out")
                time.sleep(0.01)
            output.seek(0)
            content = output.read(MAX_GIT_BYTES + 1)
            if len(content) > MAX_GIT_BYTES:
                raise ProcessError("review worktree Git output exceeded its byte limit")
            if process.returncode:
                detail = content[:2000].decode("utf-8", errors="replace").strip()
                raise ProcessError(f"cannot inspect review worktrees: {detail}")
            if deadline is not None:
                _check_deadline(deadline)
            return content
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _safe_info(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if stat.S_ISLNK(info.st_mode) or reparse:
        raise ProcessError(f"review history must not traverse a link or reparse point: {path}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ProcessError(f"review history has an unsupported file type: {path}")
    return info


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _directory_identity(path: Path) -> tuple[int, ...]:
    info = _safe_info(path, directory=True)
    return (info.st_dev, info.st_ino, info.st_mode)


def _common_directory(root: Path, *, deadline: float | None = None) -> Path:
    result = _git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                  deadline=deadline)
    path = Path(os.fsdecode(result.removesuffix(b"\n")))
    if not path.is_absolute():
        raise ProcessError("Git common directory must be absolute")
    _safe_info(path, directory=True)
    return path.resolve()


@contextmanager
def _review_lock(root: Path) -> Iterator[None]:
    common = _common_directory(root)
    path = common / "engineering-process-review.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        current = _safe_info(path, directory=False)
        if (current.st_dev, current.st_ino, current.st_mode) != (
            opened.st_dev, opened.st_ino, opened.st_mode
        ):
            raise ProcessError("review transaction lock changed while opening")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise ProcessError("review transaction lock timed out") from error
                time.sleep(0.02)
        try:
            current = _safe_info(path, directory=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProcessError("review transaction lock was replaced")
            yield
        finally:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def history_transaction(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def locked(project_root: Path, *args: Any, **kwargs: Any) -> Any:
        try:
            with _review_lock(project_root):
                return function(project_root, *args, **kwargs)
        except OSError as error:
            raise ProcessError(f"cannot inspect or lock lifecycle history: {error}") from error
    return locked


def _worktrees(root: Path, *, deadline: float) -> list[Path]:
    raw = _git(root, ["worktree", "list", "--porcelain", "-z"], deadline=deadline)
    roots = []
    for record in raw.split(b"\0\0"):
        _check_deadline(deadline)
        if not record:
            continue
        fields = record.split(b"\0")
        if not fields[0].startswith(b"worktree "):
            raise ProcessError("Git returned an invalid review worktree record")
        if b"bare" in fields:
            continue
        path = Path(os.fsdecode(fields[0][len(b"worktree "):]))
        if not path.is_absolute():
            raise ProcessError("review worktree paths must be absolute")
        _safe_info(path, directory=True)
        roots.append(path.resolve())
        if len(roots) > MAX_WORKTREES:
            raise ProcessError("review history exceeds its worktree limit")
    if root.resolve() not in roots or len(roots) != len(set(roots)):
        raise ProcessError("Git returned an incomplete or duplicate review worktree inventory")
    return sorted(roots)


def _read_run(path: Path, parents: list[Path], process_root: Path) -> tuple[dict[str, Any], int]:
    before = [_directory_identity(parent) for parent in parents]
    info = _safe_info(path, directory=False)
    if info.st_size > MAX_JSON_BYTES:
        raise ProcessError(f"{path} exceeds {MAX_JSON_BYTES} bytes")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ProcessError(f"review history changed while opening: {path}")
        if before != [_directory_identity(parent) for parent in parents]:
            raise ProcessError(f"review history path changed while opening: {path}")
        content = stream.read(MAX_JSON_BYTES + 1)
        after = _safe_info(path, directory=False)
        if _identity(after) != _identity(info) or len(content) != info.st_size:
            raise ProcessError(f"review history changed while reading: {path}")
    if before != [_directory_identity(parent) for parent in parents]:
        raise ProcessError(f"review history path changed while reading: {path}")
    state = validate_document(
        parse_json_bytes(content, source=str(path)), "run",
        schema_root=schemas_root(process_root), source=str(path),
    )
    if state["changeId"] != path.parent.name:
        raise ProcessError(f"{path}: change identity mismatch")
    return state, len(content)


def recorded_context_conflict(
    project_root: Path, process_root: Path, current: dict[str, Any], context_id: str,
) -> dict[str, str] | None:
    """Return one recorded foreign use; absence only covers the inspected history."""
    deadline = time.monotonic() + SCAN_TIMEOUT_SECONDS
    common = _common_directory(project_root, deadline=deadline)
    roots = _worktrees(project_root, deadline=deadline)
    accepted = (current["changeId"], current["contract"]["digest"])
    total = entries = 0
    for root in roots:
        _check_deadline(deadline)
        if _common_directory(root, deadline=deadline) != common:
            raise ProcessError(f"registered review worktree changed repository: {root}")
        process = root / ".process"
        runs = process / "runs"
        try:
            _safe_info(process, directory=True)
            _safe_info(runs, directory=True)
        except FileNotFoundError:
            _check_deadline(deadline)
            continue
        directories = []
        with os.scandir(runs) as children:
            for child in children:
                entries += 1
                if entries > MAX_RUN_ENTRIES or time.monotonic() >= deadline:
                    raise ProcessError("review history exceeds its entry or time limit")
                directories.append(Path(child.path))
        for directory in sorted(directories):
            _check_deadline(deadline)
            info = directory.lstat()
            if stat.S_ISREG(info.st_mode):
                continue
            _safe_info(directory, directory=True)
            path = directory / "run.json"
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            state, size = _read_run(path, [root, process, runs, directory], process_root)
            _check_deadline(deadline)
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ProcessError("review history exceeds its aggregate byte limit")
            if (state["changeId"], state["contract"]["digest"]) == accepted:
                continue
            actors = [event["actor"] for event in state["history"]]
            actors.extend(item["actor"] for item in state["implementations"])
            if state["reviewAssignment"] is not None:
                actors.append(state["reviewAssignment"]["reviewer"])
            for event in state["history"]:
                if event["event"] == "review-assignment-replaced":
                    actors.append(event["details"]["previousAssignment"]["reviewer"])
            if any(actor["contextId"] == context_id for actor in actors):
                return {
                    "changeId": state["changeId"],
                    "contractDigest": state["contract"]["digest"],
                    "runPath": str(path),
                    "runDigest": digest_json(state),
                }
    if _worktrees(project_root, deadline=deadline) != roots:
        raise ProcessError("review worktree inventory changed during inspection")
    _check_deadline(deadline)
    return None


def require_unreused_context(
    project_root: Path, process_root: Path, state: dict[str, Any], reviewer: dict[str, str],
) -> None:
    if reviewer["kind"] != "agent":
        return
    conflict = recorded_context_conflict(project_root, process_root, state, reviewer["contextId"])
    if conflict is not None:
        raise ProcessError(
            f"reviewer context was used by another change: {conflict['changeId']} "
            f"({conflict['runPath']}); spawn a fresh reviewer for this change"
        )
