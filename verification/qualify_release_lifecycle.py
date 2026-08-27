from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.bounded_process import run_bounded_process
from engineering_process.contracts import ContractError
from engineering_process.diagnostics import (
    classify_diagnostics,
    diagnostic_failure_message,
)


COMMAND_TIMEOUT_SECONDS = 120
PROFILE_TIMEOUT_SECONDS = 2_400
COMMAND_OUTPUT_STREAM_LIMIT = 1_000_000
COMMAND_OUTPUT_TOTAL_LIMIT = 1_500_000
SENSITIVE_ENVIRONMENT_MARKERS = (
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


def pending_release_changes(project_root: Path) -> tuple[Path, ...]:
    changes_root = project_root / "release-changes"
    if not changes_root.is_dir():
        raise ContractError("release qualification requires release-changes/")
    return tuple(sorted(path for path in changes_root.glob("*.json") if path.is_file()))


def _safe_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in SENSITIVE_ENVIRONMENT_MARKERS)
    }
    interpreter_root = str(Path(sys.executable).absolute().parent)
    environment["PATH"] = os.pathsep.join(
        (interpreter_root, environment.get("PATH", ""))
    )
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
    capture: bool = False,
) -> str:
    try:
        result = run_bounded_process(
            command,
            working_directory=cwd,
            environment=_safe_environment(),
            timeout_seconds=timeout_seconds,
            max_stream_bytes=COMMAND_OUTPUT_STREAM_LIMIT,
            max_total_bytes=COMMAND_OUTPUT_TOTAL_LIMIT,
        )
    except (OSError, ValueError) as error:
        raise ContractError(f"release qualification command failed: {error}") from error
    if result.timed_out:
        raise ContractError(
            f"release qualification command exceeded {timeout_seconds} seconds"
        )
    if result.output_exceeded:
        raise ContractError("release qualification command output exceeded its limit")
    if result.descendants_found or result.cleanup_error is not None:
        raise ContractError(
            result.cleanup_error
            or "release qualification command left descendant processes"
        )
    stdout = result.stdout
    stderr = result.stderr
    if not capture:
        if stdout:
            sys.stdout.buffer.write(stdout)
            sys.stdout.buffer.flush()
        if stderr:
            sys.stderr.buffer.write(stderr)
            sys.stderr.buffer.flush()
    if result.returncode != 0:
        detail = ""
        if capture:
            combined = stdout + stderr
            detail = combined[-8_192:].decode("utf-8", errors="replace").strip()
        rendered = " ".join(command)
        raise ContractError(
            f"release qualification command exited {result.returncode}: {rendered}"
            + (f": {detail}" if detail else "")
        )
    diagnostics = classify_diagnostics(stdout=stdout, stderr=stderr)
    diagnostic_error = diagnostic_failure_message(
        diagnostics, subject="release qualification command"
    )
    if diagnostic_error is not None:
        raise ContractError(diagnostic_error)
    if not capture:
        return ""
    try:
        return stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError("release qualification command output is not UTF-8") from error


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _write_object(path: Path, value: dict[str, object], label: str) -> None:
    try:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ContractError(f"cannot write {label}: {error}") from error


def qualify_release_lifecycle(
    project_root: Path,
    processctl: Path,
    *,
    temporary_root: Path | None = None,
) -> dict[str, object]:
    root = project_root.resolve(strict=True)
    authority = Path(os.path.abspath(processctl.expanduser()))
    if not authority.is_file():
        raise ContractError("release qualification processctl must be a file")
    if authority.parent != Path(sys.executable).absolute().parent:
        raise ContractError(
            "release qualification must run with the public authority Python"
        )
    changes = pending_release_changes(root)
    if not changes:
        return {"status": "not-applicable", "reason": "no pending release changes"}
    initial_status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture=True,
    )
    if initial_status:
        raise ContractError("release qualification requires a clean source checkpoint")
    source_checkpoint = _run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=root, capture=True
    )
    temp_parent = (
        str(temporary_root.resolve(strict=True)) if temporary_root is not None else None
    )
    with tempfile.TemporaryDirectory(
        prefix="engineering-process-release-qualification-", dir=temp_parent
    ) as directory:
        qualification_root = Path(directory)
        candidate = qualification_root / "candidate"
        _run(
            [
                "git",
                "clone",
                "--quiet",
                "--local",
                "--no-hardlinks",
                "--no-checkout",
                str(root),
                str(candidate),
            ],
            cwd=root,
        )
        _run(["git", "checkout", "--quiet", "--detach", source_checkpoint], cwd=candidate)
        _run(["git", "config", "user.email", "qualification@example.invalid"], cwd=candidate)
        _run(["git", "config", "user.name", "Release Qualification"], cwd=candidate)
        _run(
            [sys.executable, "processctl.py", "publication", "prepare-release"],
            cwd=candidate,
        )
        _run(["git", "add", "--all"], cwd=candidate)
        staged = _run(["git", "diff", "--cached", "--name-only"], cwd=candidate, capture=True)
        if not staged:
            raise ContractError("release qualification candidate did not change source")
        _run(
            ["git", "commit", "--quiet", "-m", "chore(release): qualification candidate"],
            cwd=candidate,
        )
        checkpoint = _run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=candidate,
            capture=True,
        )
        release_change = _read_object(candidate / ".release" / "change.json", "release change")
        release_plan = _read_object(candidate / ".release" / "plan.json", "release plan")
        change_id = release_change.get("id")
        if not isinstance(change_id, str) or not change_id:
            raise ContractError("generated release change id is invalid")
        provenance = release_plan.get("provenance")
        if not isinstance(provenance, dict):
            raise ContractError("release qualification plan provenance is missing")
        plan_kind = provenance.get("kind")
        if plan_kind == "authored":
            author = provenance.get("author")
            if not isinstance(author, dict):
                raise ContractError("release qualification plan author is missing")
            actor_id = author.get("actorId")
            context = author.get("contextId")
            actor_kind = author.get("kind")
            if (
                not isinstance(actor_id, str)
                or not isinstance(context, str)
                or actor_kind not in {"agent", "human"}
            ):
                raise ContractError("release qualification plan author is invalid")
        elif plan_kind == "process-generated":
            actor_id = "qualification-release-bot"
            context = f"qualification-only-{checkpoint}"
            actor_kind = "agent"
        else:
            raise ContractError("release qualification plan provenance kind is invalid")
        authority_command = str(authority)
        lifecycle_commands: list[tuple[Sequence[str], int]] = [
            ((authority_command, "doctor"), COMMAND_TIMEOUT_SECONDS),
            (
                (
                    authority_command,
                    "contract",
                    "validate",
                    "--kind",
                    "change",
                    ".release/change.json",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
            (
                (
                    authority_command,
                    "contract",
                    "validate",
                    "--kind",
                    "plan",
                    ".release/plan.json",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
            (
                (
                    authority_command,
                    "change",
                    "start",
                    "--actor",
                    actor_id,
                    "--context",
                    context,
                    "--actor-kind",
                    actor_kind,
                    "--contract",
                    ".release/change.json",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
            (
                (
                    authority_command,
                    "change",
                    "plan",
                    "--actor",
                    actor_id,
                    "--context",
                    context,
                    "--actor-kind",
                    actor_kind,
                    "--change-id",
                    change_id,
                    "--plan",
                    ".release/plan.json",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
        ]
        if plan_kind == "authored":
            lifecycle_commands.append(
                (
                    (
                        authority_command,
                        "change",
                        "decision",
                        "start",
                        "--actor",
                        "qualification-plan-reviewer",
                        "--context",
                        f"qualification-plan-review-{checkpoint}",
                        "--actor-kind",
                        "agent",
                        "--change-id",
                        change_id,
                        "--method",
                        "isolated-context",
                        "--attested-by",
                        "release-qualification",
                        "--attestation-evidence",
                        "Ephemeral read-only qualification assignment; no review, implementation, completion, or publication occurs",
                    ),
                    COMMAND_TIMEOUT_SECONDS,
                )
            )
            expected_phase = "planned"
            next_skill = "plan-decision-review"
        else:
            lifecycle_commands.extend((
                (
                (
                    authority_command,
                    "change",
                    "implement",
                    "--actor",
                    "qualification-release-bot",
                    "--context",
                    context,
                    "--actor-kind",
                    "agent",
                    "--change-id",
                    change_id,
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
            (
                (
                    authority_command,
                    "change",
                    "verify",
                    "--actor",
                    "qualification-release-bot",
                    "--context",
                    context,
                    "--actor-kind",
                    "agent",
                    "--change-id",
                    change_id,
                    "--profile",
                    "development",
                ),
                PROFILE_TIMEOUT_SECONDS,
            ),
            (
                (
                    authority_command,
                    "change",
                    "verify",
                    "--actor",
                    "qualification-release-bot",
                    "--context",
                    context,
                    "--actor-kind",
                    "agent",
                    "--change-id",
                    change_id,
                    "--profile",
                    "review",
                ),
                PROFILE_TIMEOUT_SECONDS,
            ),
            ))
            expected_phase = "verified"
            next_skill = "review-change"
        for command, timeout_seconds in lifecycle_commands:
            _run(command, cwd=candidate, timeout_seconds=timeout_seconds)
        raw_status = _run(
            [
                authority_command,
                "change",
                "status",
                "--change-id",
                change_id,
                "--json",
            ],
            cwd=candidate,
            capture=True,
        )
        try:
            lifecycle_status = json.loads(raw_status)
        except json.JSONDecodeError as error:
            raise ContractError("release qualification status is invalid JSON") from error
        if (
            not isinstance(lifecycle_status, dict)
            or lifecycle_status.get("status") != "passed"
            or lifecycle_status.get("phase") != expected_phase
            or lifecycle_status.get("current") is not True
        ):
            raise ContractError(
                "release qualification did not stop at the expected reviewer handoff"
            )
        if plan_kind == "authored":
            decision = lifecycle_status.get("planDecision")
            if (
                not isinstance(decision, dict)
                or decision.get("authorized") is not False
                or decision.get("assignment") is None
                or decision.get("review") is not None
            ):
                raise ContractError(
                    "release qualification did not preserve the pending plan review"
                )
    final_status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture=True,
    )
    if final_status != initial_status:
        raise ContractError("release qualification changed the source checkout")
    return {
        "status": "passed",
        "sourceCheckpoint": source_checkpoint,
        "candidateCheckpoint": checkpoint,
        "changeId": change_id,
        "phase": expected_phase,
        "nextSkill": next_skill,
        "planKind": plan_kind,
        "authority": str(authority),
        "pendingChanges": [path.name for path in changes],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--processctl", type=Path, required=True)
    parser.add_argument("--temporary-root", type=Path)
    arguments = parser.parse_args()
    try:
        result = qualify_release_lifecycle(
            arguments.project_root,
            arguments.processctl,
            temporary_root=arguments.temporary_root,
        )
    except (ContractError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
