"""The complete engineering change state machine.

The lifecycle deliberately has six operations: start, plan, implement, verify,
review, and finish. Skills explain how to do the work; this module alone advances
state.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .commands import run_profile
from .contracts import (
    ProcessError,
    digest_json,
    load_and_validate,
    read_json,
    validate_document,
    write_json_atomic,
)
from .distribution import schemas_root
from .project import (
    accepted_issue_url_prefix,
    require_consumer_evidence,
    required_profiles,
)
from .repository import repository_snapshot, same_checkpoint


NEXT_COMMAND = {
    "specified": "change plan",
    "planned": "change implement",
    "implementing": "change verify",
    "verified": "change review start",
    "review-pending": "change review submit",
    "approved": "change finish",
    "changes-requested": "change implement",
    "completed": None,
    "blocked": None,
}
MAX_REVIEW_CORRECTION_CYCLES = 2
REVIEW_SCHEMA_VERSION = 6


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _actor(actor_id: str, context_id: str, kind: str) -> dict[str, str]:
    if not actor_id or not context_id:
        raise ProcessError("actor and context identities must be non-empty")
    if len(actor_id) > 200 or len(context_id) > 200:
        raise ProcessError("actor and context identities must not exceed 200 characters")
    if kind not in {"agent", "human"}:
        raise ProcessError(f"invalid actor kind: {kind}")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def _run_path(project_root: Path, change_id: str) -> Path:
    return project_root / ".process" / "runs" / change_id / "run.json"


def _receipt_path(project_root: Path, change_id: str) -> Path:
    return project_root / ".process" / "receipts" / f"{change_id}.json"


def _load_state(
    project_root: Path,
    process_root: Path,
    change_id: str,
) -> dict[str, Any]:
    path = _run_path(project_root, change_id)
    state = load_and_validate(path, "run", schema_root=schemas_root(process_root))
    if state["changeId"] != change_id:
        raise ProcessError(f"{path}: change identity mismatch")
    return state


def _save_state(
    project_root: Path,
    process_root: Path,
    state: dict[str, Any],
) -> None:
    validate_document(
        state,
        "run",
        schema_root=schemas_root(process_root),
        source="lifecycle state",
    )
    write_json_atomic(_run_path(project_root, state["changeId"]), state)


def _event(
    state: dict[str, Any],
    name: str,
    actor: dict[str, str],
    **details: Any,
) -> None:
    state["history"].append(
        {"event": name, "at": _now(), "actor": actor, "details": details}
    )


def _require_phase(state: dict[str, Any], *phases: str) -> None:
    if state["phase"] not in phases:
        expected = ", ".join(phases)
        raise ProcessError(
            f"change {state['changeId']} is {state['phase']}; expected {expected}"
        )


def start_change(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    contract_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    contract = load_and_validate(
        contract_path, "change", schema_root=schemas_root(process_root)
    )
    change_id = contract["id"]
    path = _run_path(project_root, change_id)
    if path.exists():
        raise ProcessError(f"change {change_id} already exists; resume it with status")

    declared = set(contract["requiredProfiles"])
    available = set(project["profiles"])
    missing = sorted(declared - available)
    if missing:
        raise ProcessError(
            f"change requires unknown verification profiles: {', '.join(missing)}"
        )
    weakened = sorted(set(required_profiles(project)) - declared)
    if weakened:
        raise ProcessError(
            "change cannot omit project-required profiles: " + ", ".join(weakened)
        )
    if require_consumer_evidence(project) and not contract.get("consumerEvidence"):
        raise ProcessError(
            "this project requires a real consumer incident or request before changing the process"
        )
    issue_prefix = accepted_issue_url_prefix(project)
    if issue_prefix is not None:
        source = contract["source"]
        issue_number = source[len(issue_prefix):] if source.startswith(issue_prefix) else ""
        if (
            not issue_number
            or not issue_number.isascii()
            or not issue_number.isdigit()
            or issue_number.startswith("0")
        ):
            raise ProcessError(
                "change source must be a numbered issue under the configured accepted issue URL prefix"
            )
    if project["project"] not in contract["affectedProjects"]:
        raise ProcessError("change affectedProjects must include the current project")

    actor = _actor(actor_id, context_id, kind)
    state: dict[str, Any] = {
        "schemaVersion": 1,
        "changeId": change_id,
        "phase": "specified",
        "cycle": 0,
        "contract": {"digest": digest_json(contract), "document": contract},
        "plan": None,
        "implementations": [],
        "currentImplementation": None,
        "verification": {},
        "reviewAssignment": None,
        "review": None,
        "reviewHistory": [],
        "receipt": None,
        "history": [],
    }
    _event(state, "started", actor)
    _save_state(project_root, process_root, state)
    return state


def register_plan(
    project_root: Path,
    process_root: Path,
    change_id: str,
    plan_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "specified")
    plan = load_and_validate(plan_path, "plan", schema_root=schemas_root(process_root))
    if plan["changeId"] != change_id:
        raise ProcessError("plan changeId does not match lifecycle state")
    if plan["contractDigest"] != state["contract"]["digest"]:
        raise ProcessError("plan contractDigest does not match the accepted contract")
    actor = _actor(actor_id, context_id, kind)
    state["plan"] = {"digest": digest_json(plan), "document": plan}
    state["phase"] = "planned"
    _event(state, "planned", actor)
    _save_state(project_root, process_root, state)
    return state


def begin_implementation(
    project_root: Path,
    process_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    actor = _actor(actor_id, context_id, kind)
    if state["phase"] == "implementing":
        participants = [
            item
            for item in state["implementations"]
            if item["cycle"] == state["cycle"]
        ]
        if any(item["actor"] == actor for item in participants):
            return state
        implementation = {
            "cycle": state["cycle"],
            "actor": actor,
            "startedAt": _now(),
        }
        state["implementations"].append(implementation)
        _event(
            state,
            "implementation-participant-registered",
            actor,
            cycle=state["cycle"],
        )
        _save_state(project_root, process_root, state)
        return state
    if state["phase"] not in {"planned", "changes-requested"}:
        if state["phase"] not in {"verified", "review-pending", "approved"}:
            _require_phase(state, "planned", "changes-requested")
        checkpoint = (
            state["reviewAssignment"]["checkpoint"]
            if state["reviewAssignment"] is not None
            else next(iter(state["verification"].values()))["checkpoint"]
        )
        if same_checkpoint(repository_snapshot(project_root), checkpoint):
            raise ProcessError(
                f"change {change_id} has current {state['phase']} evidence; implementation cannot restart"
            )
    state["cycle"] += 1
    implementation = {"cycle": state["cycle"], "actor": actor, "startedAt": _now()}
    state["implementations"].append(implementation)
    state["currentImplementation"] = implementation
    state["verification"] = {}
    state["reviewAssignment"] = None
    state["review"] = None
    state["phase"] = "implementing"
    _event(state, "implementation-started", actor, cycle=state["cycle"])
    _save_state(project_root, process_root, state)
    return state


def verify_change(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "implementing")
    if profile not in state["contract"]["document"]["requiredProfiles"]:
        raise ProcessError(f"profile {profile} is not required by change {change_id}")

    before = repository_snapshot(project_root)
    state["verification"] = {
        name: report
        for name, report in state["verification"].items()
        if same_checkpoint(report["checkpoint"], before)
    }
    report = run_profile(project_root, project, profile)
    after = repository_snapshot(project_root)
    report["checkpoint"] = after
    report["recordedAt"] = _now()
    if not same_checkpoint(before, after):
        report["status"] = "failed"
        report["reason"] = "repository changed while verification was running"
    state["verification"][profile] = report

    required = state["contract"]["document"]["requiredProfiles"]
    all_passed = all(
        name in state["verification"]
        and state["verification"][name]["status"] == "passed"
        and same_checkpoint(state["verification"][name]["checkpoint"], after)
        for name in required
    )
    if all_passed:
        state["phase"] = "verified"
    actor = state["currentImplementation"]["actor"]
    _event(
        state,
        "profile-verified" if report["status"] == "passed" else "profile-failed",
        actor,
        profile=profile,
    )
    _save_state(project_root, process_root, state)
    return state, report


def start_review(
    project_root: Path,
    process_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "verified")
    reviewer = _actor(actor_id, context_id, kind)
    if state["reviewHistory"]:
        original_reviewer = state["reviewHistory"][0]["document"]["reviewer"]
        if reviewer != original_reviewer:
            raise ProcessError(
                "correction review must use the original independent reviewer identity"
            )
    implementers = [
        item["actor"]
        for item in state["implementations"]
        if item["cycle"] == state["cycle"]
    ]
    if any(item["actorId"] == reviewer["actorId"] for item in implementers):
        raise ProcessError("reviewer actor must be independent from implementation")
    if any(item["contextId"] == reviewer["contextId"] for item in implementers):
        raise ProcessError("reviewer context must be independent from implementation")

    checkpoint = repository_snapshot(project_root)
    required = state["contract"]["document"]["requiredProfiles"]
    if not all(
        name in state["verification"]
        and state["verification"][name]["status"] == "passed"
        and same_checkpoint(state["verification"][name]["checkpoint"], checkpoint)
        for name in required
    ):
        raise ProcessError("verification evidence is stale or incomplete")
    state["reviewAssignment"] = {
        "reviewer": reviewer,
        "checkpoint": checkpoint,
        "startedAt": _now(),
        "reportSchemaVersion": REVIEW_SCHEMA_VERSION,
    }
    state["phase"] = "review-pending"
    _event(state, "review-started", reviewer)
    _save_state(project_root, process_root, state)
    return state


def submit_review(
    project_root: Path,
    process_root: Path,
    change_id: str,
    review_path: Path,
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "review-pending")
    review = load_and_validate(
        review_path, "review", schema_root=schemas_root(process_root)
    )
    assignment = state["reviewAssignment"]
    expected_schema = assignment.get("reportSchemaVersion", 5)
    if review["schemaVersion"] != expected_schema:
        raise ProcessError(f"review schemaVersion must be {expected_schema} for this assignment")
    if review["changeId"] != change_id:
        raise ProcessError("review changeId does not match lifecycle state")
    if review["reviewer"] != assignment["reviewer"]:
        raise ProcessError("reviewer does not match the assigned independent identity")
    if not same_checkpoint(review["checkpoint"], assignment["checkpoint"]):
        raise ProcessError("review checkpoint does not match the assignment")
    current = repository_snapshot(project_root)
    if not same_checkpoint(current, assignment["checkpoint"]):
        raise ProcessError("repository changed after review assignment")

    blocking = [finding for finding in review["findings"] if finding["severity"] == "blocking"]
    if review["verdict"] == "approved" and blocking:
        raise ProcessError("an approved review cannot contain blocking findings")
    if review["verdict"] == "changes-requested" and not blocking:
        raise ProcessError("changes-requested requires at least one blocking finding")

    criteria = {item["id"] for item in state["contract"]["document"]["acceptanceCriteria"]}
    finding_ids = [finding["id"] for finding in review["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise ProcessError("review finding ids must be unique")
    if any(finding["criterionId"] not in criteria for finding in review["findings"]):
        raise ProcessError("every review finding must map to an accepted criterion")
    prior_blocking_ids = {
        finding["id"]
        for prior in state["reviewHistory"]
        for finding in prior["document"]["findings"]
        if finding["severity"] == "blocking"
    }
    prior_findings = {
        finding["id"]: finding
        for prior in state["reviewHistory"]
        for finding in prior["document"]["findings"]
    }
    for finding in review["findings"]:
        prior = prior_findings.get(finding["id"])
        if prior is not None and any(
            finding[field] != prior[field]
            for field in ("criterionId", "origin", "priority")
        ):
            raise ProcessError("carried review finding identity fields are immutable")
    if not state["reviewHistory"] and any(
        finding["origin"] != "contract" for finding in blocking
    ):
        raise ProcessError("first-pass blockers must originate in the frozen contract")
    if state["reviewHistory"]:
        for finding in blocking:
            if finding["id"] in prior_blocking_ids:
                continue
            if finding["origin"] not in {
                "remediation-regression",
                "critical-late",
            }:
                raise ProcessError(
                    "a new late blocker must be a remediation regression or critical-late"
                )
            if not finding.get("lateRationale"):
                raise ProcessError("a new late blocker requires a bounded rationale")
            if (
                finding["origin"] == "critical-late"
                and finding["priority"] not in {"P0", "P1"}
            ):
                raise ProcessError("critical-late blockers must be P0 or P1")

    state["review"] = {"digest": digest_json(review), "document": review}
    state["reviewHistory"].append(
        {
            "cycle": state["cycle"],
            "digest": digest_json(review),
            "document": review,
        }
    )
    if review["verdict"] == "approved":
        state["phase"] = "approved"
    else:
        requested_count = sum(
            prior["document"]["verdict"] == "changes-requested"
            for prior in state["reviewHistory"]
        )
        state["phase"] = (
            "blocked"
            if requested_count > MAX_REVIEW_CORRECTION_CYCLES
            else "changes-requested"
        )
    _event(
        state,
        "review-submitted",
        assignment["reviewer"],
        verdict=review["verdict"],
    )
    _save_state(project_root, process_root, state)
    return state


def finish_change(
    project_root: Path,
    process_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "approved")
    actor = _actor(actor_id, context_id, kind)
    checkpoint = repository_snapshot(project_root)
    if not same_checkpoint(checkpoint, state["reviewAssignment"]["checkpoint"]):
        raise ProcessError("repository changed after approval")

    evidence: dict[str, Any] = {}
    for name, report in sorted(state["verification"].items()):
        evidence[name] = {
            "status": report["status"],
            "checkpoint": report["checkpoint"],
            "checks": [
                {
                    "id": check["id"],
                    "status": check["status"],
                    "exitCode": check["exitCode"],
                    "stdoutSha256": check["stdout"]["sha256"],
                    "stderrSha256": check["stderr"]["sha256"],
                }
                for check in report["checks"]
            ],
        }
    receipt = {
        "schemaVersion": 1,
        "changeId": change_id,
        "cycle": state["cycle"],
        "completedAt": _now(),
        "checkpoint": checkpoint,
        "contractDigest": state["contract"]["digest"],
        "planDigest": state["plan"]["digest"],
        "verification": evidence,
        "review": {
            "reviewer": state["reviewAssignment"]["reviewer"],
            "digest": state["review"]["digest"],
            "verdict": state["review"]["document"]["verdict"],
        },
    }
    validate_document(
        receipt,
        "receipt",
        schema_root=schemas_root(process_root),
        source="completion receipt",
    )
    receipt_path = _receipt_path(project_root, change_id)
    write_json_atomic(receipt_path, receipt)
    state["phase"] = "completed"
    state["receipt"] = {
        "path": str(receipt_path.relative_to(project_root)),
        "digest": digest_json(receipt),
    }
    _event(state, "finished", actor)
    _save_state(project_root, process_root, state)
    return state, receipt


def lifecycle_status(
    project_root: Path,
    process_root: Path,
    change_id: str,
) -> dict[str, Any]:
    state = deepcopy(_load_state(project_root, process_root, change_id))
    state["nextCommand"] = NEXT_COMMAND[state["phase"]]
    return state


def read_state_file(path: Path) -> dict[str, Any]:
    """Small public helper for tooling that only needs the serialized state."""
    value = read_json(path)
    if not isinstance(value, dict):
        raise ProcessError(f"{path}: lifecycle state must be an object")
    return value
