"""Small 1.x compatibility checks for pre-1.0 consumer workflows.

Publication policy is consumer-owned in the new process. These four read-only checks
remain temporarily so an existing consumer can adopt 1.0 before moving the equivalent
rules into its own repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ProcessError
from .artifact_standards import resolve_standard
from .distribution import distribution_root
from .pr_description import _without_html_comments, body_issues
from .source_publication import (
    BRANCH, SUBJECT, MAX_COMMITS, branch_issues, commit_issues,
    current_branch, current_commit_subject, validate_current_source, validate_range,
)


MAX_BODY_BYTES = 1_000_000


def _pull_request_body_issues(body: str, state: str) -> list[str]:
    standard = resolve_standard(Path.cwd(), distribution_root(), "pull-request")
    return body_issues(body, state, standard)


def validate_pull_request(
    *,
    title: str,
    branch: str,
    state: str,
    body_path: Path | None,
    project_root: Path | None = None,
    process_root: Path | None = None,
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
    standard = resolve_standard(project_root or Path.cwd(), distribution_root(process_root), "pull-request")
    issues.extend(body_issues(body, state, standard))
    return {"issues": issues, "standard": standard.metadata}
