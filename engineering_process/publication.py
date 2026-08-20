from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import ContractError
from .git import run_git
from .markdown import (
    COMMENT_RE,
    contains_raw_html,
    mask_nonvisible_markdown_blocks,
    normalized_rendered_inline_text,
    strip_html_comments,
    visible_markdown_links,
)


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
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
CHECKBOX_RE = re.compile(
    r"^\s*-\s+\[(?P<state>[ xX])\]\s+\*\*(?P<label>[^*]+)\*\*(?P<detail>.*)$",
    re.MULTILINE,
)
PROJECT_REQUIREMENT_RE = re.compile(
    r"^- \[(?P<state>[ xX])\] \*\*Project-specific: "
    r"(?P<label>[A-Za-z0-9][A-Za-z0-9 /_.-]{0,99})\*\* "
    r"— (?P<detail>\S.*)$"
)
CHECKLIST_STATUS_RE = re.compile(
    r"\[status:\s*(?P<status>[a-z-]+)\]\s*$", re.IGNORECASE
)
CHECKLIST_REASON_RE = re.compile(
    r"\[reason:\s*(?P<reason>[^\]]*\S[^\]]*)\]", re.IGNORECASE
)
CHECKLIST_STATUSES = {"satisfied", "not-applicable", "pending"}
PR_DESCRIPTION_START = "<!-- engineering-process:pr-description:start -->"
PR_DESCRIPTION_END = "<!-- engineering-process:pr-description:end -->"
_START_TOKEN = "ENGINEERING_PROCESS_MANAGED_PR_DESCRIPTION_START"
_END_TOKEN = "ENGINEERING_PROCESS_MANAGED_PR_DESCRIPTION_END"
RAW_HTML_BLOCK_RE = re.compile(r"(?m)^ {0,3}</?[A-Za-z][^>]*>")
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
STANDARD_REQUIREMENT_DETAILS = {
    "Scope and contract": (
        "— accepted scope is implemented without unapproved expansion."
    ),
    "Verification evidence": (
        "— required current profiles pass on the published checkpoint."
    ),
    "Independent review": (
        "— a separate reviewer approved the published checkpoint with no open "
        "required finding."
    ),
}
STANDARD_EVIDENCE_REFERENCES = {
    "Scope and contract": ("Contract and scope", "evidence: contract"),
    "Verification evidence": ("Verification", "evidence: verification"),
    "Independent review": (
        "Independent review",
        "evidence: independent review",
    ),
}
MAX_MANAGED_LINKS = 64
MAX_EVIDENCE_URL_CHARACTERS = 2048
MAX_MANAGED_URL_BYTES = 32_768


def _normalized_markdown(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _policy_key(text: str) -> str:
    return "".join(
        character
        for character in normalized_rendered_inline_text(text)
        if character.isalnum()
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


def _evidence_url_issues(label: str, destination: str) -> list[str]:
    issues: list[str] = []
    if (
        not destination
        or destination != destination.strip()
        or len(destination) > MAX_EVIDENCE_URL_CHARACTERS
        or any(character.isspace() or ord(character) < 32 for character in destination)
    ):
        return [f"Evidence reference `{label}` has an invalid bounded URL"]
    try:
        target = urlsplit(destination)
        port = target.port
    except ValueError:
        return [f"Evidence reference `{label}` has an invalid URL"]
    if (
        target.scheme.lower() != "https"
        or target.hostname is None
        or target.username is not None
        or target.password is not None
        or port == 0
    ):
        issues.append(
            f"Evidence reference `{label}` must use HTTPS with a host and no credentials"
        )
    return issues


def _evidence_reference_issues(
    sections: dict[str, str], requirement_statuses: dict[str, str]
) -> list[str]:
    issues: list[str] = []
    links: list[tuple[str, str, str]] = []
    for section_name, section in sections.items():
        links.extend(
            (section_name, label, destination)
            for label, destination in visible_markdown_links(section)
        )
    if len(links) > MAX_MANAGED_LINKS:
        issues.append(
            f"Managed PR evidence exceeds {MAX_MANAGED_LINKS} visible links"
        )
    aggregate_url_bytes = sum(
        len(destination.encode("utf-8")) for _, _, destination in links
    )
    if aggregate_url_bytes > MAX_MANAGED_URL_BYTES:
        issues.append(
            f"Managed PR evidence URLs exceed {MAX_MANAGED_URL_BYTES} bytes"
        )

    expected_labels = {
        label for _, label in STANDARD_EVIDENCE_REFERENCES.values()
    }
    references: dict[str, list[tuple[str, str]]] = {
        label: [] for label in expected_labels
    }
    for section_name, label, destination in links:
        normalized_label = normalized_rendered_inline_text(label)
        if normalized_label.startswith("evidence:") and normalized_label not in expected_labels:
            issues.append(f"Unsupported evidence reference label: {label}")
            continue
        if normalized_label not in expected_labels:
            continue
        references[normalized_label].append((section_name, destination))
        issues.extend(_evidence_url_issues(normalized_label, destination))

    for requirement, (owner_section, reference_label) in (
        STANDARD_EVIDENCE_REFERENCES.items()
    ):
        matches = references[reference_label]
        if len(matches) > 1:
            issues.append(
                f"Evidence reference `{reference_label}` must appear exactly once"
            )
        owner_matches = [
            destination
            for section_name, destination in matches
            if section_name == owner_section
        ]
        if any(section_name != owner_section for section_name, _ in matches):
            issues.append(
                f"Evidence reference `{reference_label}` must be in ## {owner_section}"
            )
        status = requirement_statuses.get(requirement)
        if status == "satisfied" and len(owner_matches) != 1:
            issues.append(
                f"Satisfied requirement {requirement} requires one "
                f"[{reference_label}](https://...) link in ## {owner_section}"
            )
        elif status in {"pending", "not-applicable"} and matches:
            issues.append(
                f"Requirement {requirement} with status {status} must not publish "
                f"the completed `{reference_label}` reference"
            )
    return issues


def _managed_span(text: str) -> tuple[int, int]:
    if text.count(PR_DESCRIPTION_START) != 1 or text.count(PR_DESCRIPTION_END) != 1:
        raise ContractError(
            "PR body must contain exactly one engineering-process managed block"
        )
    start_matches = list(
        re.finditer(
            rf"(?m)^{re.escape(PR_DESCRIPTION_START)}$",
            text,
        )
    )
    end_matches = list(
        re.finditer(
            rf"(?m)^{re.escape(PR_DESCRIPTION_END)}$",
            text,
        )
    )
    if len(start_matches) != 1 or len(end_matches) != 1:
        raise ContractError("PR managed markers must each occupy their own line")
    start = start_matches[0]
    end = end_matches[0]
    if start.end() >= end.start():
        raise ContractError("PR managed block markers are out of order")
    return start.start(), end.end()


def managed_pull_request_block(text: str) -> str:
    normalized = _normalized_markdown(text)
    start, end = _managed_span(normalized)
    return normalized[start:end]


def merge_managed_pull_request_template(current: str, block: str) -> str:
    canonical = managed_pull_request_block(block).strip()
    current = _normalized_markdown(current)
    if PR_DESCRIPTION_START not in current and PR_DESCRIPTION_END not in current:
        if current.strip():
            raise ContractError(
                ".github/PULL_REQUEST_TEMPLATE.md: existing unmanaged template must "
                "be migrated around the engineering-process managed block"
            )
        return canonical + "\n"
    start, end = _managed_span(current)
    prefix = current[:start].rstrip()
    suffix = current[end:].strip()
    parts = [part for part in (canonical, prefix, suffix) if part]
    return "\n\n".join(parts) + "\n"


def _visible_managed_content(body: str) -> tuple[str | None, list[str]]:
    try:
        start, _ = _managed_span(body)
    except ContractError as error:
        return None, [str(error)]
    if body[:start].strip():
        return None, [
            "PR managed block must be the first non-whitespace content; append project "
            "extensions after it"
        ]
    if _START_TOKEN in body or _END_TOKEN in body:
        return None, ["PR body contains reserved engineering-process marker text"]
    visible = body.replace(PR_DESCRIPTION_START, _START_TOKEN).replace(
        PR_DESCRIPTION_END, _END_TOKEN
    )
    visible, malformed_comments = strip_html_comments(visible)
    if malformed_comments:
        return None, ["PR body contains an unterminated or malformed HTML comment"]
    if contains_raw_html(visible):
        return None, ["PR body must not contain raw HTML"]
    structural = mask_nonvisible_markdown_blocks(visible)
    lines = structural.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == _START_TOKEN]
    ends = [index for index, line in enumerate(lines) if line.strip() == _END_TOKEN]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None, [
            "PR managed block must be visible and must not be inside a code fence or comment"
        ]
    managed = "\n".join(lines[starts[0] + 1 : ends[0]])
    if RAW_HTML_BLOCK_RE.search(managed):
        return None, ["PR managed block must not contain raw HTML block elements"]
    return managed, []


def managed_pull_request_visibility_issues(body: str) -> list[str]:
    _, issues = _visible_managed_content(_normalized_markdown(body))
    return issues


PROJECT_EXTENSION_HEADING = "## Project-specific requirements"


def validate_project_extensions(body: str, *, allow_pending: bool) -> list[str]:
    body = _normalized_markdown(body)
    _, end = _managed_span(body)
    extension = body[end:]
    if not extension.strip():
        return []
    if contains_raw_html(extension):
        return ["PR extension must not contain raw HTML"]
    lines = extension.strip().splitlines()
    if not lines or lines[0] != PROJECT_EXTENSION_HEADING:
        return [
            "PR extension must use only the supported project-specific checklist grammar"
        ]
    issues: list[str] = []
    labels: set[str] = set()
    requirements = 0
    for line in lines[1:]:
        if not line:
            continue
        match = PROJECT_REQUIREMENT_RE.fullmatch(line)
        if match is None:
            issues.append(
                "Unsupported PR extension content; use `- [ ] **Project-specific: "
                "Label** — detail. [status: pending]`"
            )
            continue
        requirements += 1
        label = match.group("label").strip()
        folded = label.casefold()
        if folded in labels:
            issues.append(f"Duplicate project-specific requirement: {label}")
        labels.add(folded)
        normalized_line = _policy_key(line)
        if any(
            _policy_key(required) in normalized_line
            for required in REQUIRED_REQUIREMENTS
        ):
            issues.append(
                "Project-specific requirement must not restate reserved core policy: "
                + label
            )
        line_status = CHECKLIST_STATUS_RE.search(line)
        if line_status is None:
            issues.append(f"Project-specific requirement is missing a status: {line}")
            continue
        status = line_status.group("status").lower()
        checked = match.group("state").lower() == "x"
        if status not in CHECKLIST_STATUSES:
            issues.append(
                f"Project-specific requirement has invalid status `{status}`: {line}"
            )
        elif status == "pending":
            if checked:
                issues.append(
                    f"Pending project-specific requirement must be unchecked: {line}"
                )
            if not allow_pending:
                issues.append(
                    "Pending project-specific requirement is not ready for publication: "
                    + line
                )
        elif not checked:
            issues.append(
                f"Resolved project-specific requirement must be checked: {line}"
            )
        if (
            status == "not-applicable"
            and CHECKLIST_REASON_RE.search(line) is None
        ):
            issues.append(
                "Not-applicable project-specific requirement must include "
                f"`[reason: ...]`: {line}"
            )
    if requirements == 0:
        issues.append("PR extension must contain a project-specific checklist item")
    return issues


def validate_pr_body(body: str, *, allow_pending: bool) -> list[str]:
    body = _normalized_markdown(body)
    if not _visible(body):
        return ["PR body is empty; use the managed pull-request template"]
    managed, marker_issues = _visible_managed_content(body)
    if managed is None:
        return marker_issues
    headings = [match.group(1).strip() for match in HEADING_RE.finditer(managed)]
    issues = list(marker_issues)
    if tuple(headings) != REQUIRED_SECTIONS:
        issues.append(
            "Managed PR sections must exactly match the canonical order: "
            + ", ".join(f"## {name}" for name in REQUIRED_SECTIONS)
        )
    sections, duplicates = _sections(managed)
    issues.extend(
        f"Duplicate section: ## {name}" for name in sorted(set(duplicates))
    )
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
    if tuple(labels) != REQUIRED_REQUIREMENTS:
        issues.append(
            "Managed standard requirements must exactly match the canonical order: "
            + ", ".join(REQUIRED_REQUIREMENTS)
        )
    for required in REQUIRED_REQUIREMENTS:
        count = labels.count(required)
        if count == 0:
            issues.append(f"Missing standard requirement: {required}")
        elif count > 1:
            issues.append(f"Duplicate standard requirement: {required}")

    requirement_statuses: dict[str, str] = {}
    for match in checkboxes:
        line = match.group(0).strip()
        label = match.group("label").strip()
        checked = match.group("state").lower() == "x"
        status_match = CHECKLIST_STATUS_RE.search(line)
        if status_match is None:
            issues.append(f"Requirement is missing a structured status: {line}")
            continue
        status = status_match.group("status").lower()
        if status not in CHECKLIST_STATUSES:
            issues.append(f"Requirement has invalid status `{status}`: {line}")
            continue
        requirement_statuses[label] = status
        detail = match.group("detail").strip()
        detail_without_status = CHECKLIST_STATUS_RE.sub("", detail).strip()
        reason_match = CHECKLIST_REASON_RE.search(detail_without_status)
        if reason_match is not None:
            if detail_without_status[reason_match.end() :].strip():
                issues.append(f"Requirement reason must precede its status: {line}")
            detail_without_status = (
                detail_without_status[: reason_match.start()].strip()
            )
        expected_detail = STANDARD_REQUIREMENT_DETAILS.get(label)
        if expected_detail is not None and detail_without_status != expected_detail:
            issues.append(
                f"Standard requirement text must remain canonical for {label}"
            )
        if status == "pending":
            if checked:
                issues.append(f"Pending requirement must be unchecked: {line}")
            if not allow_pending:
                issues.append(f"Pending requirement is not ready for publication: {line}")
        elif not checked:
            issues.append(f"Resolved requirement must be checked: {line}")
        if status == "not-applicable" and reason_match is None:
            issues.append(
                f"Not-applicable requirement must include `[reason: ...]`: {line}"
            )
    issues.extend(_evidence_reference_issues(sections, requirement_statuses))
    issues.extend(validate_project_extensions(body, allow_pending=allow_pending))
    return issues


def validate_pull_request(
    *, title: str, body: str, branch: str, state: str
) -> list[str]:
    if state not in {"draft", "ready"}:
        raise ContractError("pull-request state must be draft or ready")
    allow_pending = state == "draft"
    return [
        *validate_branch(branch),
        *validate_pr_title(title),
        *validate_pr_body(body, allow_pending=allow_pending),
    ]


def commit_subjects(project_root: Path, range_spec: str) -> list[tuple[str, str]]:
    if GIT_RANGE_RE.fullmatch(range_spec) is None:
        raise ContractError(f"invalid Git revision or range: {range_spec}")
    result = run_git(
        project_root,
        ["log", "--format=%H%x00%s", range_spec, "--"],
        label=f"inspect commit range {range_spec}",
        timeout_seconds=30,
        max_stdout_bytes=1_000_000,
    )
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
        if len(records) > 5_000:
            raise ContractError("commit range exceeds 5000 commits")
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
