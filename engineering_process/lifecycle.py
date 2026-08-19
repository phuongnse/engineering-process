from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    MAX_JSON_BYTES,
    PROFILE_PATTERN,
    Project,
    _validate_legacy_review,
    read_json,
    validate_change,
    validate_completion,
    validate_plan,
    validate_review,
    validate_verification,
)
from .environment import require_environment_profile
from .git import run_git
from .runner import run_profile, source_state


PHASES = {
    "specified",
    "planned",
    "implementing",
    "verified",
    "review-pending",
    "changes-requested",
    "approved",
    "completed",
}

FINDING_IDENTITY_FIELDS = (
    "id",
    "severity",
    "path",
    "line",
    "summary",
    "evidence",
)
UNRESOLVED_FINDING_STATUSES = {"open", "deferred"}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _digest_file(path: Path) -> str:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError as error:
        raise ContractError(f"{path}: cannot read lifecycle artifact: {error}") from error


def _resolve_commit(project_root: Path, reference: str) -> str:
    result = run_git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"],
        label=f"resolve comparison base {reference}",
        timeout_seconds=10,
        max_stdout_bytes=128,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"comparison base {reference} does not resolve to a commit"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.decode("ascii").strip()


def _actor(actor_id: str, context_id: str, kind: str) -> dict[str, str]:
    if not actor_id or actor_id != actor_id.strip() or len(actor_id) > 256:
        raise ContractError("actor id must be a non-empty trimmed value up to 256 characters")
    if not context_id or context_id != context_id.strip() or len(context_id) > 256:
        raise ContractError(
            "context id must be a non-empty trimmed value up to 256 characters"
        )
    if kind not in {"agent", "human"}:
        raise ContractError("actor kind must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def _runs_root(project_root: Path) -> Path:
    return project_root / ".process" / "runs"


def lifecycle_environment_issues(project_root: Path) -> list[str]:
    issues: list[str] = []
    try:
        top = run_git(
            project_root,
            ["rev-parse", "--show-toplevel"],
            label="inspect Git lifecycle boundary",
            timeout_seconds=10,
            max_stdout_bytes=4096,
        )
    except ContractError as error:
        return [f"cannot inspect Git lifecycle boundary: {error}"]
    if top.returncode != 0:
        return ["canonical lifecycle requires a Git repository"]
    try:
        repository_root = Path(top.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError:
        return ["Git repository root is not valid UTF-8"]
    if repository_root != project_root.resolve():
        issues.append(
            f"project root {project_root.resolve()} must equal Git root {repository_root}"
        )
    try:
        ignore = run_git(
            project_root,
            ["check-ignore", "-q", ".process/runs/__process_probe__"],
            label="inspect lifecycle evidence ignore rule",
            timeout_seconds=10,
            max_stdout_bytes=128,
        )
    except ContractError as error:
        issues.append(f"cannot inspect lifecycle evidence ignore rule: {error}")
        return issues
    if ignore.returncode != 0:
        issues.append(
            ".process/runs/ must be ignored so lifecycle evidence cannot dirty source"
        )
    return issues


def _run_root(project_root: Path, change_id: str) -> Path:
    if PROFILE_PATTERN.fullmatch(change_id) is None or len(change_id) > 64:
        raise ContractError(f"invalid change id: {change_id}")
    return _runs_root(project_root) / change_id


@contextmanager
def _change_lock(project_root: Path, change_id: str):
    lock_root = _runs_root(project_root) / ".locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    path = lock_root / f"{change_id}.lock"
    handle = path.open("a+b")
    try:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError(
                    f"change {change_id} is being mutated by another process"
                ) from error
        else:
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ContractError(
                    f"change {change_id} is being mutated by another process"
                ) from error
        yield
    finally:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


def _state_path(project_root: Path, change_id: str) -> Path:
    return _run_root(project_root, change_id) / "state.json"


def _relative(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ContractError(f"{path}: lifecycle artifacts must stay within the project") from error


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(content) > MAX_JSON_BYTES:
        raise ContractError(
            f"{path}: lifecycle artifact exceeds the {MAX_JSON_BYTES} byte limit"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_document(
    project_root: Path,
    source: Path,
    destination: Path,
) -> dict[str, str]:
    document = read_json(source)
    content = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_bytes(content)
    except OSError as error:
        raise ContractError(f"{destination}: cannot write lifecycle artifact: {error}") from error
    return {
        "path": _relative(project_root, destination),
        "digest": _digest_bytes(content),
    }


def _artifact_path(project_root: Path, artifact: dict[str, str]) -> Path:
    relative = artifact.get("path")
    digest = artifact.get("digest")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise ContractError("lifecycle state contains an invalid artifact reference")
    path = (project_root / relative).resolve()
    _relative(project_root, path)
    if _digest_file(path) != digest:
        raise ContractError(f"{path}: lifecycle artifact digest is stale")
    return path


def _event(
    state: dict[str, Any],
    event: str,
    actor: dict[str, str] | None,
    **details: Any,
) -> None:
    state["revision"] += 1
    record: dict[str, Any] = {
        "revision": state["revision"],
        "event": event,
        "at": _timestamp(),
    }
    if actor is not None:
        record["actor"] = actor
    record.update(details)
    state["history"].append(record)


def _validate_state(state: Any, path: Path) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ContractError(f"{path}: lifecycle state must be an object")
    required = {
        "schemaVersion",
        "changeId",
        "project",
        "phase",
        "cycle",
        "revision",
        "comparisonBase",
        "contract",
        "plan",
        "implementationActors",
        "verification",
        "pendingFindings",
        "reviewAssignment",
        "review",
        "completion",
        "history",
    }
    missing = sorted(required - set(state))
    extra = sorted(set(state) - required)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if extra:
            detail.append(f"unknown: {', '.join(extra)}")
        raise ContractError(f"{path}: invalid lifecycle state ({'; '.join(detail)})")
    if state["schemaVersion"] != 2:
        raise ContractError(f"{path}.schemaVersion: must be 2")
    if state["phase"] not in PHASES:
        raise ContractError(f"{path}.phase: invalid phase")
    if not isinstance(state["cycle"], int) or state["cycle"] < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")
    if not isinstance(state["revision"], int) or state["revision"] < 1:
        raise ContractError(f"{path}.revision: must be a positive integer")
    if not isinstance(state["history"], list) or not state["history"]:
        raise ContractError(f"{path}.history: must not be empty")
    if not isinstance(state["pendingFindings"], list):
        raise ContractError(f"{path}.pendingFindings: must be an array")
    return state


def _same_finding_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in FINDING_IDENTITY_FIELDS)


def _replay_pending_findings(
    project_root: Path, state: dict[str, Any], path: Path
) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    review_events = 0
    for event in state.get("history", []):
        if not isinstance(event, dict) or event.get("event") != "review-submitted":
            continue
        review_events += 1
        artifact = event.get("report")
        if not isinstance(artifact, dict):
            raise ContractError(
                f"{path}: cannot safely migrate review history without report artifacts"
            )
        report_path = _artifact_path(project_root, artifact)
        report = read_json(report_path)
        _validate_legacy_review(report, str(report_path))
        for finding in report["findings"]:
            identifier = finding["id"]
            previous = pending.get(identifier)
            if previous is not None and not _same_finding_identity(previous, finding):
                raise ContractError(
                    f"{path}: cannot safely migrate finding {identifier} because its "
                    "identity changed"
                )
            if finding["status"] in UNRESOLVED_FINDING_STATUSES:
                pending[identifier] = dict(finding)
            elif previous is not None:
                del pending[identifier]
    existing = state.get("pendingFindings")
    if review_events == 0 and isinstance(existing, list):
        return [dict(finding) for finding in existing]
    return list(pending.values())


def _migrate_state(project_root: Path, state: Any, path: Path) -> dict[str, Any]:
    if not isinstance(state, dict) or state.get("schemaVersion") != 1:
        return state
    migrated = dict(state)
    migrated["pendingFindings"] = _replay_pending_findings(project_root, state, path)
    if migrated["pendingFindings"] and migrated.get("phase") in {
        "approved",
        "completed",
    }:
        migrated["phase"] = "changes-requested"
        migrated["completion"] = None
    migrated["schemaVersion"] = 2
    return migrated


def load_state(project_root: Path, change_id: str) -> dict[str, Any]:
    path = _state_path(project_root, change_id)
    state = _migrate_state(project_root, read_json(path), path)
    return _validate_state(state, path)


def _save_state(project_root: Path, state: dict[str, Any]) -> None:
    _validate_state(state, _state_path(project_root, state["changeId"]))
    _write_atomic(_state_path(project_root, state["changeId"]), state)


def _require_phase(state: dict[str, Any], *allowed: str) -> None:
    if state["phase"] not in allowed:
        expected = ", ".join(allowed)
        raise ContractError(
            f"change {state['changeId']} is {state['phase']}; expected {expected}"
        )


def _contract(project_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    document = read_json(_artifact_path(project_root, state["contract"]))
    validate_change(document, "registered change")
    return document


def _plan(project_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["plan"] is None:
        raise ContractError(f"change {state['changeId']} has no registered plan")
    document = read_json(_artifact_path(project_root, state["plan"]))
    validate_plan(document, "registered plan")
    return document


def _start_change_unlocked(
    project_root: Path,
    project: Project,
    contract_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    document = read_json(contract_path)
    validate_change(document, str(contract_path))
    if document["schemaVersion"] != 3:
        raise ContractError(
            f"{contract_path}: new lifecycle runs require change schemaVersion 3; "
            "schemaVersion 2 remains readable only for historical runs"
        )
    change_id = document["id"]
    if project.identifier not in document["affectedProjects"]:
        raise ContractError(
            f"change {change_id} does not include project {project.identifier}"
        )
    missing_profiles = sorted(set(document["requiredProfiles"]) - set(project.profiles))
    if missing_profiles:
        raise ContractError(
            f"change {change_id} requires undefined profiles: {', '.join(missing_profiles)}"
        )
    missing_baseline = sorted(
        set(project.required_profiles) - set(document["requiredProfiles"])
    )
    if missing_baseline:
        raise ContractError(
            f"change {change_id} omits project lifecycle profiles: "
            f"{', '.join(missing_baseline)}"
        )
    assessed_dimensions = {
        assessment["dimension"] for assessment in document["quality"]["assessments"]
    }
    missing_quality_extensions = sorted(
        set(project.quality_extensions) - assessed_dimensions
    )
    if missing_quality_extensions:
        raise ContractError(
            f"change {change_id} omits project quality dimensions: "
            f"{', '.join(missing_quality_extensions)}"
        )
    run_root = _run_root(project_root, change_id)
    if run_root.exists():
        raise ContractError(f"change {change_id} already exists")
    actor = _actor(actor_id, context_id, kind)
    comparison_base = _resolve_commit(project_root, document["comparisonBase"])
    contract = _copy_document(project_root, contract_path, run_root / "contract.json")
    now = _timestamp()
    state: dict[str, Any] = {
        "schemaVersion": 2,
        "changeId": change_id,
        "project": project.identifier,
        "phase": "specified",
        "cycle": 1,
        "revision": 1,
        "comparisonBase": comparison_base,
        "contract": contract,
        "plan": None,
        "implementationActors": [],
        "verification": [],
        "pendingFindings": [],
        "reviewAssignment": None,
        "review": None,
        "completion": None,
        "history": [
            {
                "revision": 1,
                "event": "specified",
                "at": now,
                "actor": actor,
                "comparisonBaseRef": document["comparisonBase"],
                "comparisonBase": comparison_base,
            }
        ],
    }
    _save_state(project_root, state)
    return state


def _register_plan_unlocked(
    project_root: Path,
    project: Project,
    change_id: str,
    plan_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "specified")
    contract = _contract(project_root, state)
    if contract["signOff"]["required"] and contract["signOff"]["status"] != "approved":
        raise ContractError(f"change {change_id} requires sign-off before planning")
    document = read_json(plan_path)
    validate_plan(document, str(plan_path))
    if contract["schemaVersion"] == 3 and document["schemaVersion"] != 2:
        raise ContractError("new schema-3 changes require a bounded schema-2 plan")
    if document["changeId"] != change_id:
        raise ContractError(f"plan changeId does not match {change_id}")
    if document["contractDigest"] != state["contract"]["digest"]:
        raise ContractError("plan contractDigest does not match the registered contract")
    if document["openDecisions"]:
        raise ContractError("implementation plan has unresolved open decisions")
    contract_criteria = {item["id"] for item in contract["acceptanceCriteria"]}
    planned_criteria = {item["criterionId"] for item in document["acceptancePlan"]}
    if contract_criteria != planned_criteria:
        missing = sorted(contract_criteria - planned_criteria)
        extra = sorted(planned_criteria - contract_criteria)
        raise ContractError(
            "acceptance plan must map the exact contract criteria"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unknown: {', '.join(extra)}" if extra else "")
        )
    used_profiles = {
        profile
        for item in document["workItems"]
        for profile in item["verificationProfiles"]
    } | {
        profile
        for item in document["acceptancePlan"]
        for profile in item["verificationProfiles"]
    }
    unknown_profiles = sorted(used_profiles - set(project.profiles))
    if unknown_profiles:
        raise ContractError(
            f"plan references undefined profiles: {', '.join(unknown_profiles)}"
        )
    missing_required = sorted(set(contract["requiredProfiles"]) - used_profiles)
    if missing_required:
        raise ContractError(
            f"plan does not use required profiles: {', '.join(missing_required)}"
        )
    actor = _actor(actor_id, context_id, kind)
    artifact = _copy_document(
        project_root, plan_path, _run_root(project_root, change_id) / "plan.json"
    )
    state["plan"] = artifact
    state["phase"] = "planned"
    _event(state, "planned", actor)
    _save_state(project_root, state)
    return state


def _begin_implementation_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "planned", "implementing", "changes-requested")
    _contract(project_root, state)
    _plan(project_root, state)
    actor = _actor(actor_id, context_id, kind)
    if state["phase"] == "changes-requested":
        state["cycle"] += 1
        state["implementationActors"] = []
        state["verification"] = []
        state["reviewAssignment"] = None
        state["review"] = None
    if actor not in state["implementationActors"]:
        state["implementationActors"].append(actor)
    state["phase"] = "implementing"
    _event(state, "implementation-started", actor, cycle=state["cycle"])
    _save_state(project_root, state)
    return state


def _verify_change_unlocked(
    project_root: Path,
    project: Project,
    change_id: str,
    profile: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "implementing")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    actor = _actor(actor_id, context_id, kind)
    if actor not in state["implementationActors"]:
        raise ContractError("only a registered implementation actor may record verification")
    if profile not in contract["requiredProfiles"]:
        raise ContractError(f"profile {profile} is not required by change {change_id}")
    require_environment_profile(project_root, project, profile=profile)
    report = run_profile(
        project_root,
        project,
        profile,
        base_ref=contract["comparisonBase"],
    )
    validate_verification(report, f"verification profile {profile}")
    report_path = (
        _run_root(project_root, change_id)
        / "verification"
        / f"cycle-{state['cycle']}-{profile}.json"
    )
    _write_atomic(report_path, report)
    eligible = (
        report["status"] == "passed"
        and report["checkpoint"] is not None
        and report["workingTreeDirty"] is False
        and report["workspaceFingerprint"] is not None
        and not report["sourceChangedDuringVerification"]
        and report["workspaceFingerprint"] == report["completedWorkspaceFingerprint"]
    )
    if not eligible:
        _event(
            state,
            "verification-rejected",
            actor,
            cycle=state["cycle"],
            profile=profile,
            report=_relative(project_root, report_path),
            reportDigest=_digest_file(report_path),
        )
        _save_state(project_root, state)
        raise ContractError(
            "lifecycle verification requires passing checks on a clean immutable checkpoint"
        )
    evidence = {
        "profile": profile,
        "path": _relative(project_root, report_path),
        "digest": _digest_file(report_path),
        "checkpoint": report["checkpoint"],
        "workspaceFingerprint": report["workspaceFingerprint"],
    }
    state["verification"] = [
        item for item in state["verification"] if item["profile"] != profile
    ]
    state["verification"].append(evidence)
    state["verification"].sort(key=lambda item: item["profile"])
    current_profiles = {item["profile"] for item in state["verification"]}
    required_profiles = set(contract["requiredProfiles"])
    checkpoints = {item["checkpoint"] for item in state["verification"]}
    fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
    if required_profiles <= current_profiles and len(checkpoints) == 1 and len(fingerprints) == 1:
        state["phase"] = "verified"
    _event(
        state,
        "verification-recorded",
        actor,
        cycle=state["cycle"],
        profile=profile,
        phase=state["phase"],
        evidence=evidence,
    )
    _save_state(project_root, state)
    return state, report


def _start_review_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    method: str,
    attested_by: str,
    evidence: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "verified")
    _contract(project_root, state)
    _plan(project_root, state)
    reviewer = _actor(actor_id, context_id, kind)
    if method not in {"isolated-context", "separate-person"}:
        raise ContractError("review independence method is invalid")
    if (kind == "agent" and method != "isolated-context") or (
        kind == "human" and method != "separate-person"
    ):
        raise ContractError("review independence method does not match reviewer kind")
    if not attested_by or attested_by != attested_by.strip() or len(attested_by) > 256:
        raise ContractError("review attester must be a non-empty trimmed value")
    if attested_by in {reviewer["actorId"], reviewer["contextId"]}:
        raise ContractError("review independence cannot be self-attested")
    if not evidence or evidence != evidence.strip() or len(evidence) > 2000:
        raise ContractError("review independence evidence must be a non-empty trimmed value")
    actor_ids = {item["actorId"] for item in state["implementationActors"]}
    context_ids = {item["contextId"] for item in state["implementationActors"]}
    if reviewer["actorId"] in actor_ids or reviewer["contextId"] in context_ids:
        raise ContractError(
            "independent review requires an actor id and context id unused by implementation"
        )
    source = source_state(project_root)
    checkpoints = {item["checkpoint"] for item in state["verification"]}
    fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
        or checkpoints != {source["checkpoint"]}
        or fingerprints != {source["fingerprint"]}
    ):
        raise ContractError("review cannot start because verification evidence is stale")
    assignment = {
        "changeId": change_id,
        "cycle": state["cycle"],
        "checkpoint": source["checkpoint"],
        "workspaceFingerprint": source["fingerprint"],
        "comparisonBase": state["comparisonBase"],
        "reviewer": reviewer,
        "independence": {
            "method": method,
            "attestedBy": attested_by,
            "evidence": evidence,
        },
        "contract": state["contract"],
        "plan": state["plan"],
        "verification": state["verification"],
        "pendingFindings": state["pendingFindings"],
    }
    assignment_path = _run_root(project_root, change_id) / f"review-request-{state['cycle']}.json"
    _write_atomic(assignment_path, assignment)
    assignment["path"] = _relative(project_root, assignment_path)
    state["reviewAssignment"] = assignment
    state["phase"] = "review-pending"
    _event(
        state,
        "review-started",
        reviewer,
        cycle=state["cycle"],
        request=assignment["path"],
    )
    _save_state(project_root, state)
    return state, assignment


def _submit_review_unlocked(
    project_root: Path,
    change_id: str,
    report_path: Path,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "review-pending")
    assignment = state["reviewAssignment"]
    if not isinstance(assignment, dict):
        raise ContractError("review assignment is missing")
    document = read_json(report_path)
    validate_review(document, str(report_path))
    contract = _contract(project_root, state)
    required_review_schema = 3 if contract["schemaVersion"] == 3 else 2
    if document["schemaVersion"] != required_review_schema:
        raise ContractError(
            f"review schemaVersion {required_review_schema} is required for this change"
        )
    if required_review_schema == 3:
        contract_quality = {
            item["dimension"]: item for item in contract["quality"]["assessments"]
        }
        review_quality = {
            item["dimension"]: item for item in document["quality"]["assessments"]
        }
        if set(contract_quality) != set(review_quality):
            raise ContractError("review quality dimensions do not match the change contract")
        for dimension, accepted in contract_quality.items():
            reviewed = review_quality[dimension]
            expected_status = (
                "verified"
                if accepted["status"] == "applicable"
                else "not-applicable-confirmed"
            )
            if (
                (
                    reviewed["status"] not in {"verified", "failed"}
                    if expected_status == "verified"
                    else reviewed["status"] != expected_status
                )
                or reviewed["criteria"] != accepted["criteria"]
            ):
                raise ContractError(
                    f"review quality assessment for {dimension} does not match the contract"
                )
    for field in (
        "changeId",
        "cycle",
        "checkpoint",
        "workspaceFingerprint",
        "comparisonBase",
        "reviewer",
        "independence",
    ):
        if document[field] != assignment[field]:
            raise ContractError(f"review report {field} does not match its assignment")
    findings_by_id = {finding["id"]: finding for finding in document["findings"]}
    for pending in state["pendingFindings"]:
        current = findings_by_id.get(pending["id"])
        if current is None:
            raise ContractError(
                f"review report must carry forward pending finding {pending['id']}"
            )
        changed = [
            field
            for field in FINDING_IDENTITY_FIELDS
            if current[field] != pending[field]
        ]
        if changed:
            raise ContractError(
                f"review finding {pending['id']} changed immutable fields: "
                + ", ".join(changed)
            )
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] != document["checkpoint"]
        or source["fingerprint"] != document["workspaceFingerprint"]
    ):
        raise ContractError("review report is stale for the current source")
    destination = (
        _run_root(project_root, change_id) / f"review-{state['cycle']}.json"
    )
    artifact = _copy_document(project_root, report_path, destination)
    state["review"] = artifact
    state["pendingFindings"] = [
        dict(finding)
        for finding in document["findings"]
        if finding["status"] in UNRESOLVED_FINDING_STATUSES
    ]
    state["phase"] = (
        "approved" if document["verdict"] == "approved" else "changes-requested"
    )
    _event(
        state,
        "review-submitted",
        document["reviewer"],
        cycle=state["cycle"],
        verdict=document["verdict"],
        report=artifact,
    )
    _save_state(project_root, state)
    return state


def _finish_change_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "approved")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    if state["review"] is None:
        raise ContractError("approved change has no review artifact")
    if state["pendingFindings"]:
        raise ContractError("completion requires every pending finding to be resolved")
    review = read_json(_artifact_path(project_root, state["review"]))
    validate_review(review, "registered review")
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] != review["checkpoint"]
        or source["fingerprint"] != review["workspaceFingerprint"]
    ):
        raise ContractError("approved review is stale for the current source")
    required = set(contract["requiredProfiles"])
    if {item["profile"] for item in state["verification"]} != required:
        raise ContractError("completion requires the exact required verification profiles")
    for item in state["verification"]:
        report = read_json(_artifact_path(project_root, item))
        validate_verification(report, f"verification profile {item['profile']}")
        if report.get("status") != "passed":
            raise ContractError(f"verification profile {item['profile']} is not passing")
        if (
            report.get("checkpoint") != review["checkpoint"]
            or report.get("workspaceFingerprint") != review["workspaceFingerprint"]
        ):
            raise ContractError(f"verification profile {item['profile']} is stale")
    actor = _actor(actor_id, context_id, kind)
    completion = {
        "schemaVersion": 1,
        "changeId": change_id,
        "cycle": state["cycle"],
        "checkpoint": review["checkpoint"],
        "workspaceFingerprint": review["workspaceFingerprint"],
        "comparisonBase": state["comparisonBase"],
        "completedAt": _timestamp(),
        "completedBy": actor,
        "contract": state["contract"],
        "plan": state["plan"],
        "verification": state["verification"],
        "review": state["review"],
    }
    validate_completion(completion, "completion")
    completion_path = _run_root(project_root, change_id) / "completion.json"
    _write_atomic(completion_path, completion)
    state["completion"] = {
        "path": _relative(project_root, completion_path),
        "digest": _digest_file(completion_path),
    }
    state["phase"] = "completed"
    _event(state, "completed", actor, cycle=state["cycle"])
    _save_state(project_root, state)
    return state, completion


def lifecycle_status(project_root: Path, change_id: str) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    issues: list[str] = []
    for name in ("contract", "plan", "review", "completion"):
        artifact = state[name]
        if artifact is None:
            continue
        try:
            _artifact_path(project_root, artifact)
        except ContractError as error:
            issues.append(str(error))
    for evidence in state["verification"]:
        try:
            _artifact_path(project_root, evidence)
        except ContractError as error:
            issues.append(str(error))
    if state["phase"] in {"verified", "review-pending", "approved", "completed"}:
        source = source_state(project_root)
        checkpoints = {item["checkpoint"] for item in state["verification"]}
        fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
        if (
            source["dirty"] is not False
            or source["checkpoint"] is None
            or source["fingerprint"] is None
            or checkpoints != {source["checkpoint"]}
            or fingerprints != {source["fingerprint"]}
        ):
            issues.append("current source no longer matches lifecycle verification")
    return {**state, "current": not issues, "issues": issues}


def start_change(
    project_root: Path,
    project: Project,
    contract_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    document = read_json(contract_path)
    validate_change(document, str(contract_path))
    change_id = document["id"]
    with _change_lock(project_root, change_id):
        return _start_change_unlocked(
            project_root,
            project,
            contract_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def register_plan(
    project_root: Path,
    project: Project,
    change_id: str,
    plan_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _register_plan_unlocked(
            project_root,
            project,
            change_id,
            plan_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def begin_implementation(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _begin_implementation_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def verify_change(
    project_root: Path,
    project: Project,
    change_id: str,
    profile: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _verify_change_unlocked(
            project_root,
            project,
            change_id,
            profile,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def start_review(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    method: str,
    attested_by: str,
    evidence: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _start_review_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
            method=method,
            attested_by=attested_by,
            evidence=evidence,
        )


def submit_review(
    project_root: Path,
    change_id: str,
    report_path: Path,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _submit_review_unlocked(project_root, change_id, report_path)


def finish_change(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _finish_change_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )
