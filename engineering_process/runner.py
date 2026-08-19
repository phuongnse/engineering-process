from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Check, Project
from .environment import (
    environment_command_bindings,
    environment_path_entries,
    execute_command,
)
from .impact import IMPACT_FILE_ENV, plan_profile
from .tooling import ManagedCommandBinding


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


def _run_check(
    root: Path,
    check: Check,
    *,
    path_entries: tuple[Path, ...] = (),
    command_bindings: dict[str, ManagedCommandBinding] | None = None,
    impact_file: Path | None = None,
) -> dict[str, Any]:
    print(f"[{check.identifier}] {' '.join(check.run)}", file=sys.stderr)
    execution = execute_command(
        root,
        identifier=check.identifier,
        run=check.run,
        timeout_seconds=check.timeout_seconds,
        working_directory=check.working_directory,
        path_entries=path_entries,
        command_bindings=command_bindings,
        environment_overrides={
            IMPACT_FILE_ENV: str(impact_file) if impact_file is not None else None
        },
        stream_output=True,
    )
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
    }
    return {key: value for key, value in execution.items() if key in allowed}


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
    with tempfile.TemporaryDirectory(prefix="engineering-process-impact-") as directory:
        impact_file = Path(directory) / "impact.json"
        impact_file.write_text(
            json.dumps(plan.evidence, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        results = [
            _run_check(
                root,
                check,
                path_entries=path_entries,
                command_bindings=command_bindings,
                impact_file=impact_file,
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
    return {
        "schemaVersion": 1,
        "project": project.identifier,
        "profile": profile,
        "checkpoint": source_before["checkpoint"],
        "workingTreeDirty": source_before["dirty"],
        "workspaceFingerprint": source_before["fingerprint"],
        "completedWorkspaceFingerprint": source_after["fingerprint"],
        "sourceChangedDuringVerification": source_changed,
        "impact": plan.evidence,
        "startedAt": started,
        "completedAt": _timestamp(),
        "status": status,
        "checks": results,
    }
