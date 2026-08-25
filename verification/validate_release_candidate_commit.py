from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from engineering_process.git import run_git


GIT_TIMEOUT_SECONDS = 15.0
MAX_GIT_OUTPUT_BYTES = 1_000_000
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _git_text(root: Path, arguments: list[str], *, label: str) -> str:
    result = run_git(
        root,
        arguments,
        label=label,
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        max_stdout_bytes=MAX_GIT_OUTPUT_BYTES,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"{label}: Git exited {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError(f"{label}: Git output is not UTF-8") from error


def validate_release_candidate_commit(
    project_root: Path,
    *,
    expected_base: str,
) -> dict[str, str]:
    if SHA_PATTERN.fullmatch(expected_base) is None:
        raise ContractError("expected candidate base must be a full lowercase Git SHA")
    try:
        root = project_root.resolve(strict=True)
    except OSError as error:
        raise ContractError(f"cannot resolve candidate project root: {error}") from error
    if not root.is_dir():
        raise ContractError("candidate project root must be a directory")

    status = run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        label="release candidate cleanliness",
        timeout_seconds=GIT_TIMEOUT_SECONDS,
        max_stdout_bytes=MAX_GIT_OUTPUT_BYTES,
    )
    if status.returncode != 0:
        detail = status.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            "release candidate cleanliness: Git status failed"
            + (f": {detail}" if detail else "")
        )
    if status.stdout:
        raise ContractError(
            "release candidate contains uncommitted tracked, staged, or untracked output"
        )

    identity = _git_text(
        root,
        ["rev-list", "--parents", "-n", "1", "HEAD"],
        label="release candidate identity",
    ).split()
    if len(identity) != 2 or any(SHA_PATTERN.fullmatch(value) is None for value in identity):
        raise ContractError(
            "release candidate must be one non-merge commit with one exact parent"
        )
    head_sha, base_sha = identity
    if base_sha != expected_base:
        raise ContractError(
            "release candidate parent does not match the protected source checkpoint"
        )
    return {
        "baseSha": base_sha,
        "headSha": head_sha,
        "status": "clean",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-base", required=True)
    arguments = parser.parse_args()
    try:
        result = validate_release_candidate_commit(
            arguments.project_root,
            expected_base=arguments.expected_base,
        )
    except ContractError as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
