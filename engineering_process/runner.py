from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Check, ContractError, Project


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def _source_state(root: Path) -> dict[str, Any]:
    status_result = _git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if status_result.returncode != 0:
        return {"checkpoint": None, "dirty": None, "fingerprint": None}

    head_result = _git(root, ["rev-parse", "HEAD"])
    checkpoint = (
        head_result.stdout.decode("ascii").strip()
        if head_result.returncode == 0
        else None
    )
    diff = b""
    if checkpoint is not None:
        diff_result = _git(
            root,
            ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
        )
        if diff_result.returncode != 0:
            return {"checkpoint": checkpoint, "dirty": True, "fingerprint": None}
        diff = diff_result.stdout

    untracked_result = _git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if untracked_result.returncode != 0:
        return {
            "checkpoint": checkpoint,
            "dirty": bool(status_result.stdout),
            "fingerprint": None,
        }

    digest = hashlib.sha256()
    digest.update(b"checkpoint\0")
    digest.update((checkpoint or "").encode("ascii"))
    digest.update(b"\0status\0")
    digest.update(status_result.stdout)
    digest.update(b"\0diff\0")
    digest.update(diff)
    for encoded_path in sorted(
        path for path in untracked_result.stdout.split(b"\0") if path
    ):
        relative = os.fsdecode(encoded_path)
        path = root / relative
        digest.update(b"\0untracked\0")
        digest.update(encoded_path)
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                digest.update(b"\0symlink\0")
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                digest.update(b"\0file\0")
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            else:
                digest.update(b"\0other\0")
                digest.update(str(mode).encode("ascii"))
        except OSError as error:
            digest.update(b"\0unreadable\0")
            digest.update(str(error).encode("utf-8", errors="replace"))
    return {
        "checkpoint": checkpoint,
        "dirty": bool(status_result.stdout),
        "fingerprint": f"sha256:{digest.hexdigest()}",
    }


def source_state(root: Path) -> dict[str, Any]:
    """Return the checkpoint state used to bind lifecycle evidence."""
    return _source_state(root)


def _contained_working_directory(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    working = (resolved_root / relative).resolve()
    try:
        working.relative_to(resolved_root)
    except ValueError as error:
        raise ContractError(
            f"working directory escapes the project root: {relative}"
        ) from error
    if not working.is_dir():
        raise ContractError(f"working directory does not exist: {relative}")
    return working


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait()


def _run_check(root: Path, check: Check) -> dict[str, Any]:
    working = _contained_working_directory(root, check.working_directory)
    started = _timestamp()
    monotonic_start = time.monotonic()
    command_digest = hashlib.sha256(
        json.dumps(check.run, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    print(f"[{check.identifier}] {' '.join(check.run)}", file=sys.stderr)
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            check.run,
            cwd=working,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            shell=False,
            start_new_session=os.name == "posix",
            creationflags=creation_flags,
        )
    except OSError as error:
        return {
            "id": check.identifier,
            "status": "failed-to-start",
            "exitCode": None,
            "startedAt": started,
            "durationMs": round((time.monotonic() - monotonic_start) * 1000),
            "workingDirectory": check.working_directory,
            "command": list(check.run),
            "commandSha256": command_digest,
            "error": str(error),
        }
    status = "passed"
    exit_code: int | None
    error_message: str | None = None
    try:
        exit_code = process.wait(timeout=check.timeout_seconds)
        if exit_code != 0:
            status = "failed"
    except subprocess.TimeoutExpired:
        status = "timed-out"
        error_message = f"exceeded {check.timeout_seconds} seconds"
        _stop_process(process)
        exit_code = process.returncode
    except KeyboardInterrupt:
        _stop_process(process)
        raise
    result: dict[str, Any] = {
        "id": check.identifier,
        "status": status,
        "exitCode": exit_code,
        "startedAt": started,
        "durationMs": round((time.monotonic() - monotonic_start) * 1000),
        "workingDirectory": check.working_directory,
        "command": list(check.run),
        "commandSha256": command_digest,
    }
    if error_message is not None:
        result["error"] = error_message
    return result


def run_profile(root: Path, project: Project, profile: str) -> dict[str, Any]:
    checks = project.profiles.get(profile)
    if checks is None:
        available = ", ".join(sorted(project.profiles))
        raise ContractError(
            f"unknown profile {profile}; available profiles: {available}"
        )
    started = _timestamp()
    source_before = _source_state(root)
    results = [_run_check(root, check) for check in checks]
    source_after = _source_state(root)
    source_changed = (
        source_before["fingerprint"] is not None
        and source_after["fingerprint"] is not None
        and source_before["fingerprint"] != source_after["fingerprint"]
    )
    status = (
        "passed"
        if all(item["status"] == "passed" for item in results) and not source_changed
        else "failed"
    )
    return {
        "schemaVersion": 1,
        "project": project.identifier,
        "profile": profile,
        "checkpoint": source_before["checkpoint"],
        "workingTreeDirty": source_before["dirty"],
        "workspaceFingerprint": source_before["fingerprint"],
        "completedWorkspaceFingerprint": source_after["fingerprint"],
        "sourceChangedDuringVerification": source_changed,
        "startedAt": started,
        "completedAt": _timestamp(),
        "status": status,
        "checks": results,
    }
