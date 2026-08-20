from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Check, ContractError, MAX_JSON_BYTES, Project
from .environment import (
    environment_command_bindings,
    environment_path_entries,
    execute_command,
)
from .impact import IMPACT_FILE_ENV, plan_profile
from .git import (
    GitResult,
    remaining_seconds,
    run_git,
    validate_tracked_index_result,
)
from .tooling import ManagedCommandBinding


SOURCE_STATE_TIMEOUT_SECONDS = 30.0
MAX_SOURCE_STATUS_BYTES = 500_000
MAX_SOURCE_DIFF_BYTES = 8_000_000
MAX_SOURCE_PATHS = 5_000
MAX_UNTRACKED_FILE_BYTES = 8_000_000
MAX_UNTRACKED_TOTAL_BYTES = 32_000_000
MAX_SOURCE_ISSUE_STDERR_BYTES = 4096
MAX_SOURCE_ISSUE_ERROR_BYTES = 4096


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(
    root: Path,
    arguments: list[str],
    *,
    label: str,
    deadline: float,
    max_stdout_bytes: int,
):
    return run_git(
        root,
        arguments,
        label=f"workspace fingerprint {label}",
        timeout_seconds=remaining_seconds(
            deadline, label=f"workspace fingerprint {label}"
        ),
        max_stdout_bytes=max_stdout_bytes,
    )


def _git_failure_issue(
    operation: str, arguments: list[str], result: GitResult
) -> dict[str, Any]:
    preview = result.stderr[:MAX_SOURCE_ISSUE_STDERR_BYTES]
    return {
        "operation": operation,
        "command": ["git", *arguments],
        "failureKind": "nonzero-exit",
        "exitCode": result.returncode,
        "stderr": preview.decode("utf-8", errors="replace"),
        "stderrBytes": len(result.stderr),
        "stderrSha256": hashlib.sha256(result.stderr).hexdigest(),
        "stderrTruncated": len(result.stderr) > len(preview),
        "error": "",
        "errorBytes": 0,
        "errorSha256": hashlib.sha256(b"").hexdigest(),
        "errorTruncated": False,
    }


def _git_execution_issue(
    operation: str, arguments: list[str], error: ContractError
) -> dict[str, Any]:
    encoded = str(error).encode("utf-8")
    preview = encoded[:MAX_SOURCE_ISSUE_ERROR_BYTES]
    return {
        "operation": operation,
        "command": ["git", *arguments],
        "failureKind": "execution-error",
        "exitCode": None,
        "stderr": "",
        "stderrBytes": 0,
        "stderrSha256": hashlib.sha256(b"").hexdigest(),
        "stderrTruncated": False,
        "error": preview.decode("utf-8", errors="replace"),
        "errorBytes": len(encoded),
        "errorSha256": hashlib.sha256(encoded).hexdigest(),
        "errorTruncated": len(encoded) > len(preview),
    }


def _source_state(root: Path) -> dict[str, Any]:
    deadline = time.monotonic() + SOURCE_STATE_TIMEOUT_SECONDS
    issues: list[dict[str, Any]] = []
    ignored_bytecode_sha256: str | None = None
    tracked_index_sha256: str | None = None
    status_sha256: str | None = None
    diff_sha256: str | None = None
    untracked_sha256: str | None = None
    untracked_path_count: int | None = None
    untracked_bytes: int | None = None

    def result(
        *,
        checkpoint: str | None,
        dirty: bool | None,
        fingerprint: str | None,
    ) -> dict[str, Any]:
        return {
            "checkpoint": checkpoint,
            "dirty": dirty,
            "fingerprint": fingerprint,
            "diagnostics": {
                "issues": issues,
                "ignoredBytecodeSha256": ignored_bytecode_sha256,
                "trackedIndexSha256": tracked_index_sha256,
                "statusSha256": status_sha256,
                "diffSha256": diff_sha256,
                "untrackedSha256": untracked_sha256,
                "untrackedPathCount": untracked_path_count,
                "untrackedBytes": untracked_bytes,
            },
        }

    def probe(
        operation: str,
        arguments: list[str],
        *,
        label: str,
        max_stdout_bytes: int,
    ) -> GitResult | None:
        try:
            return _git(
                root,
                arguments,
                label=label,
                deadline=deadline,
                max_stdout_bytes=max_stdout_bytes,
            )
        except ContractError as error:
            issues.append(_git_execution_issue(operation, arguments, error))
            return None

    sourceless_arguments = [
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        "*.pyc",
        "*.pyo",
        ":(glob,exclude)**/__pycache__/**",
    ]
    sourceless_bytecode = probe(
        "ignored-sourceless-bytecode",
        sourceless_arguments,
        label="ignored sourceless Python bytecode",
        max_stdout_bytes=MAX_SOURCE_STATUS_BYTES,
    )
    if sourceless_bytecode is None:
        return result(checkpoint=None, dirty=None, fingerprint=None)
    if sourceless_bytecode.returncode != 0:
        issues.append(
            _git_failure_issue(
                "ignored-sourceless-bytecode",
                sourceless_arguments,
                sourceless_bytecode,
            )
        )
        return result(checkpoint=None, dirty=None, fingerprint=None)
    ignored_bytecode_sha256 = (
        f"sha256:{hashlib.sha256(sourceless_bytecode.stdout).hexdigest()}"
    )
    tracked_index_arguments = ["ls-files", "-v", "-z", "--cached", "--"]
    tracked_index = probe(
        "tracked-index",
        tracked_index_arguments,
        label="workspace fingerprint tracked index",
        max_stdout_bytes=MAX_SOURCE_STATUS_BYTES,
    )
    if tracked_index is None:
        return result(checkpoint=None, dirty=None, fingerprint=None)
    if tracked_index.returncode != 0:
        issues.append(
            _git_failure_issue(
                "tracked-index",
                tracked_index_arguments,
                tracked_index,
            )
        )
        return result(checkpoint=None, dirty=None, fingerprint=None)
    validate_tracked_index_result(
        tracked_index,
        label="workspace fingerprint tracked index",
        max_paths=MAX_SOURCE_PATHS,
    )
    tracked_index_sha256 = (
        f"sha256:{hashlib.sha256(tracked_index.stdout).hexdigest()}"
    )
    ignored_bytecode_paths = sorted(
        path for path in sourceless_bytecode.stdout.split(b"\0") if path
    )
    if len(ignored_bytecode_paths) > MAX_SOURCE_PATHS:
        raise ContractError(
            "workspace ignored sourceless bytecode path count exceeds "
            f"{MAX_SOURCE_PATHS}"
        )
    if ignored_bytecode_paths:
        first = os.fsdecode(ignored_bytecode_paths[0])
        raise ContractError(
            "ignored sourceless Python bytecode can shadow checkpoint source; "
            f"remove it before verification: {first}"
        )
    status_arguments = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    status_result = probe(
        "status",
        status_arguments,
        label="status",
        max_stdout_bytes=MAX_SOURCE_STATUS_BYTES,
    )
    if status_result is None:
        return result(checkpoint=None, dirty=None, fingerprint=None)
    if status_result.returncode != 0:
        issues.append(_git_failure_issue("status", status_arguments, status_result))
        return result(checkpoint=None, dirty=None, fingerprint=None)
    status_sha256 = f"sha256:{hashlib.sha256(status_result.stdout).hexdigest()}"

    head_arguments = ["rev-parse", "--verify", "HEAD^{commit}"]
    head_result = probe(
        "head",
        head_arguments,
        label="HEAD",
        max_stdout_bytes=128,
    )
    if head_result is None:
        return result(checkpoint=None, dirty=bool(status_result.stdout), fingerprint=None)
    checkpoint = (
        head_result.stdout.decode("ascii").strip()
        if head_result.returncode == 0
        else None
    )
    if head_result.returncode != 0:
        issues.append(_git_failure_issue("head", head_arguments, head_result))
    diff = b""
    if checkpoint is not None:
        diff_arguments = [
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ]
        diff_result = probe(
            "tracked-diff",
            diff_arguments,
            label="tracked diff",
            max_stdout_bytes=MAX_SOURCE_DIFF_BYTES,
        )
        if diff_result is None:
            return result(checkpoint=checkpoint, dirty=True, fingerprint=None)
        if diff_result.returncode != 0:
            issues.append(
                _git_failure_issue("tracked-diff", diff_arguments, diff_result)
            )
            return result(checkpoint=checkpoint, dirty=True, fingerprint=None)
        diff = diff_result.stdout
        diff_sha256 = f"sha256:{hashlib.sha256(diff).hexdigest()}"

    untracked_arguments = [
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    untracked_result = probe(
        "untracked-paths",
        untracked_arguments,
        label="untracked paths",
        max_stdout_bytes=MAX_SOURCE_STATUS_BYTES,
    )
    if untracked_result is None:
        return result(
            checkpoint=checkpoint,
            dirty=bool(status_result.stdout),
            fingerprint=None,
        )
    if untracked_result.returncode != 0:
        issues.append(
            _git_failure_issue(
                "untracked-paths", untracked_arguments, untracked_result
            )
        )
        return result(
            checkpoint=checkpoint,
            dirty=bool(status_result.stdout),
            fingerprint=None,
        )

    encoded_paths = sorted(
        path for path in untracked_result.stdout.split(b"\0") if path
    )
    if len(encoded_paths) > MAX_SOURCE_PATHS:
        raise ContractError(
            "workspace fingerprint untracked path count exceeds "
            f"{MAX_SOURCE_PATHS}"
        )

    digest = hashlib.sha256()
    digest.update(b"checkpoint\0")
    digest.update((checkpoint or "").encode("ascii"))
    digest.update(b"\0status\0")
    digest.update(status_result.stdout)
    digest.update(b"\0diff\0")
    digest.update(diff)
    untracked_digest = hashlib.sha256()
    total_untracked_bytes = 0

    def update_untracked(value: bytes) -> None:
        digest.update(value)
        untracked_digest.update(value)

    for encoded_path in encoded_paths:
        remaining_seconds(deadline, label="workspace fingerprint untracked files")
        relative = os.fsdecode(encoded_path)
        path = root / relative
        update_untracked(b"\0untracked\0")
        update_untracked(encoded_path)
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                update_untracked(b"\0symlink\0")
                target = os.fsencode(os.readlink(path))
                if len(target) > 4096:
                    raise ContractError(
                        f"workspace fingerprint symlink target is too long: {relative}"
                    )
                update_untracked(target)
            elif stat.S_ISREG(mode):
                before = path.lstat()
                if before.st_size > MAX_UNTRACKED_FILE_BYTES:
                    raise ContractError(
                        f"workspace fingerprint untracked file exceeds "
                        f"{MAX_UNTRACKED_FILE_BYTES} bytes: {relative}"
                    )
                total_untracked_bytes += before.st_size
                if total_untracked_bytes > MAX_UNTRACKED_TOTAL_BYTES:
                    raise ContractError(
                        "workspace fingerprint untracked content exceeds "
                        f"{MAX_UNTRACKED_TOTAL_BYTES} bytes"
                    )
                update_untracked(b"\0file\0")
                file_bytes = 0
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        remaining_seconds(
                            deadline,
                            label="workspace fingerprint untracked files",
                        )
                        file_bytes += len(chunk)
                        if file_bytes > before.st_size:
                            raise ContractError(
                                f"workspace fingerprint file changed while reading: {relative}"
                            )
                        update_untracked(chunk)
                after = path.lstat()
                if (
                    file_bytes != before.st_size
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or after.st_mode != before.st_mode
                ):
                    raise ContractError(
                        f"workspace fingerprint file changed while reading: {relative}"
                    )
            else:
                raise ContractError(
                    f"workspace fingerprint unsupported untracked file type: {relative}"
                )
        except OSError as error:
            raise ContractError(
                f"workspace fingerprint cannot read {relative}: {error}"
            ) from error
    untracked_sha256 = f"sha256:{untracked_digest.hexdigest()}"
    untracked_path_count = len(encoded_paths)
    untracked_bytes = total_untracked_bytes
    return result(
        checkpoint=checkpoint,
        dirty=bool(status_result.stdout),
        fingerprint=f"sha256:{digest.hexdigest()}",
    )


def source_state(root: Path) -> dict[str, Any]:
    """Return the checkpoint state used to bind lifecycle evidence."""
    return _source_state(root)


def _run_check(
    root: Path,
    check: Check,
    *,
    path_entries: tuple[Path, ...] = (),
    command_bindings: dict[str, ManagedCommandBinding] | None = None,
    impact_bytes: bytes,
    impact_root: Path,
) -> dict[str, Any]:
    print(f"[{check.identifier}] {' '.join(check.run)}", file=sys.stderr)
    directory = Path(
        tempfile.mkdtemp(prefix=f"{check.identifier}-", dir=impact_root)
    )
    impact_file = directory / "impact.json"
    pycache_root = directory / "pycache"
    expected_digest = hashlib.sha256(impact_bytes).hexdigest()
    integrity = "verified"
    try:
        pycache_root.mkdir()
        pycache_root.chmod(stat.S_IRWXU)
        impact_file.write_bytes(impact_bytes)
        impact_file.chmod(stat.S_IRUSR)
        directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
        execution = execute_command(
            root,
            identifier=check.identifier,
            run=check.run,
            timeout_seconds=check.timeout_seconds,
            working_directory=check.working_directory,
            path_entries=path_entries,
            command_bindings=command_bindings,
            environment_overrides={
                IMPACT_FILE_ENV: str(impact_file),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(pycache_root),
                "PYTHONHOME": None,
                "PYTHONPATH": None,
            },
            stream_output=True,
        )
        try:
            actual_bytes = impact_file.read_bytes()
        except OSError:
            actual_bytes = b""
        if actual_bytes != impact_bytes:
            integrity = "failed"
            execution["status"] = "failed"
            detail = "process-owned impact document was modified during the check"
            if execution.get("error"):
                detail = f"{execution['error']}; {detail}"
            execution["error"] = detail
    finally:
        try:
            directory.chmod(stat.S_IRWXU)
            if impact_file.exists():
                impact_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            shutil.rmtree(directory)
        except OSError as error:
            raise ContractError(
                f"impact document cleanup failed for {check.identifier}: {error}"
            ) from error
    allowed = {
        "id",
        "status",
        "exitCode",
        "startedAt",
        "durationMs",
        "workingDirectory",
        "command",
        "commandSha256",
        "error",
        "pathEntries",
        "outputTruncated",
        "streamOutputTruncated",
        "stdoutBytes",
        "stderrBytes",
        "stdoutSha256",
        "stderrSha256",
    }
    result = {key: value for key, value in execution.items() if key in allowed}
    result["timeoutSeconds"] = check.timeout_seconds
    result["impactSha256"] = expected_digest
    result["impactIntegrity"] = integrity
    return result


def run_profile(
    root: Path,
    project: Project,
    profile: str,
    *,
    base_ref: str | None = None,
) -> dict[str, Any]:
    started = _timestamp()
    source_before = _source_state(root)
    plan = plan_profile(root, project, profile, base_ref=base_ref)
    path_entries = environment_path_entries(project, profile=profile)
    command_bindings = environment_command_bindings(project, profile=profile)
    impact_bytes = (
        json.dumps(plan.evidence, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="engineering-process-impact-") as directory:
        results = [
            _run_check(
                root,
                check,
                path_entries=path_entries,
                command_bindings=command_bindings,
                impact_bytes=impact_bytes,
                impact_root=Path(directory),
            )
            for check in plan.checks
        ]
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
    report = {
        "schemaVersion": 2,
        "project": project.identifier,
        "profile": profile,
        "checkpoint": source_before["checkpoint"],
        "workingTreeDirty": source_before["dirty"],
        "workspaceFingerprint": source_before["fingerprint"],
        "completedWorkspaceFingerprint": source_after["fingerprint"],
        "sourceStateDiagnostics": source_before["diagnostics"],
        "completedSourceStateDiagnostics": source_after["diagnostics"],
        "sourceChangedDuringVerification": source_changed,
        "impact": plan.evidence,
        "startedAt": started,
        "completedAt": _timestamp(),
        "status": status,
        "checks": results,
    }
    serialized = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(serialized) > MAX_JSON_BYTES:
        raise ContractError(
            "verification report exceeds the lifecycle artifact limit: "
            f"{len(serialized)} > {MAX_JSON_BYTES} bytes"
        )
    return report
