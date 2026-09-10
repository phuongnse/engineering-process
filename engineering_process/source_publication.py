"""Read-only source checks for an explicitly selected publication policy."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

from .contracts import ProcessError
from .repository import _git, resolve_commit


BRANCH = re.compile(
    r"^(?:feat|feature|fix|chore|docs|refactor|test|build|ci|perf)/"
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
    r"|^automation/(?:renovate|release)/[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
)
SUBJECT = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|build|ci|perf)"
    r"(?:\([a-z0-9._/-]+\))?!?: [^\r\n]{1,200}$"
)
MAX_COMMITS = 2_000


def _git_text(
    project_root: Path,
    arguments: list[str],
    label: str,
    *,
    strip: bool = True,
) -> str:
    try:
        output = _git(project_root, arguments)
    except ProcessError as error:
        raise ProcessError(f"cannot inspect {label}: {error}") from error
    text = output.decode("utf-8", errors="replace")
    return text.strip() if strip else text.rstrip("\r\n")


def current_branch(project_root: Path) -> str:
    return _git_text(project_root, ["branch", "--show-current"], "current branch")


def current_commit_subject(project_root: Path, revision: str = "HEAD") -> str:
    return _git_text(
        project_root,
        ["log", "-1", "--format=%s", "--end-of-options", revision],
        "current commit subject",
        strip=False,
    )


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
            [
                "git",
                "log",
                f"--max-count={MAX_COMMITS + 1}",
                "--format=%H%x00%s",
                "-z",
                "--end-of-options",
                range_spec,
            ],
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


def validate_current_source(
    project_root: Path, comparison_base: str
) -> dict[str, Any]:
    branch = current_branch(project_root)
    head = resolve_commit(project_root, "HEAD")
    subject = current_commit_subject(project_root, head)
    range_spec = f"{comparison_base}..{head}"
    range_result = validate_range(project_root, branch, range_spec)
    branch_errors = branch_issues(branch)
    issues = branch_errors + commit_issues(subject)
    issues.extend(
        issue for issue in range_result["issues"] if issue not in branch_errors
    )
    return {
        "branch": branch,
        "subject": subject,
        "range": range_spec,
        "issues": issues,
    }
