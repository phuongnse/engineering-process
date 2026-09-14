"""PR document generation and structural checks from one selected standard."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .artifact_standards import ArtifactStandard, MAX_DOCUMENT_BYTES
from .contracts import ProcessError, validate_document
from .distribution import distribution_root, schemas_root


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
NON_MARKDOWN_LINE_SEPARATOR = re.compile("[\v\f\x85\u2028\u2029]")
ISSUE_TARGET = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*"
ISSUE_REFERENCE = re.compile(
    rf"^(?:Refs {ISSUE_TARGET}|Closes {ISSUE_TARGET}(?:, closes {ISSUE_TARGET})*)\.$"
)


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


def body_issues(body: str, state: str, standard: ArtifactStandard) -> list[str]:
    if standard.document["adapter"] != "pr-description":
        raise ProcessError("selected standard requires a different document adapter")
    sections = tuple(section["heading"] for section in standard.rules["sections"])
    fields = {section["heading"]: tuple(field["label"] for field in section["fields"]) for section in standard.rules["sections"]}
    checks = {check["label"]: section["heading"] for section in standard.rules["sections"] for check in section["checks"]}
    visible_body, issues = _without_html_comments(body)
    lines, fence_issues = _structural_lines(visible_body)
    issues.extend(fence_issues)
    if state not in {"draft", "ready"}:
        issues.append("pull request state must be draft or ready")
    field_positions: list[int] = []
    section_positions: list[int] = []
    for section in sections:
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
            and line not in sections
        }
    )
    issues.extend(
        f"pull request body has unexpected section {section}"
        for section in unexpected_sections
    )
    ordered_sections = (
        len(section_positions) == len(sections)
        and section_positions == sorted(section_positions)
    )
    if len(section_positions) == len(sections) and not ordered_sections:
        issues.append("pull request body sections are out of order")

    section_ranges: dict[str, tuple[int, int]] = {}
    if ordered_sections:
        boundaries = section_positions[1:] + [len(lines)]
        section_ranges = {
            section: (start + 1, end)
            for section, start, end in zip(sections, section_positions, boundaries)
        }
    for section, expected_fields in fields.items():
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
                field_positions.append(position)
                if not match.group(1).strip():
                    issues.append(f"pull request body has no value for {field}")
                if state == "ready" and standard.unresolved(match.group(1)):
                    issues.append(f"ready pull request has unresolved value for {field}")
                if ordered_sections:
                    start, end = section_ranges[section]
                    if not start <= position < end:
                        issues.append(f"pull request body misplaces {field} from {section}")
        if len(ordered_fields) == len(expected_fields) and ordered_fields != sorted(
            ordered_fields
        ):
            issues.append(f"pull request body fields are out of order in {section}")

    checklist_positions: list[int] = []
    for check, owner_section in checks.items():
        completion_range = section_ranges.get(owner_section)
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
    if len(checklist_positions) == len(checks):
        if checklist_positions != sorted(checklist_positions):
            issues.append("pull request body checklist items are out of order")
    reference_positions = [
        index
        for index, line in enumerate(lines)
        if line is not None and standard.rules["issueReferences"] and ISSUE_REFERENCE.fullmatch(line)
    ]
    if len(reference_positions) > 1:
        issues.append("pull request body repeats its issue reference")
    elif (
        reference_positions
        and (field_positions or checklist_positions)
        and reference_positions[0] <= max(field_positions + checklist_positions)
    ):
        issues.append("pull request body issue reference must follow the checklist")
    if state == "draft" and any(
        lines[position].startswith("Closes ") for position in reference_positions
    ):
        issues.append("draft pull request cannot close issues")
    field_patterns = tuple(
        re.compile(rf"^- {re.escape(field)}:\s*.*$")
        for section_fields in fields.values()
        for field in section_fields
    )
    checklist_patterns = tuple(
        re.compile(rf"^- \[[ xX]\] {re.escape(check)}$") for check in checks
    )
    for line_number, line in enumerate(lines, start=1):
        if (
            line is None
            or not line
            or line in sections
            or (standard.rules["issueReferences"] and ISSUE_REFERENCE.fullmatch(line))
            or any(pattern.fullmatch(line) for pattern in field_patterns)
            or any(pattern.fullmatch(line) for pattern in checklist_patterns)
        ):
            continue
        issues.append(
            "pull request body has unsupported visible content at line "
            f"{line_number}"
        )
    return issues


def render_description(
    standard: ArtifactStandard,
    data: dict[str, Any] | None = None,
    *,
    state: str = "draft",
    template: bool = False,
    process_root: Path | None = None,
) -> str:
    if standard.document["adapter"] != "pr-description":
        raise ProcessError("selected standard cannot generate a PR template")
    data = data if data is not None else {"schemaVersion": 1, "fields": {}, "checks": {}}
    validate_document(data, "pr-description-data", schema_root=schemas_root(distribution_root(process_root)))
    sections = standard.rules["sections"]
    for kind in ("fields", "checks"):
        expected = {item["id"] for section in sections for item in section[kind]}
        unknown = sorted(data[kind].keys() - expected)
        if unknown:
            raise ProcessError(f"PR data contains unknown {kind}: " + ", ".join(unknown))
    lines: list[str] = []
    for section in sections:
        lines.extend([section["heading"], ""])
        for field in section["fields"]:
            value = data["fields"].get(field["id"], standard.document["pendingValues"][0])
            hint = f" <!-- {field['description']} -->" if template else ""
            lines.append(f"- {field['label']}: {value}{hint}")
        for check in section["checks"]:
            marker = "x" if data["checks"].get(check["id"], False) else " "
            lines.append(f"- [{marker}] {check['label']}")
        lines.append("")
    if "issueReference" in data:
        lines.extend([data["issueReference"], ""])
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ProcessError("PR document exceeds its size limit")
    issues = body_issues(body, state, standard)
    if issues:
        raise ProcessError("; ".join(issues))
    return body


def render_template(standard: ArtifactStandard, *, process_root: Path | None = None) -> str:
    body = render_description(standard, template=True, process_root=process_root)
    guidance = standard.rules.get("guidance", "")
    if guidance:
        body = f"<!-- {guidance} -->\n" + body
    issues = body_issues(body, "draft", standard)
    if issues:
        raise ProcessError("; ".join(issues))
    return "<!-- engineering-process:pr-description:start -->\n" + body + "<!-- engineering-process:pr-description:end -->\n"


def render_renovate_preset(standard: ArtifactStandard, *, process_root: Path | None = None) -> dict[str, str]:
    if standard.document["adapter"] != "pr-description":
        raise ProcessError("selected standard cannot generate a Renovate PR preset")
    known = {
        "outcome": "Update {{#each upgrades}}{{depName}} from {{currentValue}}{{#if currentDigest}} ({{currentDigest}}){{/if}} to {{newValue}}{{#if newDigest}} ({{newDigest}}){{/if}}{{#unless @last}}; {{/unless}}{{/each}}.",
        "scope": "{{#each upgrades}}{{packageFile}}{{#unless @last}}; {{/unless}}{{/each}}.",
    }
    fields = {field["id"] for section in standard.rules["sections"] for field in section["fields"]}
    data = {"schemaVersion": 1, "fields": {key: value for key, value in known.items() if key in fields}, "checks": {}}
    preset = {
        "$schema": "https://docs.renovatebot.com/renovate-schema.json",
        "description": f"Draft PR body for {standard.document['id']}@{standard.document['version']}.",
        "prBodyTemplate": "{{{header}}}",
        "prHeader": render_description(standard, data, process_root=process_root),
    }
    validate_document(preset, "renovate-preset", schema_root=schemas_root(distribution_root(process_root)))
    return preset


def build_pr_description_data(
    project_root: Path,
    process_root: Path | None,
    change_id: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .contracts import digest_json, load_and_validate, read_json
    from .repository import repository_snapshot, same_checkpoint

    dist_root = distribution_root(process_root)
    run_path = project_root / ".process" / "runs" / change_id / "run.json"
    state = load_and_validate(run_path, "run", schema_root=schemas_root(dist_root))
    if state.get("changeId") != change_id:
        raise ProcessError(f"{run_path}: change identity mismatch")
    current_checkpoint = repository_snapshot(project_root)

    contract = state.get("contract", {}).get("document", {})
    source = contract.get("source", "pending")
    risk = contract.get("risk", "pending")

    required_profiles = contract.get("requiredProfiles", [])
    verification = state.get("verification", {})
    passed_profiles = [
        profile
        for profile in required_profiles
        if verification.get(profile, {}).get("status") == "passed"
        and same_checkpoint(
            verification.get(profile, {}).get("checkpoint", {}), current_checkpoint
        )
    ]
    passed_profiles.sort()
    profiles_val = (
        ", ".join(f"`{profile}`" for profile in passed_profiles)
        if passed_profiles
        else "pending"
    )

    review = state.get("review", {}).get("document")
    review_history = state.get("reviewHistory", [])
    review_cycles = len(review_history)

    snapshot_val = "pending"
    verdict_val = "pending"
    cycles_val = "pending"
    blocking_val = "pending"
    dispositions_val = "pending"

    review_matched = False
    blocking_count = 0
    non_blocking_count = 0
    all_dispositions_recorded = True

    if review is not None:
        reviewed_checkpoint = review.get("checkpoint", {})
        if same_checkpoint(current_checkpoint, reviewed_checkpoint):
            review_matched = True
            snapshot_val = f"`{reviewed_checkpoint.get('fingerprint', '')}`"
            verdict_val = review.get("verdict", "pending")
            cycles_val = str(review_cycles) if review_cycles > 0 else "1"
            findings = review.get("findings", [])
            blocking = [f for f in findings if f.get("severity") == "blocking"]
            blocking_count = len(blocking)
            blocking_val = f"{blocking_count} open"

            non_blocking = [f for f in findings if f.get("severity") == "non-blocking"]
            non_blocking_count = len(non_blocking)
            if not non_blocking:
                dispositions_val = "none"
            else:
                items = []
                for f in non_blocking:
                    disp = f.get("disposition")
                    if not disp or not disp.get("status"):
                        all_dispositions_recorded = False
                        items.append(f"{f['id']}: unrecorded")
                    elif disp["status"] == "resolved":
                        items.append(f"{f['id']}: resolved")
                    else:
                        owner = disp.get("owner")
                        url = disp.get("recordUrl")
                        if owner and url:
                            items.append(f"{f['id']}: {disp['status']} ({owner}, {url})")
                        else:
                            items.append(f"{f['id']}: {disp['status']}")
                dispositions_val = "; ".join(items)

    receipt_path = project_root / ".process" / "receipts" / f"{change_id}.json"
    receipt_val = "pending"
    receipt_matched = False
    if state.get("phase") == "completed" and receipt_path.exists():
        try:
            receipt = read_json(receipt_path)
            validate_document(
                receipt, "receipt", schema_root=schemas_root(dist_root)
            )
            if same_checkpoint(receipt.get("checkpoint", {}), current_checkpoint):
                receipt_val = f"`{digest_json(receipt)}`"
                receipt_matched = True
        except Exception:
            receipt_val = "pending"

    req_profiles_passed = bool(
        len(passed_profiles) == len(required_profiles)
        and required_profiles
        and review_matched
    )
    independent_review_ok = bool(
        review_matched and verdict_val == "approved" and blocking_count == 0
    )
    dispositions_ok = bool(
        review_matched and (non_blocking_count == 0 or all_dispositions_recorded)
    )

    fields: dict[str, Any] = {
        "outcome": "pending",
        "scope": "pending",
        "source": source,
        "risk": risk,
        "compatibility": "none",
        "stack": "none",
        "profiles": profiles_val,
        "snapshot": snapshot_val,
        "completion-receipt": receipt_val,
        "verdict": verdict_val,
        "cycles": cycles_val,
        "blocking-findings": blocking_val,
        "non-blocking-dispositions": dispositions_val,
    }

    checks: dict[str, Any] = {
        "accepted-scope": False,
        "required-profiles": req_profiles_passed,
        "independent-review": independent_review_ok,
        "finding-dispositions": dispositions_ok,
    }

    data: dict[str, Any] = {
        "schemaVersion": 1,
        "fields": fields,
        "checks": checks,
    }

    if overrides is not None:
        if "fields" in overrides:
            for k, v in overrides["fields"].items():
                if k in {
                    "completion-receipt",
                    "snapshot",
                    "profiles",
                    "verdict",
                    "cycles",
                    "blocking-findings",
                    "non-blocking-dispositions",
                }:
                    continue
                fields[k] = v
        if "checks" in overrides:
            for k, v in overrides["checks"].items():
                if k in {"required-profiles", "independent-review", "finding-dispositions"}:
                    if not review_matched or (k == "required-profiles" and not req_profiles_passed):
                        continue
                    if k == "independent-review" and not independent_review_ok:
                        continue
                    if k == "finding-dispositions" and not dispositions_ok:
                        continue
                checks[k] = bool(v)
        if "issueReference" in overrides:
            data["issueReference"] = overrides["issueReference"]

    validate_document(data, "pr-description-data", schema_root=schemas_root(dist_root))
    return data
