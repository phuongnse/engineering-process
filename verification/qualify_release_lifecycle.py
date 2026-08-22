from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from verification.prepare_release_review import (
    TRUSTED_VERIFIER_REPOSITORY,
    TRUSTED_VERIFIER_SHA,
)


COMMAND_TIMEOUT_SECONDS = 120
PROFILE_TIMEOUT_SECONDS = 2_400
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


def qualification_evidence(checkpoint: str) -> dict[str, str]:
    return {
        "status": "passed",
        "governanceMode": "single-maintainer",
        "verificationKind": "independent-automated",
        "repository": "phuongnse/engineering-process",
        "headSha": checkpoint,
        "verifierRepository": TRUSTED_VERIFIER_REPOSITORY,
        "verifierSha": TRUSTED_VERIFIER_SHA,
    }


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
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=_safe_environment(),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"release qualification command failed: {error}") from error
    if result.returncode != 0:
        detail = ""
        if capture:
            combined = (result.stdout or b"") + (result.stderr or b"")
            detail = combined[-8_192:].decode("utf-8", errors="replace").strip()
        rendered = " ".join(command)
        raise ContractError(
            f"release qualification command exited {result.returncode}: {rendered}"
            + (f": {detail}" if detail else "")
        )
    if not capture:
        return ""
    try:
        return result.stdout.decode("utf-8").strip()
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
        change_id = release_change.get("id")
        if not isinstance(change_id, str) or not change_id:
            raise ContractError("generated release change id is invalid")
        context = f"qualification-only-{checkpoint}"
        authority_command = str(authority)
        lifecycle_commands: tuple[tuple[Sequence[str], int], ...] = (
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
                    "qualification-release-bot",
                    "--context",
                    context,
                    "--actor-kind",
                    "agent",
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
                    "qualification-release-bot",
                    "--context",
                    context,
                    "--actor-kind",
                    "agent",
                    "--change-id",
                    change_id,
                    "--plan",
                    ".release/plan.json",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
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
            (
                (
                    authority_command,
                    "change",
                    "review",
                    "start",
                    "--actor",
                    "renovate-ops-independent-reviewer",
                    "--context",
                    f"review-{context}",
                    "--actor-kind",
                    "agent",
                    "--change-id",
                    change_id,
                    "--method",
                    "isolated-context",
                    "--attested-by",
                    f"renovate-ops-{TRUSTED_VERIFIER_SHA}",
                    "--attestation-evidence",
                    f"github://{TRUSTED_VERIFIER_REPOSITORY}/commit/{TRUSTED_VERIFIER_SHA}",
                ),
                COMMAND_TIMEOUT_SECONDS,
            ),
        )
        for command, timeout_seconds in lifecycle_commands:
            _run(command, cwd=candidate, timeout_seconds=timeout_seconds)
        independent_evidence = qualification_root / "qualification-evidence.json"
        _write_object(
            independent_evidence,
            qualification_evidence(checkpoint),
            "qualification evidence",
        )
        review_report = qualification_root / "release-review.json"
        _run(
            [
                sys.executable,
                "verification/prepare_release_review.py",
                "--project-root",
                str(candidate),
                "--change-id",
                change_id,
                "--independent-evidence",
                str(independent_evidence),
                "--output",
                str(review_report),
            ],
            cwd=candidate,
        )
        _run(
            [
                authority_command,
                "change",
                "review",
                "submit",
                "--change-id",
                change_id,
                "--report",
                str(review_report),
            ],
            cwd=candidate,
        )
        _run(
            [
                authority_command,
                "change",
                "finish",
                "--actor",
                "qualification-release-bot",
                "--context",
                context,
                "--actor-kind",
                "agent",
                "--change-id",
                change_id,
            ],
            cwd=candidate,
        )
        release = _read_object(candidate / "release.json", "release contract")
        provenance = release.get("provenance")
        identity = release.get("identity")
        if not isinstance(provenance, dict) or not isinstance(identity, dict):
            raise ContractError("generated release identity is invalid")
        mode = provenance.get("mode")
        if mode == "governed":
            evidence_name = identity.get("receiptAsset")
            export_command = [authority_command, "evidence", "export"]
            validate_command = [authority_command, "evidence", "validate"]
        elif mode == "bootstrap-authority":
            evidence_name = identity.get("authorizationAsset")
            export_command = [sys.executable, "processctl.py", "evidence", "export-bootstrap"]
            validate_command = [
                sys.executable,
                "processctl.py",
                "evidence",
                "validate-bootstrap",
            ]
        else:
            raise ContractError("generated release provenance mode is not publishable")
        if not isinstance(evidence_name, str) or not evidence_name:
            raise ContractError("generated release evidence asset is invalid")
        exported_evidence = qualification_root / evidence_name
        _run(
            [*export_command, "--change-id", change_id, "--output", str(exported_evidence)],
            cwd=candidate,
        )
        _run([*validate_command, str(exported_evidence)], cwd=candidate)
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
            or lifecycle_status.get("phase") != "completed"
            or lifecycle_status.get("current") is not True
        ):
            raise ContractError("release qualification lifecycle did not complete")
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
