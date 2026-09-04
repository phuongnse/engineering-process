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
PR_SECTIONS = (
    "## Summary",
    "## Contract and risk",
    "## Verification",
    "## Independent review",
    "## Completion gate",
)
# This is a closed publication protocol, not a vocabulary for inferring prose meaning.
PR_FIELDS = {
    "## Summary": ("Outcome", "Scope"),
    "## Contract and risk": ("Source", "Risk", "Compatibility", "Stack"),
    "## Verification": ("Profiles", "Snapshot", "Completion receipt"),
    "## Independent review": (
        "Verdict",
        "Cycles",
        "Blocking findings",
        "Non-blocking dispositions",
    ),
}
PR_CHECKS = (
    "Accepted scope is implemented without silent expansion.",
    "Required profiles pass on the reviewed snapshot.",
    "Independent review approved with no blocking finding.",
    "Every non-blocking finding has a recorded disposition.",
)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
NON_MARKDOWN_LINE_SEPARATOR = re.compile("[\v\f\x85\u2028\u2029]")
ISSUE_TARGET = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*"
ISSUE_REFERENCE = re.compile(
    rf"^(?:Refs {ISSUE_TARGET}|Closes {ISSUE_TARGET}(?:, closes {ISSUE_TARGET})*)\.$"
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


def _without_html_comments(body: str) -> tuple[str, list[str]]:
    visible: list[str] = []
    issues: list[str] = []
    cursor = 0
    while cursor < len(body):
        opener = body.find("<!--", cursor)
        closer = body.find("-->", cursor)
        if closer >= 0 and (opener < 0 or closer < opener):
            issues.append("pull request body has an unmatched HTML comment close")
            visible.append(body[cursor:closer])
            cursor = closer + 3
            continue
        if opener < 0:
            visible.append(body[cursor:])
            break
        visible.append(body[cursor:opener])
        closer = body.find("-->", opener + 4)
        if closer < 0:
            issues.append("pull request body has an unclosed HTML comment")
            break
        cursor = closer + 3
    return "".join(visible), issues


def _structural_lines(body: str) -> tuple[list[str | None], list[str]]:
    issues: list[str] = []
    if NON_MARKDOWN_LINE_SEPARATOR.search(body):
        issues.append("pull request body contains a non-Markdown line separator")
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    structural: list[str | None] = []
    fence_character = ""
    fence_length = 0
    for line in lines:
        marker = FENCE.match(line)
        if fence_character:
            structural.append(None)
            if marker and marker.group(1)[0] == fence_character:
                candidate = marker.group(1)
                if len(candidate) >= fence_length and not marker.group(2).strip():
                    fence_character = ""
                    fence_length = 0
            continue
        if marker:
            structural.append(None)
            issues.append("pull request body contains a Markdown fence")
            fence_character = marker.group(1)[0]
            fence_length = len(marker.group(1))
            continue
        structural.append(line)
    if fence_character:
        issues.append("pull request body has an unclosed Markdown fence")
    return structural, issues


def _pull_request_body_issues(body: str, state: str) -> list[str]:
    visible_body, issues = _without_html_comments(body)
    lines, fence_issues = _structural_lines(visible_body)
    issues.extend(fence_issues)
    section_positions: list[int] = []
    for section in PR_SECTIONS:
        positions = [index for index, line in enumerate(lines) if line == section]
        if not positions:
            issues.append(f"pull request body is missing {section}")
        elif len(positions) > 1:
            issues.append(f"pull request body repeats {section}")
        else:
            section_positions.append(positions[0])
    unexpected_sections = sorted(
        {
            line
            for line in lines
            if line is not None
            and line.startswith("## ")
            and line not in PR_SECTIONS
        }
    )
    issues.extend(
        f"pull request body has unexpected section {section}"
        for section in unexpected_sections
    )
    ordered_sections = (
        len(section_positions) == len(PR_SECTIONS)
        and section_positions == sorted(section_positions)
    )
    if len(section_positions) == len(PR_SECTIONS) and not ordered_sections:
        issues.append("pull request body sections are out of order")

    section_ranges: dict[str, tuple[int, int]] = {}
    if ordered_sections:
        boundaries = section_positions[1:] + [len(lines)]
        section_ranges = {
            section: (start + 1, end)
            for section, start, end in zip(PR_SECTIONS, section_positions, boundaries)
        }
    for section, expected_fields in PR_FIELDS.items():
        ordered_fields: list[int] = []
        for field in expected_fields:
            pattern = re.compile(rf"^- {re.escape(field)}:\s*(.*)$")
            matches = [
                (index, match)
                for index, line in enumerate(lines)
                if line is not None and (match := pattern.fullmatch(line)) is not None
            ]
            if not matches:
                issues.append(f"pull request body is missing {field} in {section}")
            elif len(matches) > 1:
                issues.append(f"pull request body repeats {field}")
            else:
                position, match = matches[0]
                ordered_fields.append(position)
                if not match.group(1).strip():
                    issues.append(f"pull request body has no value for {field}")
                if ordered_sections:
                    start, end = section_ranges[section]
                    if not start <= position < end:
                        issues.append(f"pull request body misplaces {field} from {section}")
        if len(ordered_fields) == len(expected_fields) and ordered_fields != sorted(
            ordered_fields
        ):
            issues.append(f"pull request body fields are out of order in {section}")

    checklist_positions: list[int] = []
    completion_range = section_ranges.get("## Completion gate")
    for check in PR_CHECKS:
        pattern = re.compile(rf"^- \[([ xX])\] {re.escape(check)}$")
        matches = [
            (index, match)
            for index, line in enumerate(lines)
            if line is not None and (match := pattern.fullmatch(line)) is not None
        ]
        if not matches:
            issues.append(f"pull request body is missing checklist item: {check}")
        elif len(matches) > 1:
            issues.append(f"pull request body repeats checklist item: {check}")
        else:
            position, match = matches[0]
            checklist_positions.append(position)
            if completion_range and not (
                completion_range[0] <= position < completion_range[1]
            ):
                issues.append(f"pull request body misplaces checklist item: {check}")
            if state == "ready" and match.group(1).lower() != "x":
                issues.append(f"ready pull request has unchecked item: {check}")
    if len(checklist_positions) == len(PR_CHECKS):
        if checklist_positions != sorted(checklist_positions):
            issues.append("pull request body checklist items are out of order")
    reference_positions = [
        index
        for index, line in enumerate(lines)
        if line is not None and ISSUE_REFERENCE.fullmatch(line)
    ]
    if len(reference_positions) > 1:
        issues.append("pull request body repeats its issue reference")
    elif (
        reference_positions
        and checklist_positions
        and reference_positions[0] <= checklist_positions[-1]
    ):
        issues.append("pull request body issue reference must follow the checklist")
    if state == "draft" and any(
        lines[position].startswith("Closes ") for position in reference_positions
    ):
        issues.append("draft pull request cannot close issues")
    field_patterns = tuple(
        re.compile(rf"^- {re.escape(field)}:\s*.*$")
        for fields in PR_FIELDS.values()
        for field in fields
    )
    checklist_patterns = tuple(
        re.compile(rf"^- \[[ xX]\] {re.escape(check)}$") for check in PR_CHECKS
    )
    for line_number, line in enumerate(lines, start=1):
        if (
            line is None
            or not line
            or line in PR_SECTIONS
            or ISSUE_REFERENCE.fullmatch(line)
            or any(pattern.fullmatch(line) for pattern in field_patterns)
            or any(pattern.fullmatch(line) for pattern in checklist_patterns)
        ):
            continue
        issues.append(
            "pull request body has unsupported visible content at line "
            f"{line_number}"
        )
    return issues


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
    issues.extend(_pull_request_body_issues(body, state))
    return {"issues": issues}
