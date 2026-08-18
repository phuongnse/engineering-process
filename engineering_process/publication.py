from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .contracts import ContractError


CONVENTIONAL_SUBJECT_MAX_LENGTH = 72
CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^[a-z]+(?:\([a-z0-9-]+\))?!?: \S(?:.*\S)?$"
)
MANUAL_BRANCH_RE = re.compile(
    r"^(?:feat|fix|docs|refactor|test|chore|build|ci|perf)/"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
AUTOMATION_BRANCH_RE = re.compile(
    r"^automation/[a-z0-9]+(?:-[a-z0-9]+)*/"
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
)
GIT_RANGE_RE = re.compile(
    r"^[0-9A-Za-z][0-9A-Za-z._/~^-]*(?:\.\.\.?[0-9A-Za-z][0-9A-Za-z._/~^-]*)?$"
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
CHECKBOX_RE = re.compile(
    r"^\s*-\s+\[(?P<state>[ xX])\]\s+\*\*(?P<label>[^*]+)\*\*(?P<detail>.*)$",
    re.MULTILINE,
)
CHECKLIST_STATUS_RE = re.compile(
    r"\[status:\s*(?P<status>[a-z-]+)\]\s*$", re.IGNORECASE
)
CHECKLIST_REASON_RE = re.compile(
    r"\[reason:\s*(?P<reason>[^\]]*\S[^\]]*)\]", re.IGNORECASE
)
CHECKLIST_STATUSES = {"satisfied", "not-applicable", "pending"}
REQUIRED_SECTIONS = (
    "Summary",
    "Contract and scope",
    "Impact and risk",
    "Verification",
    "Independent review",
    "Requirements and rules followed",
)
REQUIRED_REQUIREMENTS = (
    "Scope and contract",
    "Verification evidence",
    "Independent review",
)


def validate_conventional_subject(subject: str, *, label: str) -> list[str]:
    stripped = subject.strip()
    if not stripped:
        return [f"{label} is empty; use Conventional Commit style"]
    issues: list[str] = []
    if subject != stripped or CONVENTIONAL_SUBJECT_RE.fullmatch(stripped) is None:
        issues.append(
            f"{label} must use Conventional Commit style: "
            "`type(scope): subject` or `type: subject`"
        )
    if len(stripped) > CONVENTIONAL_SUBJECT_MAX_LENGTH:
        issues.append(
            f"{label} must be at most {CONVENTIONAL_SUBJECT_MAX_LENGTH} characters "
            f"({len(stripped)} found)"
        )
    if stripped.endswith("."):
        issues.append(f"{label} must not end with a period")
    return issues


def validate_commit_subject(subject: str) -> list[str]:
    return validate_conventional_subject(subject, label="Commit subject")


def validate_pr_title(title: str) -> list[str]:
    return validate_conventional_subject(title, label="PR title")


def is_automation_branch(branch: str) -> bool:
    return AUTOMATION_BRANCH_RE.fullmatch(branch.strip()) is not None


def validate_branch(branch: str) -> list[str]:
    stripped = branch.strip()
    if not stripped:
        return ["PR branch is empty"]
    if branch != stripped:
        return ["PR branch must not contain surrounding whitespace"]
    if MANUAL_BRANCH_RE.fullmatch(stripped) or AUTOMATION_BRANCH_RE.fullmatch(stripped):
        return []
    return [
        "PR branch must be `{type}/{kebab-description}` or "
        "`automation/{owner}/{description}`"
    ]


def _sections(body: str) -> tuple[dict[str, str], list[str]]:
    matches = list(HEADING_RE.finditer(body))
    result: dict[str, str] = {}
    duplicates: list[str] = []
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if name in result:
            duplicates.append(name)
        result[name] = body[start:end]
    return result, duplicates


def _visible(text: str) -> str:
    return COMMENT_RE.sub("", text).strip()


def validate_pr_body(body: str, *, allow_pending: bool) -> list[str]:
    body = body.lstrip("\ufeff")
    if not _visible(body):
        return ["PR body is empty; use the managed pull-request template"]
    sections, duplicates = _sections(body)
    issues = [f"Duplicate section: ## {name}" for name in sorted(set(duplicates))]
    for required in REQUIRED_SECTIONS:
        if required not in sections:
            issues.append(f"Missing section: ## {required}")
        elif required != "Requirements and rules followed" and not _visible(
            sections[required]
        ):
            issues.append(f"Section must be filled in: ## {required}")

    requirements = sections.get("Requirements and rules followed", "")
    checkboxes = list(CHECKBOX_RE.finditer(requirements))
    labels = [match.group("label").strip() for match in checkboxes]
    for required in REQUIRED_REQUIREMENTS:
        count = labels.count(required)
        if count == 0:
            issues.append(f"Missing standard requirement: {required}")
        elif count > 1:
            issues.append(f"Duplicate standard requirement: {required}")

    for match in checkboxes:
        line = match.group(0).strip()
        checked = match.group("state").lower() == "x"
        status_match = CHECKLIST_STATUS_RE.search(line)
        if status_match is None:
            issues.append(f"Requirement is missing a structured status: {line}")
            continue
        status = status_match.group("status").lower()
        if status not in CHECKLIST_STATUSES:
            issues.append(f"Requirement has invalid status `{status}`: {line}")
            continue
        if status == "pending":
            if checked:
                issues.append(f"Pending requirement must be unchecked: {line}")
            if not allow_pending:
                issues.append(f"Pending requirement is not ready for publication: {line}")
        elif not checked:
            issues.append(f"Resolved requirement must be checked: {line}")
        if status == "not-applicable" and CHECKLIST_REASON_RE.search(line) is None:
            issues.append(
                f"Not-applicable requirement must include `[reason: ...]`: {line}"
            )
    return issues


def validate_pull_request(
    *, title: str, body: str, branch: str, state: str
) -> list[str]:
    if state not in {"draft", "ready"}:
        raise ContractError("pull-request state must be draft or ready")
    allow_pending = state == "draft" or is_automation_branch(branch)
    return [
        *validate_branch(branch),
        *validate_pr_title(title),
        *validate_pr_body(body, allow_pending=allow_pending),
    ]


def commit_subjects(project_root: Path, range_spec: str) -> list[tuple[str, str]]:
    if GIT_RANGE_RE.fullmatch(range_spec) is None:
        raise ContractError(f"invalid Git revision or range: {range_spec}")
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H%x00%s", range_spec, "--"],
            cwd=project_root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"cannot inspect commit range {range_spec}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"cannot inspect commit range {range_spec}"
            + (f": {detail}" if detail else "")
        )
    records: list[tuple[str, str]] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        commit, separator, subject = line.partition("\0")
        if not separator:
            raise ContractError("git returned an invalid commit record")
        records.append((commit, subject))
    if not records:
        raise ContractError(f"commit range {range_spec} is empty")
    return records


def validate_commit_range(
    project_root: Path, *, branch: str, range_spec: str
) -> tuple[list[str], list[tuple[str, str]]]:
    records = commit_subjects(project_root, range_spec)
    issues = validate_branch(branch)
    for commit, subject in records:
        issues.extend(
            f"Commit {commit[:12]}: {issue}"
            for issue in validate_commit_subject(subject)
        )
    return issues, records
