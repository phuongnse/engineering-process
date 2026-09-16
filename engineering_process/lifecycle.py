"""The complete engineering change state machine.

The lifecycle deliberately has six operations: start, plan, implement, verify,
review, and finish. Skills explain how to do the work; this module alone advances
state.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import os
from pathlib import Path
import time
from typing import Any

from .commands import run_check, run_profile, verification_lock
from .contracts import (
    ProcessError,
    digest_json,
    load_and_validate,
    read_json,
    validate_document,
    write_json_atomic,
)
from .distribution import distribution_digest, schemas_root
from .evidence import (
    execution_identity,
    verification_input_digest,
    verification_report_matches_inputs,
)
from .impact import (
    impact_unit_lookup,
    unavailable_impact_selection,
    resolve_impact_selection,
)
from .project import (
    accepted_issue_url_prefix,
    impact_assurance_profiles,
    load_project,
    publication_required,
    require_consumer_evidence,
    required_profiles,
)
from .production_engineering import (
    PLAN_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    validate_current_run_documents,
    validate_plan_assessments,
    validate_review_assessments,
)
from .source_publication import branch_issues, current_branch, validate_current_source
from .repository import (
    changed_paths,
    repository_snapshot,
    require_committed_candidate,
    resolve_commit,
    same_checkpoint,
)
from .review_contexts import (
    recorded_context_conflict,
    require_unreused_context,
    history_transaction,
)


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


def _repository_relative_path(project_root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    value = relative.as_posix()
    return value if value and value != "." else None


def _remember_control_path(
    state: dict[str, Any], project_root: Path, path: Path
) -> None:
    relative = _repository_relative_path(project_root, path)
    if relative is None:
        return
    paths = set(state.get("controlPaths", []))
    paths.add(relative)
    state["controlPaths"] = sorted(paths)


def _platform_scope_path(value: str) -> str:
    return value.replace("\\", "/") if os.name == "nt" else value


def _scope_path(value: str, *, source: str) -> str:
    normalized = _platform_scope_path(value)
    if normalized.startswith("/"):
        raise ProcessError(
            f"{source}: affectedPaths must contain literal repository-relative paths"
        )
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized or normalized == ".":
        return ""
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or (len(parts[0]) == 2 and parts[0][1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
        or any(character in normalized for character in "*?[")
    ):
        raise ProcessError(
            f"{source}: affectedPaths must contain literal repository-relative paths"
        )
    return normalized


def _scope_candidate_path(value: str) -> str:
    normalized = _platform_scope_path(value)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _has_plan_scope_policy(state: dict[str, Any]) -> bool:
    return any(
        event["event"] == "plan-scope-registered"
        and event["details"].get("policy") == "literal-repository-boundaries-v1"
        for event in state.get("history", [])
    )


def _require_planned_scope(project_root: Path, state: dict[str, Any]) -> None:
    """Reject candidate files that the current plan did not declare.

    The check proves only structural scope alignment. Causal correctness and
    minimality remain independent-review judgments.
    """
    if not _has_plan_scope_policy(state):
        return
    comparison_base = state.get("comparisonBaseCommit")
    plan = state.get("plan")
    if not comparison_base or not isinstance(plan, dict):
        return
    boundaries = [
        _scope_path(raw_path, source=f"plan work item {work_item['id']}")
        for work_item in plan["document"]["workItems"]
        for raw_path in work_item["affectedPaths"]
    ]
    if not boundaries:
        raise ProcessError("plan must declare at least one affected path boundary")
    control_paths = {
        _scope_candidate_path(path) for path in state.get("controlPaths", [])
    }
    uncovered = sorted(
        candidate
        for raw_candidate in changed_paths(project_root, comparison_base)
        for candidate in [_scope_candidate_path(raw_candidate)]
        if candidate not in control_paths
        and not any(
            boundary == ""
            or candidate == boundary
            or candidate.startswith(boundary + "/")
            for boundary in boundaries
        )
    )
    if uncovered:
        raise ProcessError(
            "candidate paths are outside the declared plan scope: "
            + ", ".join(uncovered)
        )


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
    validate_current_run_documents(state, process_root)
    if state["plan"] is not None and not _has_plan_scope_policy(state):
        raise ProcessError("lifecycle state is missing the current plan scope binding")
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


def process_improvement_signals(state: dict[str, Any]) -> list[str]:
    signals: set[str] = set()
    for item in state["history"]:
        if item["event"] == "profile-failed":
            signals.add("profile-failed")
        elif (
            item["event"] == "review-submitted"
            and item["details"].get("verdict") == "changes-requested"
        ):
            signals.add("review-changes-requested")
        elif item["event"] == "evidence-invalidated":
            signals.add("evidence-invalidated")
        elif item["event"] == "review-assignment-replaced":
            signals.add("review-assignment-replaced")
    return sorted(signals)



def _require_phase(state: dict[str, Any], *phases: str) -> None:
    if state["phase"] not in phases:
        expected = ", ".join(phases)
        raise ProcessError(
            f"change {state['changeId']} is {state['phase']}; expected {expected}"
        )


def _verification_input_digest(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    profile: str,
    *,
    runtime: dict[str, Any] | None = None,
    authority_digest: str | None = None,
) -> str | None:
    """Bind reusable evidence to every input controlled by this process."""
    return verification_input_digest(
        project_root,
        process_root,
        project,
        state,
        profile,
        runtime=runtime if runtime is not None else execution_identity(),
        authority_digest=authority_digest,
    )


def _current_baseline_gap(project: dict[str, Any], state: dict[str, Any]) -> list[str]:
    accepted = set(state["contract"]["document"]["requiredProfiles"])
    current = set(project["lifecycle"]["requiredProfiles"])
    return sorted(current - accepted)


def _require_current_baseline(project: dict[str, Any], state: dict[str, Any]) -> None:
    gap = _current_baseline_gap(project, state)
    if gap:
        raise ProcessError(
            "current consumer policy requires profiles absent from the accepted contract: "
            + ", ".join(gap)
        )


def _verification_report_matches_inputs(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    profile: str,
    report: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    require_input: bool,
    runtime: dict[str, Any] | None = None,
    authority_digest: str | None = None,
) -> bool:
    return verification_report_matches_inputs(
        project_root,
        process_root,
        project,
        state,
        profile,
        report,
        checkpoint,
        require_input=require_input,
        runtime=runtime if runtime is not None else execution_identity(),
        authority_digest=authority_digest,
    )


def _required_verification_matches_inputs(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    require_input: bool,
) -> bool:
    runtime = execution_identity()
    authority_digest = distribution_digest(process_root)
    return all(
        profile in state["verification"]
        and _verification_report_matches_inputs(
            project_root,
            process_root,
            project,
            state,
            profile,
            state["verification"][profile],
            checkpoint,
            require_input=require_input,
            runtime=runtime,
            authority_digest=authority_digest,
        )
        for profile in state["contract"]["document"]["requiredProfiles"]
    )


def _verification_selection(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    current = repository_snapshot(project_root)
    required = tuple(state["contract"]["document"]["requiredProfiles"])
    configured = project["profiles"]
    requirements: list[dict[str, Any]] = []
    execute: list[str] = []
    reuse: list[str] = []
    blocked: list[str] = []
    runtime = execution_identity()
    authority_digest = distribution_digest(process_root)
    assurance_profiles = tuple(impact_assurance_profiles(project))
    assurance_selection: dict[str, Any] | None = None
    assurance_requirements: dict[str, dict[str, Any]] = {}
    if assurance_profiles:
        assurance_selection = resolve_impact_selection(
            project_root,
            process_root,
            project,
            state,
            profiles=assurance_profiles,
        )
        assurance_requirements = {
            item["profile"]: item
            for item in assurance_selection["requirements"]
        }

    newly_required = _current_baseline_gap(project, state)
    blocked.extend(newly_required)

    for profile in required:
        mode = "full"
        previous = state["verification"].get(profile)
        if profile not in configured:
            status, action, reason = (
                "blocked",
                "blocked",
                "the accepted profile is not present in the current project policy",
            )
            blocked.append(profile)
        elif previous is not None and _verification_report_matches_inputs(
            project_root,
            process_root,
            project,
            state,
            profile,
            previous,
            current,
            require_input=True,
            runtime=runtime,
            authority_digest=authority_digest,
        ):
            status, action, reason = (
                "satisfied",
                "reuse",
                "valid whole-profile evidence already covers this exact candidate and input identity",
            )
            mode = previous.get("executionMode", "full")
            reuse.append(profile)
        elif previous is not None and previous["status"] == "passed" and same_checkpoint(
            previous["checkpoint"], current
        ) and not previous.get("inputDigest"):
            if profile in assurance_profiles and assurance_selection is not None:
                assurance_requirement = assurance_requirements[profile]
                if assurance_selection["status"] == "ready":
                    status, action, reason = (
                        "unknown",
                        "execute",
                        "recorded evidence has no reusable input identity; final impact assurance will execute",
                    )
                    mode = "impact-assurance"
                else:
                    status, action, reason = (
                        "blocked",
                        "blocked",
                        assurance_requirement.get(
                            "reason",
                            assurance_selection["resolution"]["reason"],
                        ),
                    )
                    blocked.append(profile)
            else:
                status, action, reason = (
                    "unknown",
                    "execute",
                    "recorded evidence has no reusable input identity",
                )
            if action == "execute":
                execute.append(profile)
        else:
            mode = "full"
            if profile in assurance_profiles and assurance_selection is not None:
                assurance_requirement = assurance_requirements[profile]
                if assurance_selection["status"] == "ready":
                    status, action, reason = (
                        "remaining",
                        "execute",
                        "final impact assurance covers every changed path with the consumer's declared units",
                    )
                    mode = "impact-assurance"
                else:
                    status, action, reason = (
                        "blocked",
                        "blocked",
                        assurance_requirement.get(
                            "reason",
                            assurance_selection["resolution"]["reason"],
                        ),
                    )
                    blocked.append(profile)
            else:
                status, action, reason = (
                    "remaining",
                    "execute",
                    "no valid reusable whole-profile evidence covers the current inputs",
                )
            if action == "execute":
                execute.append(profile)

        item: dict[str, Any] = {
            "id": profile,
            "profile": profile,
            "status": status,
            "action": action,
            "reason": reason,
        }
        item["mode"] = mode
        if previous is not None and previous["status"] == "passed":
            item["evidence"] = {
                "checkpoint": previous["checkpoint"],
                "recordedAt": previous["recordedAt"],
                **(
                    {"inputDigest": previous["inputDigest"]}
                    if previous.get("inputDigest")
                    else {}
                ),
            }
        requirements.append(item)

    for profile in newly_required:
        requirements.append(
            {
                "id": profile,
                "profile": profile,
                "status": "blocked",
                "action": "blocked",
                "reason": "the current consumer baseline added this profile after the accepted contract",
            }
        )

    inapplicable = sorted(
        set(configured) - set(required) - set(newly_required)
    )
    for profile in inapplicable:
        requirements.append(
            {
                "id": profile,
                "profile": profile,
                "status": "inapplicable",
                "action": "skip",
                "reason": "the accepted change contract does not select this optional project profile",
            }
        )

    selection = {
        "schemaVersion": 1,
        "changeId": state["changeId"],
        "phase": state["phase"],
        "status": "blocked" if blocked else "ready",
        "checkpoint": current,
        "requirements": requirements,
        "executeProfiles": execute,
        "reuseProfiles": reuse,
        "inapplicableProfiles": inapplicable,
        "blockedProfiles": blocked,
        **(
            {
                "assuranceProfiles": list(assurance_profiles),
                "assuranceSelectionDigest": digest_json(assurance_selection),
                "assurancePolicyDigest": assurance_selection["policyDigest"],
            }
            if assurance_selection is not None
            else {}
        ),
    }
    return validate_document(
        selection,
        "verification-selection",
        schema_root=schemas_root(process_root),
        source="verification selection",
    )


def resolve_verification_work(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
) -> dict[str, Any]:
    """Explain the current required work without executing consumer commands."""
    state = _load_state(project_root, process_root, change_id)
    project = load_project(project_root, process_root)
    return _verification_selection(project_root, process_root, project, state)


def resolve_impact_work(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    *,
    profiles: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Explain strict changed-path impact without executing consumer commands."""
    state = _load_state(project_root, process_root, change_id)
    try:
        project = load_project(project_root, process_root)
    except ProcessError as error:
        return unavailable_impact_selection(
            project_root,
            process_root,
            state,
            str(error),
            profiles=profiles,
        )
    return resolve_impact_selection(
        project_root,
        process_root,
        project,
        state,
        profiles=profiles,
    )


@history_transaction
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
    if publication_required(project):
        issues = branch_issues(current_branch(project_root))
        if issues:
            raise ProcessError(
                "publication branch validation failed: " + "; ".join(issues)
            )

    actor = _actor(actor_id, context_id, kind)
    comparison_base = resolve_commit(project_root, contract["comparisonBase"])
    state: dict[str, Any] = {
        "schemaVersion": 1,
        "changeId": change_id,
        "phase": "specified",
        "cycle": 0,
        "contract": {"digest": digest_json(contract), "document": contract},
        "comparisonBaseCommit": comparison_base,
        "plan": None,
        "implementations": [],
        "currentImplementation": None,
        "verification": {},
        "reviewAssignment": None,
        "review": None,
        "reviewHistory": [],
        "receipt": None,
        "requiredPlanSchemaVersion": PLAN_SCHEMA_VERSION,
        "requiredReviewSchemaVersion": REVIEW_SCHEMA_VERSION,
        "controlPaths": [],
        "history": [],
    }
    _remember_control_path(state, project_root, contract_path)
    _event(state, "started", actor)
    _save_state(project_root, process_root, state)
    return state


@history_transaction
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
    validate_plan_assessments(plan, process_root)
    expected_schema = state["requiredPlanSchemaVersion"]
    if plan["schemaVersion"] != expected_schema:
        raise ProcessError(
            f"plan schemaVersion must be {expected_schema} for this change"
        )
    if plan["changeId"] != change_id:
        raise ProcessError("plan changeId does not match lifecycle state")
    if plan["contractDigest"] != state["contract"]["digest"]:
        raise ProcessError("plan contractDigest does not match the accepted contract")
    actor = _actor(actor_id, context_id, kind)
    _remember_control_path(state, project_root, plan_path)
    state["plan"] = {"digest": digest_json(plan), "document": plan}
    state["requiredReviewSchemaVersion"] = REVIEW_SCHEMA_VERSION
    state["phase"] = "planned"
    _event(state, "planned", actor)
    _event(
        state,
        "plan-scope-registered",
        actor,
        policy="literal-repository-boundaries-v1",
        source="plan.workItems[].affectedPaths",
    )
    _save_state(project_root, process_root, state)
    return state


@history_transaction
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
        if state["phase"] not in {"verified", "review-pending", "approved", "completed"}:
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
    state["receipt"] = None
    state["phase"] = "implementing"
    _event(state, "implementation-started", actor, cycle=state["cycle"])
    _save_state(project_root, process_root, state)
    return state


def _publication_preflight(
    project_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    if not publication_required(project):
        return None
    if not (comparison_base := state.get("comparisonBaseCommit")):
        raise ProcessError("publication requires a pinned comparison base")
    publication = validate_current_source(project_root, comparison_base)
    if publication["range"] != f"{comparison_base}..{checkpoint['head']}":
        raise ProcessError("publication validation does not match the candidate HEAD")
    if publication["issues"]:
        raise ProcessError(
            "publication checks failed: "
            + "; ".join(publication["issues"])
            + "; commit the candidate on a valid publication branch before change verify"
        )
    require_committed_candidate(project_root, checkpoint["head"])
    after = repository_snapshot(project_root)
    if (not same_checkpoint(checkpoint, after)
            or current_branch(project_root) != publication["branch"]):
        raise ProcessError("repository changed while publication validation was running")
    return publication


def verify_change(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    profile: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    initial = _load_state(project_root, process_root, change_id)
    _require_phase(initial, "implementing")
    lock_path = _run_path(project_root, initial["changeId"])
    with verification_lock(lock_path):
        state = _load_state(project_root, process_root, change_id)
        _require_phase(state, "implementing")
        project = load_project(project_root, process_root)
        _require_current_baseline(project, state)
        if profile not in state["contract"]["document"]["requiredProfiles"]:
            raise ProcessError(f"profile {profile} is not required by change {change_id}")

        _require_planned_scope(project_root, state)
        before = repository_snapshot(project_root)
        _publication_preflight(project_root, project, state, before)
        report = run_profile(project_root, project, profile)
        return _record_verification(
            project_root, process_root, change_id, state["cycle"], before, report
        )


def verify_impact_change(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    profile: str,
    selection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a consumer-opted final impact assurance profile."""
    initial = _load_state(project_root, process_root, change_id)
    _require_phase(initial, "implementing")
    lock_path = _run_path(project_root, initial["changeId"])
    with verification_lock(lock_path):
        state = _load_state(project_root, process_root, change_id)
        _require_phase(state, "implementing")
        project = load_project(project_root, process_root)
        _require_current_baseline(project, state)
        if profile not in state["contract"]["document"]["requiredProfiles"]:
            raise ProcessError(f"profile {profile} is not required by change {change_id}")
        if selection.get("status") != "ready":
            raise ProcessError("final impact assurance selection is not ready")
        if profile not in selection.get("assuranceProfiles", ()):
            raise ProcessError(f"profile {profile} is not opted into final impact assurance")

        _require_planned_scope(project_root, state)
        before = repository_snapshot(project_root)
        if not same_checkpoint(before, selection["checkpoint"]):
            raise ProcessError("final impact assurance selection is stale")
        if selection.get("assurancePolicyDigest") != digest_json(project.get("impactProfiles", {})):
            raise ProcessError("final impact assurance policy changed after selection")
        _publication_preflight(project_root, project, state, before)

        impact_selection = resolve_impact_selection(
            project_root,
            process_root,
            project,
            state,
            profiles=(profile,),
        )
        if impact_selection["status"] != "ready":
            raise ProcessError("final impact assurance selection changed after selection")
        lookup = impact_unit_lookup(project, impact_selection)
        selected_units = [
            lookup[(item["profile"], item["id"])]
            for item in impact_selection["selectedUnits"]
            if item["profile"] == profile
        ]
        if not selected_units:
            raise ProcessError(f"final impact assurance selected no units for profile {profile}")
        report = run_profile(
            project_root,
            {"profiles": {profile: selected_units}},
            profile,
        )
        report["executionMode"] = "impact-assurance"
        report["selectionDigest"] = digest_json(selection)
        return _record_verification(
            project_root, process_root, change_id, state["cycle"], before, report
        )


@history_transaction
def reuse_verification(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    profile: str,
) -> dict[str, Any]:
    """Record reuse of valid prior evidence without faking a new execution."""
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "implementing")
    project = load_project(project_root, process_root)
    selection = _verification_selection(project_root, process_root, project, state)
    requirement = next(
        (item for item in selection["requirements"] if item["profile"] == profile),
        None,
    )
    if requirement is None:
        raise ProcessError(f"profile {profile} is not part of the accepted verification selection")
    if requirement["action"] != "reuse":
        raise ProcessError(
            f"profile {profile} is not reusable: {requirement['reason']}"
        )
    _record_reuse_event(state, profile, requirement)
    _save_state(project_root, process_root, state)
    return state


def _record_reuse_event(
    state: dict[str, Any], profile: str, requirement: dict[str, Any]
) -> None:
    evidence = state["verification"][profile]
    _event(
        state,
        "profile-reused",
        state["currentImplementation"]["actor"],
        profile=profile,
        reason=requirement["reason"],
        evidenceRecordedAt=evidence["recordedAt"],
        inputDigest=evidence["inputDigest"],
    )


@history_transaction
def reuse_verifications(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    profiles: tuple[str, ...],
) -> dict[str, Any]:
    """Record a stable batch of reusable profile evidence with one state write."""
    if not profiles or len(set(profiles)) != len(profiles):
        raise ProcessError("profile reuse batch must contain unique profiles")
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "implementing")
    project = load_project(project_root, process_root)
    selection = _verification_selection(project_root, process_root, project, state)
    if selection["status"] == "blocked":
        raise ProcessError(
            "verification selection is blocked: "
            + ", ".join(selection["blockedProfiles"])
        )
    requirements = {
        item["profile"]: item
        for item in selection["requirements"]
        if item["profile"] in profiles
    }
    missing = [profile for profile in profiles if profile not in requirements]
    if missing:
        raise ProcessError(
            "profiles are not part of the accepted verification selection: "
            + ", ".join(missing)
        )
    not_reusable = [
        profile
        for profile in profiles
        if requirements[profile]["action"] != "reuse"
    ]
    if not_reusable:
        reasons = "; ".join(
            f"{profile}: {requirements[profile]['reason']}"
            for profile in not_reusable
        )
        raise ProcessError(f"profiles are not reusable: {reasons}")

    current = repository_snapshot(project_root)
    if not same_checkpoint(current, selection["checkpoint"]):
        raise ProcessError("repository changed while profile reuse was being selected")
    for profile in profiles:
        _record_reuse_event(state, profile, requirements[profile])
    _save_state(project_root, process_root, state)
    return state


def verify_remaining(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute only unresolved work and retain valid prior profile evidence."""
    initial = _load_state(project_root, process_root, change_id)
    _require_phase(initial, "implementing")
    lock_path = _run_path(project_root, initial["changeId"])
    with verification_lock(lock_path):
        state = _load_state(project_root, process_root, change_id)
        _require_phase(state, "implementing")
        _require_planned_scope(project_root, state)
        executed: list[str] = []
        reused: list[str] = []
        completed: set[str] = set()
        while True:
            state = _load_state(project_root, process_root, change_id)
            if state["phase"] == "verified":
                break
            _require_phase(state, "implementing")
            project = load_project(project_root, process_root)
            selection = _verification_selection(project_root, process_root, project, state)
            if selection["status"] == "blocked":
                raise ProcessError(
                    "verification selection is blocked: "
                    + ", ".join(selection["blockedProfiles"])
                )
            requirement = next(
                (
                    item
                    for item in selection["requirements"]
                    if item["action"] in {"execute", "reuse"}
                    and item["profile"] not in completed
                ),
                None,
            )
            if requirement is None:
                break
            profile = requirement["profile"]
            if requirement["action"] == "reuse":
                batch: list[str] = []
                for candidate in selection["requirements"]:
                    candidate_profile = candidate["profile"]
                    if candidate_profile in completed:
                        continue
                    if candidate["action"] != "reuse":
                        break
                    batch.append(candidate_profile)
                state = reuse_verifications(
                    project_root,
                    process_root,
                    project,
                    change_id,
                    tuple(batch),
                )
                reused.extend(batch)
                completed.update(batch)
            else:
                if requirement.get("mode") == "impact-assurance":
                    state, report = verify_impact_change(
                        project_root,
                        process_root,
                        project,
                        change_id,
                        profile,
                        selection,
                    )
                else:
                    state, report = verify_change(
                        project_root, process_root, project, change_id, profile
                    )
                executed.append(profile)
                if report["status"] != "passed":
                    break
                completed.add(profile)

        state = _load_state(project_root, process_root, change_id)
        project = load_project(project_root, process_root)
        final_selection = _verification_selection(
            project_root,
            process_root,
            project,
            state,
        )
        if final_selection["status"] == "blocked":
            raise ProcessError(
                "verification selection is blocked: "
                + ", ".join(final_selection["blockedProfiles"])
            )
        final_selection["executeProfiles"] = executed
        final_selection["reuseProfiles"] = reused
        final_selection = validate_document(
            final_selection,
            "verification-selection",
            schema_root=schemas_root(process_root),
            source="verification selection",
        )
    return state, final_selection


def verify_affected(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    change_id: str,
    *,
    profiles: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Run only resolved impact units as non-completion feedback evidence."""
    initial = _load_state(project_root, process_root, change_id)
    _require_phase(initial, "implementing")
    lock_path = _run_path(project_root, initial["changeId"])
    with verification_lock(lock_path):
        state = _load_state(project_root, process_root, change_id)
        _require_phase(state, "implementing")
        try:
            project = load_project(project_root, process_root)
        except ProcessError as error:
            selection = unavailable_impact_selection(
                project_root,
                process_root,
                state,
                str(error),
                profiles=profiles,
            )
            state = _record_impact_event(
                project_root,
                process_root,
                change_id,
                "impact-unresolved",
                {
                    "selectionDigest": digest_json(selection),
                    "changedPathCount": len(selection["changedPaths"]),
                    "unresolvedPathCount": len(selection["unresolvedPaths"]),
                },
            )
            return state, selection, []
        _require_current_baseline(project, state)
        before = repository_snapshot(project_root)
        selection = resolve_impact_selection(
            project_root,
            process_root,
            project,
            state,
            profiles=profiles,
        )
        if selection["status"] != "ready":
            state = _record_impact_event(
                project_root,
                process_root,
                change_id,
                "impact-unresolved",
                {
                    "selectionDigest": digest_json(selection),
                    "changedPathCount": len(selection["changedPaths"]),
                    "unresolvedPathCount": len(selection["unresolvedPaths"]),
                },
            )
            return state, selection, []

        lookup = impact_unit_lookup(project, selection)
        started = time.monotonic()
        executions: list[dict[str, Any]] = []
        for selected in selection["selectedUnits"]:
            unit = lookup[(selected["profile"], selected["id"])]
            report = run_check(project_root, unit)
            executions.append(
                {
                    "profile": selected["profile"],
                    "id": selected["id"],
                    "matchedPaths": selected["matchedPaths"],
                    "status": report["status"],
                    "durationMs": report["durationMs"],
                    "launchCount": 1,
                }
            )
            if report["status"] != "passed":
                break
        after = repository_snapshot(project_root)
        mutation = not same_checkpoint(before, after)
        if mutation:
            executions.append(
                {
                    "profile": "impact",
                    "id": "repository-immutability",
                    "matchedPaths": [],
                    "status": "failed",
                    "durationMs": 0,
                    "launchCount": 0,
                    "reason": "repository changed while affected verification was running",
                }
            )
        elapsed = int((time.monotonic() - started) * 1000)
        child_duration = sum(item["durationMs"] for item in executions)
        status = (
            "passed"
            if not mutation
            and len(executions) == len(selection["selectedUnits"])
            and all(item["status"] == "passed" for item in executions)
            else "failed"
        )
        summary = {
            "status": status,
            "launchCount": sum(item["launchCount"] for item in executions),
            "durationMs": elapsed,
            "scriptDurationMs": child_duration,
            "processOverheadMs": max(0, elapsed - child_duration),
            "units": executions,
        }
        state = _record_impact_event(
            project_root,
            process_root,
            change_id,
            "impact-verified",
            {
                "selectionDigest": digest_json(selection),
                "status": status,
                "changedPathCount": len(selection["changedPaths"]),
                "unitCount": len(selection["selectedUnits"]),
                "launchCount": summary["launchCount"],
                "durationMs": elapsed,
                "scriptDurationMs": child_duration,
                "processOverheadMs": summary["processOverheadMs"],
            },
        )
        return state, selection, [
            summary
        ]


@history_transaction
def _record_impact_event(
    project_root: Path,
    process_root: Path,
    change_id: str,
    event: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "implementing")
    _event(
        state,
        event,
        state["currentImplementation"]["actor"],
        **details,
    )
    _save_state(project_root, process_root, state)
    return state


@history_transaction
def _record_verification(
    project_root: Path,
    process_root: Path,
    change_id: str,
    cycle: int,
    before: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Profiles run outside the lock. Reload so their publication cannot erase
    # participants or evidence recorded by another command while they ran.
    state = _load_state(project_root, process_root, change_id)
    _require_phase(state, "implementing")
    if state["cycle"] != cycle:
        raise ProcessError("implementation cycle changed while verification was running")
    project = load_project(project_root, process_root)
    runtime = execution_identity()
    after = repository_snapshot(project_root)
    authority_digest = distribution_digest(process_root)
    input_digests = {
        name: _verification_input_digest(
            project_root,
            process_root,
            project,
            state,
            name,
            runtime=runtime,
            authority_digest=authority_digest,
        )
        for name in {*state["verification"], report["profile"]}
    }
    retained_verification: dict[str, Any] = {}
    for name, previous in state["verification"].items():
        if _verification_report_matches_inputs(
            project_root,
            process_root,
            project,
            state,
            name,
            previous,
            before,
            require_input=True,
            runtime=runtime,
            authority_digest=authority_digest,
        ):
            retained_verification[name] = previous
        else:
            actor = (
                state["currentImplementation"]["actor"]
                if state.get("currentImplementation")
                else {"actorId": "coordinator", "contextId": "lifecycle", "kind": "agent"}
            )
            _event(
                state,
                "evidence-invalidated",
                actor,
                profile=name,
                reason=(
                    "checkpoint-mismatch"
                    if not same_checkpoint(previous.get("checkpoint", {}), before)
                    else "input-digest-mismatch"
                ),
                cycle=state.get("cycle", 1),
                **(
                    {"recordedInputDigest": previous["inputDigest"]}
                    if previous.get("inputDigest")
                    else {}
                ),
                **(
                    {"currentInputDigest": input_digests[name]}
                    if input_digests.get(name)
                    else {}
                ),
                **(
                    {"recordedCheckpointFingerprint": previous["checkpoint"]["fingerprint"]}
                    if previous.get("checkpoint", {}).get("fingerprint")
                    else {}
                ),
                **(
                    {"currentCheckpointFingerprint": before["fingerprint"]}
                    if before.get("fingerprint")
                    else {}
                ),
            )
    state["verification"] = retained_verification
    profile = report["profile"]
    report["checkpoint"] = after
    report["recordedAt"] = _now()
    input_digest = input_digests[profile]
    if input_digest is not None:
        report["inputDigest"] = input_digest
    else:
        report.pop("inputDigest", None)
    if not same_checkpoint(before, after):
        report["status"] = "failed"
        report["reason"] = "repository changed while verification was running"
    state["verification"][profile] = report

    required = state["contract"]["document"]["requiredProfiles"]
    all_passed = all(
        name in state["verification"]
        and _verification_report_matches_inputs(
            project_root,
            process_root,
            project,
            state,
            name,
            state["verification"][name],
            after,
            require_input=True,
            runtime=runtime,
            authority_digest=authority_digest,
        )
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


@history_transaction
def start_review(
    project_root: Path,
    process_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    replace_reused: bool = False,
) -> dict[str, Any]:
    state = _load_state(project_root, process_root, change_id)
    previous_assignment = conflict = None
    if replace_reused:
        _require_phase(state, "review-pending")
        if (state["reviewAssignment"] is None or state["review"] is not None
                or state["reviewHistory"]
                or any(event["event"] == "review-submitted" for event in state["history"])):
            raise ProcessError("only an unsubmitted initial review assignment can be replaced")
        previous_assignment = deepcopy(state["reviewAssignment"])
        if previous_assignment["reviewer"]["kind"] != "agent":
            raise ProcessError("replace-reused requires a recorded reused agent context")
        report_path = _run_path(project_root, change_id).with_name(f"review-{state['cycle']}.json")
        if report_path.exists() or report_path.is_symlink():
            raise ProcessError("a review report already exists; assignment replacement would discard it")
        conflict = recorded_context_conflict(
            project_root, process_root, state, previous_assignment["reviewer"]["contextId"]
        )
        if conflict is None:
            raise ProcessError("the pending reviewer context has no recorded cross-change reuse")
    else:
        _require_phase(state, "verified")
    reviewer = _actor(actor_id, context_id, kind)
    project = load_project(project_root, process_root)
    _require_current_baseline(project, state)
    _require_planned_scope(project_root, state)
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
    require_unreused_context(project_root, process_root, state, reviewer)

    checkpoint = repository_snapshot(project_root)
    if not _required_verification_matches_inputs(
        project_root,
        process_root,
        project,
        state,
        checkpoint,
        require_input=True,
    ):
        raise ProcessError("verification evidence is stale or incomplete")
    if previous_assignment is not None and not same_checkpoint(
        previous_assignment["checkpoint"], checkpoint
    ):
        raise ProcessError("repository changed after the reused review assignment")
    _publication_preflight(
        project_root, project, state, checkpoint
    )
    state["reviewAssignment"] = {
        "reviewer": reviewer,
        "checkpoint": checkpoint,
        "startedAt": _now(),
        "reportSchemaVersion": state["requiredReviewSchemaVersion"],
    }
    state["phase"] = "review-pending"
    if previous_assignment is None:
        _event(state, "review-started", reviewer)
    else:
        _event(
            state, "review-assignment-replaced", reviewer,
            reason="cross-change-context-reuse",
            previousAssignment=previous_assignment,
            replacementAssignment=deepcopy(state["reviewAssignment"]),
            conflict=conflict,
        )
    _save_state(project_root, process_root, state)
    return state


@history_transaction
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
    expected_schema = assignment["reportSchemaVersion"]
    if review["schemaVersion"] != expected_schema:
        raise ProcessError(f"review schemaVersion must be {expected_schema} for this assignment")
    validate_review_assessments(review, process_root)
    if review["changeId"] != change_id:
        raise ProcessError("review changeId does not match lifecycle state")
    if review["reviewer"] != assignment["reviewer"]:
        raise ProcessError("reviewer does not match the assigned independent identity")
    require_unreused_context(project_root, process_root, state, assignment["reviewer"])
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
    first_pass_origins = {"contract", "production-invariant"}
    if not state["reviewHistory"] and any(
        finding["origin"] not in first_pass_origins for finding in blocking
    ):
        raise ProcessError(
            "first-pass blockers must originate in the frozen contract or production invariant floor"
        )
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

    current_findings = {finding["id"]: finding for finding in review["findings"]}
    for finding_id, prior in prior_findings.items():
        if prior["severity"] != "blocking":
            continue
        current_finding = current_findings.get(finding_id)
        if current_finding is None:
            raise ProcessError(f"prior blocking finding must not be omitted: {finding_id}")
        if (
            current_finding["severity"] != "blocking"
            and current_finding.get("disposition", {}).get("status") != "resolved"
        ):
            raise ProcessError(f"prior blocking finding must remain blocking or be resolved: {finding_id}")

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


@history_transaction
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
    require_unreused_context(
        project_root, process_root, state, state["reviewAssignment"]["reviewer"]
    )
    actor = _actor(actor_id, context_id, kind)
    project = load_project(project_root, process_root)
    _require_current_baseline(project, state)
    _require_planned_scope(project_root, state)
    checkpoint = repository_snapshot(project_root)
    publication = _publication_preflight(project_root, project, state, checkpoint)
    if not same_checkpoint(checkpoint, state["reviewAssignment"]["checkpoint"]):
        raise ProcessError("repository changed after approval")
    if not _required_verification_matches_inputs(
        project_root,
        process_root,
        project,
        state,
        checkpoint,
        require_input=True,
    ):
        raise ProcessError("verification evidence is stale or incomplete")

    evidence: dict[str, Any] = {}
    for name, report in sorted(state["verification"].items()):
        evidence[name] = {
            "status": report["status"],
            "checkpoint": report["checkpoint"],
            **(
                {"executionMode": report["executionMode"]}
                if report.get("executionMode")
                else {}
            ),
            **(
                {"selectionDigest": report["selectionDigest"]}
                if report.get("selectionDigest")
                else {}
            ),
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

    try:
        from .incidents import process_improvement_intake
        process_improvement_intake(
            project_root, process_root, state, actor, project=project
        )
    except Exception:
        _event(
            state,
            "process-improvement-failed",
            actor,
            stage="intake",
            errorCode="process-improvement-intake-failed",
        )

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
    if publication is not None:
        receipt["publication"] = {
            "branch": publication["branch"],
            "subject": publication["subject"],
            "range": publication["range"],
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
