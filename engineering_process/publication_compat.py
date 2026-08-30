"""Small 1.x compatibility checks for pre-1.0 consumer workflows.

Publication policy is consumer-owned in the new process. These four read-only checks
remain temporarily so an existing consumer can adopt 1.0 before moving the equivalent
rules into its own repository.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

from .contracts import ProcessError


BRANCH = re.compile(
    r"^(?:feat|feature|fix|chore|docs|refactor|test|build|ci|perf)/"
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
    r"|^automation/(?:renovate|release)/[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
)
SUBJECT = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|build|ci|perf)"
    r"(?:\([a-z0-9._/-]+\))?!?: [^\r\n]{1,200}$"
)
MAX_BODY_BYTES = 1_000_000
MAX_COMMITS = 2_000


def branch_issues(branch: str) -> list[str]:
    issues: list[str] = []
    if not BRANCH.fullmatch(branch):
        issues.append("branch must use an accepted typed or automation prefix")
    if ".." in branch or "//" in branch or branch.endswith((".", "/")):
        issues.append("branch contains an unsafe path sequence")
    return issues


def commit_issues(subject: str) -> list[str]:
    return [] if SUBJECT.fullmatch(subject) else [
        "commit subject must use Conventional Commit style"
    ]


def validate_range(project_root: Path, branch: str, range_spec: str) -> dict[str, Any]:
    issues = branch_issues(branch)
    if not range_spec or len(range_spec) > 512 or any(
        character.isspace() for character in range_spec
    ):
        issues.append("commit range is invalid")
        return {"issues": issues, "commits": []}
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H%x00%s", "-z", range_spec],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessError(f"cannot validate commit range: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        issues.append(f"git cannot resolve commit range: {detail}")
        return {"issues": issues, "commits": []}
    fields = result.stdout.decode("utf-8", errors="replace").split("\0")
    fields = [field for field in fields if field]
    if len(fields) % 2 != 0 or len(fields) // 2 > MAX_COMMITS:
        issues.append("commit range output is invalid or exceeds its limit")
        return {"issues": issues, "commits": []}
    commits: list[dict[str, str]] = []
    for index in range(0, len(fields), 2):
        commit, subject = fields[index], fields[index + 1]
        commits.append({"commit": commit, "subject": subject})
        issues.extend(f"{commit}: {issue}" for issue in commit_issues(subject))
    if not commits:
        issues.append("commit range must contain at least one commit")
    return {"issues": issues, "commits": commits}


def validate_pull_request(
    *,
    title: str,
    branch: str,
    state: str,
    body_path: Path | None,
) -> dict[str, Any]:
    issues = branch_issues(branch) + commit_issues(title)
    if state not in {"draft", "ready"}:
        issues.append("pull request state must be draft or ready")
    body = ""
    if body_path is not None:
        try:
            data = body_path.read_bytes()
        except OSError as error:
            raise ProcessError(f"cannot read pull request body: {error}") from error
        if len(data) > MAX_BODY_BYTES:
            issues.append("pull request body exceeds its size limit")
        else:
            try:
                body = data.decode("utf-8")
            except UnicodeError:
                issues.append("pull request body must be UTF-8")
    for heading in ("## Summary", "## Verification", "## Independent review"):
        if heading not in body:
            issues.append(f"pull request body is missing {heading}")
    return {"issues": issues}
