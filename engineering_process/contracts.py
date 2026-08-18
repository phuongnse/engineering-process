from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PROFILE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SKILL_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ContractError(ValueError):
    """Raised when a process contract is invalid."""


@dataclass(frozen=True)
class Check:
    identifier: str
    run: tuple[str, ...]
    timeout_seconds: int
    working_directory: str


@dataclass(frozen=True)
class Project:
    identifier: str
    profiles: dict[str, tuple[Check, ...]]
    required_profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessLock:
    version: str
    digest: str
    skills: tuple[str, ...]


def read_json(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if len(data) > 1_000_000:
        raise ContractError(f"{path}: contract exceeds the 1 MB limit")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractError(f"{path}: UTF-8 BOM is not allowed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{path}: must be UTF-8: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(
            f"{path}:{error.lineno}:{error.colno}: invalid JSON: {error.msg}"
        ) from error


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: must be an object")
    return value


def _exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    path: str,
) -> None:
    optional = optional or set()
    actual = set(value)
    missing = sorted(required - actual)
    extra = sorted(actual - required - optional)
    if missing:
        raise ContractError(f"{path}: missing properties: {', '.join(missing)}")
    if extra:
        raise ContractError(f"{path}: unknown properties: {', '.join(extra)}")


def _string(value: Any, path: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError(f"{path}: must be a non-empty trimmed string")
    if "\x00" in value:
        raise ContractError(f"{path}: must not contain NUL")
    if len(value) > max_length:
        raise ContractError(f"{path}: exceeds {max_length} characters")
    return value


def _string_list(
    value: Any,
    path: str,
    *,
    minimum: int = 1,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"{path}: must contain at least {minimum} item(s)")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _string(item, f"{path}[{index}]")
        if pattern is not None and pattern.fullmatch(text) is None:
            raise ContractError(f"{path}[{index}]: has an invalid format")
        result.append(text)
    if len(set(result)) != len(result):
        raise ContractError(f"{path}: duplicate items are not allowed")
    return result


def _schema_version(document: dict[str, Any], path: str) -> None:
    if document.get("schemaVersion") != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")


def validate_project(document: Any, path: str = "project") -> Project:
    value = _object(document, path)
    _exact_keys(
        value,
        required={"schemaVersion", "project", "lifecycle", "profiles"},
        optional={"$schema"},
        path=path,
    )
    _schema_version(value, path)
    identifier = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.project: must use lowercase project-id format")

    lifecycle = _object(value["lifecycle"], f"{path}.lifecycle")
    _exact_keys(
        lifecycle,
        required={"requiredProfiles"},
        path=f"{path}.lifecycle",
    )
    required_profiles = _string_list(
        lifecycle["requiredProfiles"],
        f"{path}.lifecycle.requiredProfiles",
        pattern=PROFILE_PATTERN,
    )
    if required_profiles != sorted(required_profiles):
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: must be sorted"
        )

    raw_profiles = _object(value["profiles"], f"{path}.profiles")
    if not raw_profiles:
        raise ContractError(f"{path}.profiles: must define at least one profile")
    profiles: dict[str, tuple[Check, ...]] = {}
    for profile_name, raw_checks in raw_profiles.items():
        if PROFILE_PATTERN.fullmatch(profile_name) is None:
            raise ContractError(
                f"{path}.profiles.{profile_name}: invalid profile name"
            )
        if not isinstance(raw_checks, list) or not raw_checks:
            raise ContractError(
                f"{path}.profiles.{profile_name}: must contain at least one check"
            )
        checks: list[Check] = []
        identifiers: set[str] = set()
        for index, raw_check in enumerate(raw_checks):
            check_path = f"{path}.profiles.{profile_name}[{index}]"
            check = _object(raw_check, check_path)
            _exact_keys(
                check,
                required={"id", "run", "timeoutSeconds"},
                optional={"workingDirectory"},
                path=check_path,
            )
            check_id = _string(check["id"], f"{check_path}.id", max_length=64)
            if PROFILE_PATTERN.fullmatch(check_id) is None:
                raise ContractError(f"{check_path}.id: invalid check name")
            if check_id in identifiers:
                raise ContractError(
                    f"{path}.profiles.{profile_name}: duplicate check id {check_id}"
                )
            identifiers.add(check_id)
            argv = _string_list(check["run"], f"{check_path}.run")
            timeout = check["timeoutSeconds"]
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int)
                or timeout < 1
                or timeout > 86_400
            ):
                raise ContractError(
                    f"{check_path}.timeoutSeconds: must be an integer from 1 to 86400"
                )
            working_directory = check.get("workingDirectory", ".")
            working_directory = _string(
                working_directory, f"{check_path}.workingDirectory", max_length=512
            )
            work_path = Path(working_directory)
            if work_path.is_absolute() or ".." in work_path.parts:
                raise ContractError(
                    f"{check_path}.workingDirectory: must stay within the project"
                )
            checks.append(
                Check(
                    identifier=check_id,
                    run=tuple(argv),
                    timeout_seconds=timeout,
                    working_directory=working_directory,
                )
            )
        profiles[profile_name] = tuple(checks)
    missing_required = sorted(set(required_profiles) - set(profiles))
    if missing_required:
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: undefined profiles: "
            f"{', '.join(missing_required)}"
        )
    return Project(
        identifier=identifier,
        profiles=profiles,
        required_profiles=tuple(required_profiles),
    )


def validate_process_lock(document: Any, path: str = "process.lock") -> ProcessLock:
    value = _object(document, path)
    _exact_keys(
        value,
        required={"schemaVersion", "process", "skills"},
        optional={"$schema"},
        path=path,
    )
    _schema_version(value, path)
    process = _object(value["process"], f"{path}.process")
    _exact_keys(
        process,
        required={"version", "digest"},
        path=f"{path}.process",
    )
    version = _string(process["version"], f"{path}.process.version", max_length=64)
    if SEMVER_PATTERN.fullmatch(version) is None:
        raise ContractError(f"{path}.process.version: must be SemVer")
    digest = _string(process["digest"], f"{path}.process.digest", max_length=71)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(
            f"{path}.process.digest: must be a lowercase sha256 digest"
        )
    skills = _string_list(value["skills"], f"{path}.skills", pattern=SKILL_PATTERN)
    if skills != sorted(skills):
        raise ContractError(f"{path}.skills: must be sorted")
    return ProcessLock(version=version, digest=digest, skills=tuple(skills))


def validate_change(document: Any, path: str = "change") -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "id",
            "summary",
            "source",
            "comparisonBase",
            "specification",
            "risk",
            "affectedProjects",
            "acceptanceCriteria",
            "requiredProfiles",
            "signOff",
        },
        optional={"$schema"},
        path=path,
    )
    if value.get("schemaVersion") != 2:
        raise ContractError(f"{path}.schemaVersion: must be 2")
    identifier = _string(value["id"], f"{path}.id", max_length=64)
    if PROFILE_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.id: invalid change id")
    _string(value["summary"], f"{path}.summary", max_length=500)
    _string(value["source"], f"{path}.source", max_length=1000)
    _string(value["comparisonBase"], f"{path}.comparisonBase", max_length=256)
    specification = _object(value["specification"], f"{path}.specification")
    _exact_keys(
        specification,
        required={"kind", "reference", "rationale"},
        path=f"{path}.specification",
    )
    if specification["kind"] not in {"project", "change-contract"}:
        raise ContractError(
            f"{path}.specification.kind: must be project or change-contract"
        )
    _string(
        specification["reference"],
        f"{path}.specification.reference",
        max_length=1000,
    )
    _string(
        specification["rationale"],
        f"{path}.specification.rationale",
        max_length=2000,
    )
    if value["risk"] not in {"low", "medium", "high"}:
        raise ContractError(f"{path}.risk: must be low, medium, or high")
    _string_list(
        value["affectedProjects"],
        f"{path}.affectedProjects",
        pattern=NAME_PATTERN,
    )
    _string_list(
        value["requiredProfiles"],
        f"{path}.requiredProfiles",
        pattern=PROFILE_PATTERN,
    )

    criteria = value["acceptanceCriteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ContractError(f"{path}.acceptanceCriteria: must not be empty")
    criterion_ids: set[str] = set()
    for index, raw_criterion in enumerate(criteria):
        criterion_path = f"{path}.acceptanceCriteria[{index}]"
        criterion = _object(raw_criterion, criterion_path)
        _exact_keys(
            criterion,
            required={"id", "outcome"},
            path=criterion_path,
        )
        identifier = _string(
            criterion["id"], f"{criterion_path}.id", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{criterion_path}.id: invalid criterion id")
        if identifier in criterion_ids:
            raise ContractError(
                f"{path}.acceptanceCriteria: duplicate id {identifier}"
            )
        criterion_ids.add(identifier)
        _string(criterion["outcome"], f"{criterion_path}.outcome", max_length=1000)

    sign_off = _object(value["signOff"], f"{path}.signOff")
    _exact_keys(
        sign_off,
        required={"required", "status", "evidence"},
        path=f"{path}.signOff",
    )
    required = sign_off["required"]
    if not isinstance(required, bool):
        raise ContractError(f"{path}.signOff.required: must be boolean")
    status = sign_off["status"]
    allowed_statuses = {"pending", "approved"} if required else {"not-required"}
    if status not in allowed_statuses:
        raise ContractError(
            f"{path}.signOff.status: invalid for required={str(required).lower()}"
        )
    evidence = sign_off["evidence"]
    if status == "approved":
        _string(evidence, f"{path}.signOff.evidence", max_length=1000)
    elif evidence is not None:
        raise ContractError(
            f"{path}.signOff.evidence: must be null unless status is approved"
        )


def validate_plan(document: Any, path: str = "plan") -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "changeId",
            "contractDigest",
            "approach",
            "workItems",
            "acceptancePlan",
            "risks",
            "openDecisions",
        },
        optional={"$schema"},
        path=path,
    )
    _schema_version(value, path)
    change_id = _string(value["changeId"], f"{path}.changeId", max_length=64)
    if PROFILE_PATTERN.fullmatch(change_id) is None:
        raise ContractError(f"{path}.changeId: invalid change id")
    digest = _string(
        value["contractDigest"], f"{path}.contractDigest", max_length=71
    )
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(f"{path}.contractDigest: must be a lowercase sha256 digest")
    _string(value["approach"], f"{path}.approach", max_length=4000)

    work_items = value["workItems"]
    if not isinstance(work_items, list) or not work_items:
        raise ContractError(f"{path}.workItems: must not be empty")
    work_item_ids: set[str] = set()
    for index, raw_item in enumerate(work_items):
        item_path = f"{path}.workItems[{index}]"
        item = _object(raw_item, item_path)
        _exact_keys(
            item,
            required={"id", "outcome", "affectedPaths", "verificationProfiles"},
            path=item_path,
        )
        item_id = _string(item["id"], f"{item_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(item_id) is None:
            raise ContractError(f"{item_path}.id: invalid work-item id")
        if item_id in work_item_ids:
            raise ContractError(f"{path}.workItems: duplicate id {item_id}")
        work_item_ids.add(item_id)
        _string(item["outcome"], f"{item_path}.outcome", max_length=1000)
        _string_list(item["affectedPaths"], f"{item_path}.affectedPaths")
        _string_list(
            item["verificationProfiles"],
            f"{item_path}.verificationProfiles",
            pattern=PROFILE_PATTERN,
        )

    acceptance_plan = value["acceptancePlan"]
    if not isinstance(acceptance_plan, list) or not acceptance_plan:
        raise ContractError(f"{path}.acceptancePlan: must not be empty")
    criterion_ids: set[str] = set()
    for index, raw_mapping in enumerate(acceptance_plan):
        mapping_path = f"{path}.acceptancePlan[{index}]"
        mapping = _object(raw_mapping, mapping_path)
        _exact_keys(
            mapping,
            required={"criterionId", "workItems", "verificationProfiles"},
            path=mapping_path,
        )
        criterion_id = _string(
            mapping["criterionId"], f"{mapping_path}.criterionId", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(criterion_id) is None:
            raise ContractError(f"{mapping_path}.criterionId: invalid criterion id")
        if criterion_id in criterion_ids:
            raise ContractError(
                f"{path}.acceptancePlan: duplicate criterion {criterion_id}"
            )
        criterion_ids.add(criterion_id)
        mapped_items = _string_list(
            mapping["workItems"], f"{mapping_path}.workItems", pattern=PROFILE_PATTERN
        )
        unknown_items = sorted(set(mapped_items) - work_item_ids)
        if unknown_items:
            raise ContractError(
                f"{mapping_path}.workItems: unknown ids: {', '.join(unknown_items)}"
            )
        _string_list(
            mapping["verificationProfiles"],
            f"{mapping_path}.verificationProfiles",
            pattern=PROFILE_PATTERN,
        )

    risks = value["risks"]
    if not isinstance(risks, list):
        raise ContractError(f"{path}.risks: must be an array")
    for index, raw_risk in enumerate(risks):
        risk_path = f"{path}.risks[{index}]"
        risk = _object(raw_risk, risk_path)
        _exact_keys(risk, required={"risk", "mitigation"}, path=risk_path)
        _string(risk["risk"], f"{risk_path}.risk", max_length=1000)
        _string(risk["mitigation"], f"{risk_path}.mitigation", max_length=1000)

    decisions = value["openDecisions"]
    if not isinstance(decisions, list):
        raise ContractError(f"{path}.openDecisions: must be an array")
    if decisions:
        _string_list(decisions, f"{path}.openDecisions")


def _validate_actor(value: Any, path: str) -> dict[str, str]:
    actor = _object(value, path)
    _exact_keys(actor, required={"actorId", "contextId", "kind"}, path=path)
    actor_id = _string(actor["actorId"], f"{path}.actorId", max_length=256)
    context_id = _string(actor["contextId"], f"{path}.contextId", max_length=256)
    kind = actor["kind"]
    if kind not in {"agent", "human"}:
        raise ContractError(f"{path}.kind: must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def validate_review(document: Any, path: str = "review") -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "changeId",
            "cycle",
            "checkpoint",
            "workspaceFingerprint",
            "comparisonBase",
            "reviewer",
            "independence",
            "verdict",
            "findings",
        },
        optional={"$schema"},
        path=path,
    )
    if value.get("schemaVersion") != 2:
        raise ContractError(f"{path}.schemaVersion: must be 2")
    change_id = _string(value["changeId"], f"{path}.changeId", max_length=64)
    if PROFILE_PATTERN.fullmatch(change_id) is None:
        raise ContractError(f"{path}.changeId: invalid change id")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")
    _string(value["checkpoint"], f"{path}.checkpoint", max_length=256)
    fingerprint = _string(
        value["workspaceFingerprint"],
        f"{path}.workspaceFingerprint",
        max_length=71,
    )
    if DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise ContractError(
            f"{path}.workspaceFingerprint: must be a lowercase sha256 digest"
        )
    _string(value["comparisonBase"], f"{path}.comparisonBase", max_length=256)
    _validate_actor(value["reviewer"], f"{path}.reviewer")
    independence = _object(value["independence"], f"{path}.independence")
    _exact_keys(
        independence,
        required={"method", "attestedBy", "evidence"},
        path=f"{path}.independence",
    )
    if independence["method"] not in {"isolated-context", "separate-person"}:
        raise ContractError(f"{path}.independence.method: invalid method")
    _string(
        independence["attestedBy"],
        f"{path}.independence.attestedBy",
        max_length=256,
    )
    _string(
        independence["evidence"],
        f"{path}.independence.evidence",
        max_length=2000,
    )
    verdict = value["verdict"]
    if verdict not in {"approved", "changes-requested"}:
        raise ContractError(
            f"{path}.verdict: must be approved or changes-requested"
        )
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ContractError(f"{path}.findings: must be an array")
    finding_ids: set[str] = set()
    open_findings = 0
    for index, raw_finding in enumerate(findings):
        finding_path = f"{path}.findings[{index}]"
        finding = _object(raw_finding, finding_path)
        _exact_keys(
            finding,
            required={
                "id",
                "severity",
                "path",
                "line",
                "summary",
                "evidence",
                "status",
                "resolutionEvidence",
            },
            path=finding_path,
        )
        identifier = _string(finding["id"], f"{finding_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{finding_path}.id: invalid finding id")
        if identifier in finding_ids:
            raise ContractError(f"{path}.findings: duplicate id {identifier}")
        finding_ids.add(identifier)
        if finding["severity"] not in {"critical", "high", "medium", "low"}:
            raise ContractError(f"{finding_path}.severity: invalid severity")
        _string(finding["path"], f"{finding_path}.path", max_length=1000)
        line = finding["line"]
        if line is not None and (
            isinstance(line, bool) or not isinstance(line, int) or line < 1
        ):
            raise ContractError(
                f"{finding_path}.line: must be null or a positive integer"
            )
        _string(finding["summary"], f"{finding_path}.summary", max_length=1000)
        _string(finding["evidence"], f"{finding_path}.evidence", max_length=4000)
        if finding["status"] not in {
            "open",
            "resolved",
            "deferred",
            "false-positive",
        }:
            raise ContractError(f"{finding_path}.status: invalid status")
        if finding["status"] == "open":
            open_findings += 1
            if finding["resolutionEvidence"] is not None:
                raise ContractError(
                    f"{finding_path}.resolutionEvidence: must be null while open"
                )
        else:
            _string(
                finding["resolutionEvidence"],
                f"{finding_path}.resolutionEvidence",
                max_length=4000,
            )
    if verdict == "approved" and open_findings:
        raise ContractError(f"{path}: approved review cannot contain open findings")
    if verdict == "changes-requested" and not open_findings:
        raise ContractError(
            f"{path}: changes-requested review must contain an open finding"
        )
