from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import stat
from pathlib import Path

from .contracts import (
    AutomationProposal,
    ContractError,
    MAX_AUTOMATION_PROPOSAL_PATHS,
    MAX_JSON_BYTES,
    canonical_json_digest,
    validate_adoption_migration,
    validate_automation_policy,
    validate_process_lock,
)
from .git import portable_git_path, run_git
from .artifact_attestation import validate_distribution_attestation
from .release import validate_release_checkpoint
from .markdown import (
    COMMENT_RE,
    contains_raw_html,
    mask_nonvisible_markdown_blocks,
    normalized_rendered_inline_text,
    strip_html_comments,
)


CONVENTIONAL_SUBJECT_MAX_LENGTH = 72
MAX_PULL_REQUEST_BODY_BYTES = 65_536
MAX_PROPOSAL_CHANGED_PATH_BYTES = 600_000
PROTECTED_AUTOMATION_PROPOSAL_PATHS = {
    ".github/CODEOWNERS",
    "AGENTS.md",
    "CODEOWNERS",
    "RELEASING.md",
    "SECURITY.md",
    "release.json",
    "requirements/process.in",
    "requirements/process.txt",
}
PROTECTED_AUTOMATION_PROPOSAL_PREFIXES = (
    ".agents/",
    ".github/workflows/",
    ".process/",
    ".release/",
    "release-changes/",
)
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
RENOVATE_DEBUG_COMMENT_RE = re.compile(
    r"^<!--renovate-debug:(?P<payload>[A-Za-z0-9+/]{1,4096}={0,2})-->$"
)
RENOVATE_DEBUG_KEYS = {
    "createdInVer",
    "updatedInVer",
    "targetBranch",
    "labels",
}


def _strip_renovate_debug_comment(extension: str) -> tuple[str, list[str]]:
    lines = extension.strip().splitlines()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := RENOVATE_DEBUG_COMMENT_RE.fullmatch(line)) is not None
    ]
    if not matches:
        return extension, []
    if len(matches) != 1 or matches[0][0] != len(lines) - 1:
        return extension, ["Renovate metadata comment must appear exactly once at the end"]
    _, match = matches[0]
    try:
        decoded = base64.b64decode(match.group("payload"), validate=True)
        if len(decoded) > 3072:
            raise ValueError("payload exceeds 3072 decoded bytes")
        metadata = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return extension, ["Renovate metadata comment must contain bounded base64 JSON"]
    if not isinstance(metadata, dict) or set(metadata) != RENOVATE_DEBUG_KEYS:
        return extension, ["Renovate metadata comment has an unexpected JSON contract"]
    if any(
        not isinstance(metadata[key], str)
        or not metadata[key]
        or len(metadata[key]) > 64
        for key in ("createdInVer", "updatedInVer")
    ):
        return extension, ["Renovate metadata versions must be bounded strings"]
    if metadata["targetBranch"] != "main":
        return extension, ["Renovate metadata must target protected main"]
    labels = metadata["labels"]
    if (
        not isinstance(labels, list)
        or len(labels) > 50
        or any(not isinstance(label, str) or len(label) > 100 for label in labels)
    ):
        return extension, ["Renovate metadata labels must be a bounded string list"]
    return "\n".join(lines[:-1]).strip(), []


def validate_project_extensions(body: str, *, allow_pending: bool) -> list[str]:
    body = _normalized_markdown(body)
    _, end = _managed_span(body)
    extension = body[end:]
    if not extension.strip():
        return []
    extension, metadata_issues = _strip_renovate_debug_comment(extension)
    if metadata_issues:
        return metadata_issues
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


def validate_completed_publication(
    *,
    title: str,
    body: str,
    branch: str,
    commit: str,
    lifecycle: dict[str, object],
    source: dict[str, object],
) -> list[str]:
    issues = [
        *validate_branch(branch),
        *validate_pr_title(title),
        *validate_pr_body(body, allow_pending=False),
    ]
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        issues.append("Publication commit must be a full lowercase Git SHA")
    if source.get("dirty") is not False:
        issues.append("Publication source must have a clean working tree")
    if source.get("checkpoint") != commit:
        issues.append("Publication commit does not match the current source checkpoint")
    if source.get("fingerprint") is None:
        issues.append("Publication source workspace fingerprint is unavailable")
    if lifecycle.get("phase") != "completed" or lifecycle.get("completion") is None:
        issues.append("Source publication requires a completed lifecycle")
    if lifecycle.get("current") is not True:
        issues.append("Source publication requires current lifecycle evidence")
    if lifecycle.get("pendingFindings") != []:
        issues.append("Source publication requires every finding to be resolved")
    verification = lifecycle.get("verification")
    if not isinstance(verification, list) or not verification:
        issues.append("Source publication requires verification evidence")
    else:
        checkpoints = {
            item.get("checkpoint") for item in verification if isinstance(item, dict)
        }
        fingerprints = {
            item.get("workspaceFingerprint")
            for item in verification
            if isinstance(item, dict)
        }
        if checkpoints != {commit} or fingerprints != {source.get("fingerprint")}:
            issues.append(
                "Source publication verification does not match the current checkpoint"
            )
    return issues


def validate_evidence_publication(
    *,
    title: str,
    body: str,
    branch: str,
    commit: str,
    project: str,
    evidence: dict[str, object],
    source: dict[str, object],
) -> list[str]:
    issues = [
        *validate_branch(branch),
        *validate_pr_title(title),
        *validate_pr_body(body, allow_pending=False),
    ]
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        issues.append("Publication commit must be a full lowercase Git SHA")
    if source.get("dirty") is not False:
        issues.append("Publication source must have a clean working tree")
    if source.get("checkpoint") != commit:
        issues.append("Publication commit does not match the current source checkpoint")
    if source.get("fingerprint") is None:
        issues.append("Publication source workspace fingerprint is unavailable")
    if evidence.get("project") != project:
        issues.append("Completion evidence does not identify the publication project")
    if evidence.get("checkpoint") != commit:
        issues.append("Completion evidence does not match the publication commit")
    if evidence.get("workspaceFingerprint") != source.get("fingerprint"):
        issues.append("Completion evidence does not match the publication workspace")
    return issues


def _proposal_changed_paths(
    project_root: Path, *, base_sha: str, head_sha: str, exact_base: bool = False
) -> tuple[str, ...]:
    result = run_git(
        project_root,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=ACMRDT",
            f"{base_sha}{'..' if exact_base else '...'}{head_sha}",
            "--",
        ],
        label="inspect controlled automation proposal paths",
        timeout_seconds=30,
        max_stdout_bytes=MAX_PROPOSAL_CHANGED_PATH_BYTES,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            "cannot inspect controlled automation proposal paths"
            + (f": {detail}" if detail else "")
        )
    records = result.stdout.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    paths: set[str] = set()
    index = 0
    while index < len(records):
        try:
            status = records[index].decode("ascii")
        except UnicodeDecodeError as error:
            raise ContractError(
                "controlled automation proposal status must be ASCII"
            ) from error
        index += 1
        if re.fullmatch(r"(?:[ADMT]|[CR][0-9]{1,3})", status) is None:
            raise ContractError(
                f"controlled automation proposal has unsupported Git status: {status}"
            )
        path_count = 2 if status.startswith(("C", "R")) else 1
        if index + path_count > len(records):
            raise ContractError(
                "controlled automation proposal Git status record is truncated"
            )
        for encoded_path in records[index : index + path_count]:
            paths.add(
                portable_git_path(
                    encoded_path,
                    label="controlled automation proposal changed path",
                )
            )
        index += path_count
    ordered_paths = tuple(sorted(paths))
    if not ordered_paths or len(ordered_paths) > MAX_AUTOMATION_PROPOSAL_PATHS:
        raise ContractError(
            "controlled automation proposal must contain between 1 and "
            f"{MAX_AUTOMATION_PROPOSAL_PATHS} changed paths"
        )
    return ordered_paths


def _proposal_base_is_ancestor(
    project_root: Path, *, base_sha: str, head_sha: str
) -> bool:
    result = run_git(
        project_root,
        ["merge-base", "--is-ancestor", base_sha, head_sha],
        label="validate process-adoption base ancestry",
        timeout_seconds=30,
        max_stdout_bytes=128,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    raise ContractError(
        "cannot validate process-adoption base ancestry"
        + (f": {detail}" if detail else "")
    )


def _proposal_base_policy(
    project_root: Path, *, base_sha: str, policy_path: str
) -> dict[str, object]:
    result = run_git(
        project_root,
        ["show", f"{base_sha}:{policy_path}"],
        label="read controlled automation proposal opt-in policy",
        timeout_seconds=30,
        max_stdout_bytes=MAX_JSON_BYTES,
    )
    if result.returncode != 0:
        raise ContractError(
            "controlled automation proposals require an opt-in policy on the base"
        )
    try:
        document = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(
            "base automation-proposal opt-in policy must be valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict):
        raise ContractError("base automation-proposal opt-in policy must be an object")
    return document


def _proposal_git_blob(
    project_root: Path, *, commit: str, path: str, label: str
) -> bytes:
    result = run_git(
        project_root,
        ["show", f"{commit}:{path}"],
        label=label,
        timeout_seconds=30,
        max_stdout_bytes=MAX_JSON_BYTES,
    )
    if result.returncode != 0:
        raise ContractError(f"{label} is missing from {commit[:12]}")
    return result.stdout


def _proposal_external_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label} must be a regular non-symlink file")
        if before.st_size > maximum:
            raise ContractError(f"{label} exceeds {maximum} bytes")
        content = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"{label} changed while reading")
    return content


def _proposal_external_path(
    project_root: Path, path: Path, *, label: str, directory: bool
) -> Path:
    try:
        supplied_metadata = path.lstat()
        if stat.S_ISLNK(supplied_metadata.st_mode):
            raise ContractError(f"{label} must not be a symlink")
        resolved = path.resolve(strict=True)
        project = project_root.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise ContractError(f"cannot resolve {label}: {error}") from error
    if resolved.is_symlink() or (
        directory and not stat.S_ISDIR(metadata.st_mode)
    ) or (not directory and not stat.S_ISREG(metadata.st_mode)):
        expected = "directory" if directory else "regular file"
        raise ContractError(f"{label} must be a non-symlink {expected}")
    try:
        resolved.relative_to(project)
    except ValueError:
        return resolved
    raise ContractError(f"{label} must stay outside the consumer checkout")


def _proposal_producer_repository(project_root: Path) -> str:
    result = run_git(
        project_root,
        ["remote", "get-url", "origin"],
        label="resolve process-adoption producer repository",
        timeout_seconds=30,
        max_stdout_bytes=2048,
    )
    if result.returncode != 0:
        raise ContractError("producer checkout requires an origin remote")
    try:
        url = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError("producer origin URL must be UTF-8") from error
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)(?P<repository>"
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        url,
    )
    if match is None:
        raise ContractError("producer origin must be an exact GitHub repository URL")
    return match.group("repository")


def _validate_process_adoption_producer_inputs(
    project_root: Path,
    *,
    proposal: AutomationProposal,
    producer_inputs: dict[str, Path] | None,
) -> Path:
    if proposal.process_adoption is None:
        raise ContractError("process-adoption evidence is missing")
    if producer_inputs is None:
        raise ContractError(
            "process-adoption requires independently supplied producer checkout, "
            "artifacts, receipt, and attestation"
        )
    expected_keys = {"root", "artifacts", "receipt", "attestation"}
    if set(producer_inputs) != expected_keys:
        raise ContractError(
            "process-adoption producer inputs must contain root, artifacts, receipt, "
            "and attestation"
        )
    producer_root = _proposal_external_path(
        project_root,
        producer_inputs["root"],
        label="producer checkout",
        directory=True,
    )
    artifact_root = _proposal_external_path(
        project_root,
        producer_inputs["artifacts"],
        label="producer artifact root",
        directory=True,
    )
    receipt_path = _proposal_external_path(
        project_root,
        producer_inputs["receipt"],
        label="producer lifecycle receipt",
        directory=False,
    )
    attestation_path = _proposal_external_path(
        project_root,
        producer_inputs["attestation"],
        label="producer distribution attestation",
        directory=False,
    )
    adoption = proposal.process_adoption
    producer = adoption["producerRelease"]
    if _proposal_producer_repository(producer_root) != producer["repository"]:
        raise ContractError(
            "producer checkout origin does not match protected-base policy"
        )
    release_bytes = _proposal_external_bytes(
        producer_root / "release.json",
        maximum=MAX_JSON_BYTES,
        label="producer release contract",
    )
    release_binding = producer["releaseContract"]
    if (
        release_bytes != release_binding["content"].encode("utf-8")
        or _proposal_blob_digest(release_bytes) != release_binding["sha256"]
    ):
        raise ContractError(
            "independent producer release contract does not match proposal evidence"
        )
    attestation_bytes = _proposal_external_bytes(
        attestation_path,
        maximum=256_000,
        label="producer distribution attestation",
    )
    attestation_binding = producer["distributionAttestation"]
    if (
        attestation_bytes != attestation_binding["content"].encode("utf-8")
        or _proposal_blob_digest(attestation_bytes) != attestation_binding["sha256"]
    ):
        raise ContractError(
            "independent producer attestation does not match proposal evidence"
        )
    release_result = validate_release_checkpoint(
        producer_root,
        tag=producer["tag"],
        release_name=producer["tag"],
        commit=producer["commit"],
        main_ref="origin/main",
        receipt_path=receipt_path,
        authorization_path=None,
        reviewed_commit=None,
    )
    if (
        release_result["checkpoint"] != producer["commit"]
        or release_result["version"] != producer["version"]
        or release_result["tag"] != producer["tag"]
    ):
        raise ContractError("independent producer release identity is mismatched")
    validated_attestation = validate_distribution_attestation(
        producer_root,
        artifact_root,
        attestation_path,
        receipt_path=receipt_path,
        authorization_path=None,
        checkpoint=producer["commit"],
    )
    if validated_attestation != json.loads(attestation_binding["content"]):
        raise ContractError(
            "independent producer artifact validation does not match proposal evidence"
        )
    return producer_root


def _proposal_blob_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _proposal_json_blob(content: bytes, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} must be valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ContractError(f"{label} must be a JSON object")
    return document


def _proposal_managed_tree_paths(
    project_root: Path, *, commit: str, skills: tuple[str, ...]
) -> tuple[str, ...]:
    roots = [
        "AGENTS.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".process/adopt-process.py",
        ".process/adopt-process-windows-job.py",
        ".agents/.gitattributes",
        *(f".agents/skills/{skill}" for skill in skills),
    ]
    result = run_git(
        project_root,
        ["ls-tree", "-r", "-z", commit, "--", *roots],
        label="inspect process-adoption managed tree",
        timeout_seconds=30,
        max_stdout_bytes=MAX_PROPOSAL_CHANGED_PATH_BYTES,
    )
    if result.returncode != 0:
        raise ContractError("cannot inspect process-adoption managed tree")
    paths: list[str] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded_path = record.partition(b"\t")
        parts = metadata.split(b" ")
        if not separator or len(parts) != 3 or parts[1] != b"blob":
            raise ContractError("process-adoption managed tree contains an invalid entry")
        if parts[0] not in {b"100644", b"100755"}:
            raise ContractError(
                "process-adoption managed files must be regular Git blobs"
            )
        paths.append(
            portable_git_path(
                encoded_path,
                label="process-adoption managed path",
            )
        )
    ordered = tuple(sorted(paths))
    if len(ordered) > MAX_AUTOMATION_PROPOSAL_PATHS:
        raise ContractError("process-adoption managed tree exceeds the file bound")
    return ordered


def _proposal_workflow_tree(
    project_root: Path, *, commit: str
) -> dict[str, tuple[str, bytes]]:
    result = run_git(
        project_root,
        ["ls-tree", "-r", "-z", commit, "--", ".github/workflows"],
        label="inspect process-adoption workflow tree",
        timeout_seconds=30,
        max_stdout_bytes=MAX_PROPOSAL_CHANGED_PATH_BYTES,
    )
    if result.returncode != 0:
        raise ContractError("cannot inspect process-adoption workflow tree")
    entries: dict[str, tuple[str, bytes]] = {}
    total_bytes = 0
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded_path = record.partition(b"\t")
        parts = metadata.split(b" ")
        if not separator or len(parts) != 3:
            raise ContractError("process-adoption workflow tree has an invalid entry")
        path = portable_git_path(
            encoded_path,
            label="process-adoption workflow path",
        )
        if not path.endswith((".yml", ".yaml")):
            continue
        if len(entries) >= MAX_AUTOMATION_PROPOSAL_PATHS:
            raise ContractError("process-adoption workflow tree exceeds the file bound")
        content = _proposal_git_blob(
            project_root,
            commit=commit,
            path=path,
            label=f"process-adoption workflow {path}",
        )
        total_bytes += len(content)
        if total_bytes > 8_000_000:
            raise ContractError(
                "process-adoption workflow tree exceeds 8000000 bytes"
            )
        try:
            mode = parts[0].decode("ascii")
            object_type = parts[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise ContractError(
                "process-adoption workflow metadata must be ASCII"
            ) from error
        if object_type != "blob":
            raise ContractError(
                f"process-adoption workflow {path} must be a Git blob"
            )
        entries[path] = (mode, content)
    return entries


YAML_MAPPING_LINE_RE = re.compile(
    r"^[ \t]*(?:-[ \t]*)?"
    r"(?P<key>\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|[A-Za-z_][A-Za-z0-9_-]*)"
    r"[ \t]*:[ \t]*"
    r"(?P<scalar>\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|[^#\r\n]*?)"
    r"[ \t]*(?:#[ \t]*(?P<tag>\S+))?[ \t]*$"
)
YAML_MAPPING_KEY_PREFIX_RE = re.compile(
    r"^[ \t]*(?:-[ \t]*)?"
    r"(?P<key>\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|[A-Za-z_][A-Za-z0-9_-]*)"
    r"[ \t]*:"
)


def _decode_yaml_double_quoted_scalar(value: str) -> str:
    result: list[str] = []
    index = 0
    simple = {
        "0": "\0",
        "a": "\a",
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "v": "\v",
        "f": "\f",
        "r": "\r",
        "e": "\x1b",
        " ": " ",
        '"': '"',
        "/": "/",
        "\\": "\\",
        "N": "\u0085",
        "_": "\u00a0",
        "L": "\u2028",
        "P": "\u2029",
    }
    while index < len(value):
        character = value[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        index += 1
        if index >= len(value):
            raise ContractError("unterminated YAML escape")
        escape = value[index]
        index += 1
        if escape in simple:
            result.append(simple[escape])
            continue
        digits = {"x": 2, "u": 4, "U": 8}.get(escape)
        if digits is None or index + digits > len(value):
            raise ContractError("unsupported YAML escape")
        encoded = value[index : index + digits]
        if re.fullmatch(r"[0-9A-Fa-f]+", encoded) is None:
            raise ContractError("invalid YAML Unicode escape")
        codepoint = int(encoded, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ContractError("invalid YAML Unicode codepoint")
        result.append(chr(codepoint))
        index += digits
    return "".join(result)


def _proposal_yaml_uses(
    text: str, *, path: str
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
    values: list[tuple[str, str, str | None]] = []
    issues: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = YAML_MAPPING_LINE_RE.fullmatch(line)
        if match is None:
            prefix = YAML_MAPPING_KEY_PREFIX_RE.match(line)
            if prefix is not None:
                raw_key = prefix.group("key")
                try:
                    if raw_key.startswith('"'):
                        key = _decode_yaml_double_quoted_scalar(raw_key[1:-1])
                    elif raw_key.startswith("'"):
                        key = raw_key[1:-1].replace("''", "'")
                    else:
                        key = raw_key
                except ContractError:
                    key = None
                if key == "uses":
                    issues.append(
                        f"Process-adoption workflow {path}:{line_number} uses a "
                        "multiline or unsupported scalar; use one literal line"
                    )
            continue
        raw_key = match.group("key")
        try:
            if raw_key.startswith('"'):
                key = _decode_yaml_double_quoted_scalar(raw_key[1:-1])
            elif raw_key.startswith("'"):
                key = raw_key[1:-1].replace("''", "'")
            else:
                key = raw_key
        except ContractError as error:
            issues.append(
                f"Process-adoption workflow {path}:{line_number} has an invalid "
                f"mapping key: {error}"
            )
            continue
        if key != "uses":
            continue
        raw = match.group("scalar").strip()
        if (
            (raw.startswith(('"', "'")) and not raw.endswith(raw[0]))
            or raw.endswith("\\")
        ):
            issues.append(
                f"Process-adoption workflow {path}:{line_number} uses a multiline "
                "or unsupported scalar; use one literal line"
            )
            continue
        if raw in {"|", "|-", "|+", ">", ">-", ">+"}:
            issues.append(
                f"Process-adoption workflow {path}:{line_number} uses a multiline "
                "scalar; use one literal line"
            )
            continue
        try:
            if raw.startswith('"'):
                decoded = _decode_yaml_double_quoted_scalar(raw[1:-1])
            elif raw.startswith("'"):
                decoded = raw[1:-1].replace("''", "'")
            else:
                decoded = raw
        except ContractError as error:
            issues.append(
                f"Process-adoption workflow {path}:{line_number} has an invalid "
                f"uses scalar: {error}"
            )
            continue
        values.append((raw, decoded, match.group("tag")))
    return values, issues


def _proposal_requirement_hashes(
    lock_text: str, *, version: str
) -> set[str] | None:
    lines = lock_text.splitlines()
    pin = re.compile(
        rf"^engineering-process=={re.escape(version)}[ \t]+\\$"
    )
    indexes = [index for index, line in enumerate(lines) if pin.fullmatch(line)]
    if len(indexes) != 1:
        return None
    hashes: set[str] = set()
    hash_line = re.compile(
        r"^--hash=(?P<digest>sha256:[0-9a-f]{64})(?:[ \t]+\\)?$"
    )
    for line in lines[indexes[0] + 1 :]:
        if not line.startswith((" ", "\t")):
            break
        stripped = line.strip()
        match = hash_line.fullmatch(stripped)
        if match is not None:
            hashes.add(match.group("digest"))
    return hashes


def _validate_process_adoption_workflows(
    project_root: Path,
    *,
    base_commit: str,
    head_commit: str,
    action_pins: list[dict[str, object]],
) -> list[str]:
    issues: list[str] = []
    pins_by_path: dict[str, list[dict[str, object]]] = {}
    for pin in action_pins:
        pins_by_path.setdefault(str(pin["path"]), []).append(pin)
    producer_repository = str(action_pins[0]["repository"])
    base_tree = _proposal_workflow_tree(project_root, commit=base_commit)
    head_tree = _proposal_workflow_tree(project_root, commit=head_commit)
    observed_paths: set[str] = set()
    decoded: dict[tuple[str, str], str] = {}
    usages: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for tree_name, tree in (("base", base_tree), ("head", head_tree)):
        for path, (_mode, content) in tree.items():
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(f"Process-adoption workflow {path} must be UTF-8")
                continue
            decoded[(tree_name, path)] = text
            values, scalar_issues = _proposal_yaml_uses(text, path=path)
            issues.extend(scalar_issues)
            matches: list[tuple[str, str | None]] = []
            producer_prefix = producer_repository + "@"
            normalized_producer_prefix = producer_prefix.casefold()
            recognized_literal_occurrences = 0
            for raw, scalar, tag in values:
                if not scalar.casefold().startswith(normalized_producer_prefix):
                    if producer_repository.casefold() in scalar.casefold():
                        issues.append(
                            f"Process-adoption workflow {path} contains an unsupported "
                            "producer action identity"
                        )
                    continue
                observed_paths.add(path)
                if not scalar.startswith(producer_prefix):
                    issues.append(
                        f"Process-adoption workflow {path} changes the protected "
                        "producer repository spelling"
                    )
                if producer_prefix not in raw:
                    issues.append(
                        f"Process-adoption workflow {path} escapes the producer "
                        "repository; use its literal protected-base identity"
                    )
                else:
                    recognized_literal_occurrences += 1
                matches.append((scalar[len(producer_prefix) :], tag))
            if text.count(producer_repository) != recognized_literal_occurrences:
                issues.append(
                    f"Process-adoption workflow {path} contains the producer "
                    "repository outside a supported literal uses scalar"
                )
            if matches:
                usages[(tree_name, path)] = matches
    declared_paths = set(pins_by_path)
    omitted_paths = sorted(observed_paths - declared_paths)
    extra_paths = sorted(declared_paths - observed_paths)
    if omitted_paths:
        issues.append(
            "Process-adoption actionPins omits producer workflow: "
            + omitted_paths[0]
        )
    if extra_paths:
        issues.append(
            "Process-adoption actionPins declares a workflow without the producer "
            "action: "
            + extra_paths[0]
        )
    for path, pins in pins_by_path.items():
        base_entry = base_tree.get(path)
        head_entry = head_tree.get(path)
        if base_entry is None or head_entry is None:
            issues.append(
                f"Process-adoption workflow {path} must exist on base and head"
            )
            continue
        base_mode, _base = base_entry
        head_mode, _head = head_entry
        if (
            base_mode not in {"100644", "100755"}
            or head_mode != base_mode
        ):
            issues.append(
                f"Process-adoption workflow {path} must remain a regular blob with "
                "unchanged mode"
            )
        expected = decoded.get(("base", path))
        head_text = decoded.get(("head", path))
        if expected is None or head_text is None:
            continue
        for pin in pins:
            previous_identity = (
                str(pin["previousCommit"]),
                str(pin["previousReleaseTag"]),
            )
            target_identity = (
                str(pin["targetCommit"]),
                str(pin["targetReleaseTag"]),
            )
            if any(
                identity != previous_identity
                for identity in usages.get(("base", path), [])
            ):
                issues.append(
                    f"Process-adoption base workflow {path} contains an undeclared "
                    "producer action identity"
                )
            if any(
                identity != target_identity
                for identity in usages.get(("head", path), [])
            ):
                issues.append(
                    f"Process-adoption candidate workflow {path} retains a stale or "
                    "unverified producer action identity"
                )
            pattern = re.compile(
                rf"(?m)(uses[ \t]*:[ \t]*(?P<quote>['\"]?)"
                rf"{re.escape(str(pin['repository']))}@)"
                rf"{re.escape(str(pin['previousCommit']))}"
                rf"(?P=quote)([ \t]+#[ \t]*)"
                rf"{re.escape(str(pin['previousReleaseTag']))}"
                r"([ \t]*)$"
            )
            expected, replacements = pattern.subn(
                lambda match: (
                    match.group(1)
                    + str(pin["targetCommit"])
                    + match.group("quote")
                    + match.group(3)
                    + str(pin["targetReleaseTag"])
                    + match.group(4)
                ),
                expected,
            )
            if replacements == 0:
                issues.append(
                    f"Process-adoption workflow {path} does not contain the exact "
                    "declared previous action pin"
                )
        if expected != head_text:
            issues.append(
                f"Process-adoption workflow {path} contains changes beyond the "
                "declared immutable action-pin replacements"
            )
    return issues


def _validate_process_adoption_candidate(
    project_root: Path,
    *,
    base_commit: str,
    proposal: AutomationProposal,
    changed_paths: tuple[str, ...],
    producer_inputs: dict[str, Path] | None,
) -> list[str]:
    if proposal.process_adoption is None:
        return ["Process-adoption evidence is missing"]
    adoption = proposal.process_adoption
    issues: list[str] = []
    try:
        producer_root = _validate_process_adoption_producer_inputs(
            project_root,
            proposal=proposal,
            producer_inputs=producer_inputs,
        )
    except ContractError as error:
        return [f"Process-adoption producer evidence is invalid: {error}"]
    producer_release = adoption["producerRelease"]
    source_authority = adoption["sourceAuthority"]
    target_authority = adoption["targetAuthority"]
    requirements = adoption["requirements"]
    process_lock_binding = adoption["processLock"]
    migration = adoption["projectMigration"]
    managed_files = adoption["managedFiles"]
    action_pins = adoption["actionPins"]

    bindings = [
        (requirements["inputPath"], requirements["inputSha256"], "requirements input"),
        (requirements["lockPath"], requirements["lockSha256"], "requirements lock"),
        (process_lock_binding["path"], process_lock_binding["sha256"], "process lock"),
        (migration["projectPath"], migration["projectSha256"], "project manifest"),
        *(
            [(migration["path"], migration["sha256"], "adoption migration")]
            if migration["status"] == "applied"
            else []
        ),
        *((item["path"], item["sha256"], "managed file") for item in managed_files),
    ]
    blob_cache: dict[str, bytes] = {}
    total_managed_bytes = 0
    for bound_path, digest, label in bindings:
        content = blob_cache.setdefault(
            str(bound_path),
            _proposal_git_blob(
                project_root,
                commit=proposal.head_sha,
                path=str(bound_path),
                label=f"process-adoption {label} {bound_path}",
            ),
        )
        if label == "managed file":
            total_managed_bytes += len(content)
            if total_managed_bytes > 8_000_000:
                raise ContractError(
                    "process-adoption managed distribution exceeds 8000000 bytes"
                )
        if _proposal_blob_digest(content) != digest:
            issues.append(
                f"Process-adoption {label} {bound_path} digest does not match evidence"
            )

    process_lock_content = blob_cache[str(process_lock_binding["path"])]
    process_lock = validate_process_lock(
        _proposal_json_blob(process_lock_content, label="process-adoption process lock"),
        "process-adoption process lock",
    )
    if (
        process_lock.version != target_authority["version"]
        or process_lock.digest != target_authority["processDigest"]
    ):
        issues.append(
            "Process-adoption process lock does not match the target authority"
        )

    base_lock_content = _proposal_git_blob(
        project_root,
        commit=base_commit,
        path=str(process_lock_binding["path"]),
        label="process-adoption protected-base process lock",
    )
    base_lock = validate_process_lock(
        _proposal_json_blob(base_lock_content, label="protected-base process lock"),
        "protected-base process lock",
    )
    if (
        base_lock.version != source_authority["version"]
        or base_lock.digest != source_authority["processDigest"]
    ):
        issues.append(
            "Process-adoption protected-base process lock does not match the "
            "source authority"
        )
    dropped_skills = sorted(set(base_lock.skills) - set(process_lock.skills))
    if dropped_skills:
        issues.append(
            "Process-adoption process lock drops a selected source skill: "
            + dropped_skills[0]
        )
    from .syncing import synchronized_state

    issues.extend(
        "Process-adoption target materialization is invalid: " + issue
        for issue in synchronized_state(
            project_root,
            producer_root,
            process_lock,
            authority_version=str(producer_release["version"]),
            package_root=producer_root / "engineering_process",
        )
    )

    expected_managed_paths = _proposal_managed_tree_paths(
        project_root,
        commit=proposal.head_sha,
        skills=process_lock.skills,
    )
    declared_managed_paths = tuple(item["path"] for item in managed_files)
    if declared_managed_paths != expected_managed_paths:
        issues.append(
            "Process-adoption managedFiles does not describe the complete selected "
            "managed distribution"
        )

    input_content = blob_cache[str(requirements["inputPath"])]
    lock_content = blob_cache[str(requirements["lockPath"])]
    target_version = re.escape(str(target_authority["version"]))
    direct_pin = re.compile(
        rf"(?m)^engineering-process=={target_version}$"
    )
    try:
        input_text = input_content.decode("utf-8")
        lock_text = lock_content.decode("utf-8")
    except UnicodeDecodeError:
        issues.append("Process-adoption requirements must be UTF-8")
    else:
        if len(direct_pin.findall(input_text)) != 1:
            issues.append(
                "Process-adoption requirements input must contain one exact target pin"
            )
        requirement_hashes = _proposal_requirement_hashes(
            lock_text,
            version=str(target_authority["version"]),
        )
        if not requirement_hashes:
            issues.append(
                "Process-adoption requirements lock must contain one exact hash-locked "
                "target pin"
            )
        attestation = json.loads(
            producer_release["distributionAttestation"]["content"]
        )
        wheel_artifacts = [
            item
            for item in attestation["artifacts"]
            if item["name"].endswith(".whl")
        ]
        verified_wheel_hash = (
            wheel_artifacts[0]["sha256"] if len(wheel_artifacts) == 1 else None
        )
        if requirement_hashes != {verified_wheel_hash}:
            issues.append(
                "Process-adoption target requirement hashes do not equal the verified "
                "producer wheel hash"
            )

    head_project = blob_cache[str(migration["projectPath"])]
    base_project = _proposal_git_blob(
        project_root,
        commit=base_commit,
        path=str(migration["projectPath"]),
        label="process-adoption protected-base project manifest",
    )
    if migration["status"] == "not-required":
        if head_project != base_project:
            issues.append(
                "Process-adoption project manifest changed without an applied migration"
            )
    else:
        migration_document = _proposal_json_blob(
            blob_cache[str(migration["path"])],
            label="process-adoption migration",
        )
        validate_adoption_migration(
            migration_document, "process-adoption migration"
        )
        if (
            migration_document["fromProcessVersion"] != source_authority["version"]
            or migration_document["toProcessVersion"] != target_authority["version"]
            or migration_document["sourceProjectDigest"]
            != _proposal_blob_digest(base_project)
            or migration_document["targetProjectDigest"]
            != _proposal_blob_digest(head_project)
        ):
            issues.append(
                "Process-adoption migration does not bind the exact source and target"
            )

    required_changed_paths = {
        str(requirements["inputPath"]),
        str(requirements["lockPath"]),
        str(process_lock_binding["path"]),
        *(str(pin["path"]) for pin in action_pins),
    }
    if migration["status"] == "applied":
        required_changed_paths.update(
            {str(migration["path"]), str(migration["projectPath"])}
        )
    allowed_changed_paths = required_changed_paths | set(declared_managed_paths)
    missing = sorted(required_changed_paths - set(changed_paths))
    unauthorized = sorted(set(changed_paths) - allowed_changed_paths)
    if missing:
        issues.append(
            "Process-adoption proposal omits required materialized path: " + missing[0]
        )
    if unauthorized:
        issues.append(
            "Process-adoption proposal contains an unauthorized path: "
            + unauthorized[0]
        )
    issues.extend(
        _validate_process_adoption_workflows(
            project_root,
            base_commit=base_commit,
            head_commit=proposal.head_sha,
            action_pins=action_pins,
        )
    )
    return issues


def validate_controlled_automation_proposal(
    project_root: Path,
    *,
    repository: str,
    title: str,
    body: str,
    branch: str,
    target_branch: str,
    base_commit: str,
    state: str,
    commit: str,
    verifier_repository: str,
    verifier_commit: str,
    proposal: AutomationProposal,
    source: dict[str, object],
    producer_inputs: dict[str, Path] | None = None,
) -> list[str]:
    issues = validate_pull_request(
        title=title,
        body=body,
        branch=branch,
        state=state,
    )
    if len(body.encode("utf-8")) > MAX_PULL_REQUEST_BODY_BYTES:
        issues.append(
            f"Automation proposal body exceeds {MAX_PULL_REQUEST_BODY_BYTES} bytes"
        )
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        issues.append("Automation proposal commit must be a full lowercase Git SHA")
    if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
        issues.append("Automation proposal base must be a full lowercase Git SHA")
    if source.get("dirty") is not False:
        issues.append("Automation proposal source must have a clean working tree")
    if source.get("checkpoint") != commit:
        issues.append("Automation proposal commit does not match the current source")
    if source.get("fingerprint") is None:
        issues.append("Automation proposal source fingerprint is unavailable")
    expected_body_sha256 = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    exact_fields = {
        "repository": (proposal.repository, repository),
        "title": (proposal.title, title),
        "body SHA-256": (proposal.body_sha256, expected_body_sha256),
        "branch": (proposal.branch, branch),
        "target branch": (proposal.target_branch, target_branch),
        "base SHA": (proposal.base_sha, base_commit),
        "head SHA": (proposal.head_sha, commit),
        "verifier repository": (
            proposal.verifier_repository,
            verifier_repository,
        ),
        "verifier commit": (proposal.verifier_commit, verifier_commit),
    }
    for label, (actual, expected) in exact_fields.items():
        if actual != expected:
            issues.append(f"Automation proposal {label} does not match policy evidence")
    if proposal.base_sha == proposal.head_sha:
        issues.append("Automation proposal base and head must differ")
    if branch == target_branch:
        issues.append("Automation proposal branch must differ from its target")

    base_policy = _proposal_base_policy(
        project_root,
        base_sha=base_commit,
        policy_path=proposal.opt_in_path,
    )
    if canonical_json_digest(base_policy) != proposal.opt_in_sha256:
        issues.append("Automation proposal base opt-in digest does not match evidence")
    if base_policy != proposal.opt_in_document:
        issues.append("Automation proposal base opt-in document does not match evidence")

    exact_base = proposal.proposal_kind == "process-adoption"
    if exact_base and not _proposal_base_is_ancestor(
        project_root,
        base_sha=base_commit,
        head_sha=proposal.head_sha,
    ):
        issues.append(
            "Process-adoption protected base must be an ancestor of the proposal head"
        )
        return issues

    changed_paths = _proposal_changed_paths(
        project_root,
        base_sha=base_commit,
        head_sha=proposal.head_sha,
        exact_base=exact_base,
    )
    if changed_paths != proposal.changed_paths:
        issues.append("Automation proposal changed paths do not match policy evidence")
    if proposal.proposal_kind == "process-adoption":
        issues.extend(
            _validate_process_adoption_candidate(
                project_root,
                base_commit=base_commit,
                proposal=proposal,
                changed_paths=changed_paths,
                producer_inputs=producer_inputs,
            )
        )
    else:
        protected_paths = [
            path
            for path in changed_paths
            if path in PROTECTED_AUTOMATION_PROPOSAL_PATHS
            or path.startswith(PROTECTED_AUTOMATION_PROPOSAL_PREFIXES)
        ]
        if protected_paths:
            issues.append(
                "Controlled automation proposals cannot change process, workflow, "
                "release, security-policy, or trust-root paths: "
                + protected_paths[0]
                + (
                    f" (+{len(protected_paths) - 1} more)"
                    if len(protected_paths) > 1
                    else ""
                )
            )
    return issues


def validate_controlled_automation_proposal_completion(
    project_root: Path,
    *,
    repository: str,
    project: str,
    title: str,
    body: str,
    branch: str,
    target_branch: str,
    base_commit: str,
    commit: str,
    verifier_repository: str,
    verifier_commit: str,
    proposal: AutomationProposal,
    evidence: dict[str, object],
    source: dict[str, object],
    producer_inputs: dict[str, Path] | None = None,
) -> list[str]:
    if proposal.proposal_kind == "process-adoption":
        return [
            *validate_controlled_automation_proposal(
                project_root,
                repository=repository,
                title=title,
                body=body,
                branch=branch,
                target_branch=target_branch,
                base_commit=base_commit,
                state="ready",
                commit=commit,
                verifier_repository=verifier_repository,
                verifier_commit=verifier_commit,
                proposal=proposal,
                source=source,
                producer_inputs=producer_inputs,
            ),
            "Process-adoption proposals do not use lifecycle completion or standing "
            "automation merge escalation; consumer-owner manual merge is required",
        ]
    issues = validate_controlled_automation_proposal(
        project_root,
        repository=repository,
        title=title,
        body=body,
        branch=branch,
        target_branch=target_branch,
        base_commit=base_commit,
        state="ready",
        commit=commit,
        verifier_repository=verifier_repository,
        verifier_commit=verifier_commit,
        proposal=proposal,
        source=source,
        producer_inputs=producer_inputs,
    )
    issues.extend(
        validate_evidence_publication(
            title=title,
            body=body,
            branch=branch,
            commit=commit,
            project=project,
            evidence=evidence,
            source=source,
        )
    )
    if evidence.get("comparisonBase") != base_commit:
        issues.append(
            "Completion evidence comparison base does not match automation proposal"
        )
    if not proposal.human_merge_required:
        try:
            standing_policy = _proposal_base_policy(
                project_root,
                base_sha=base_commit,
                policy_path=".process/automation.json",
            )
            validate_automation_policy(
                standing_policy, "base standing automation policy"
            )
        except ContractError as error:
            issues.append(
                "Completed automation proposal requires a valid protected-base "
                f"standing automation policy: {error}"
            )
    return issues


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
