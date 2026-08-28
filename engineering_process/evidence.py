from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    canonical_json_digest,
    DIGEST_PATTERN,
    MAX_JSON_BYTES,
    NAME_PATTERN,
    PROFILE_PATTERN,
    SEMVER_PATTERN,
    read_json,
    validate_change,
    validate_completion,
    validate_plan,
    validate_plan_decision_review,
    validate_plan_decision_review_assignment,
    validate_process_lock,
    validate_remote_verification_evidence,
    validate_remote_verification_request,
    validate_review,
    validate_recommendation,
    validate_recommendation_resolution,
    validate_recommendation_review,
    validate_recommendation_review_assignment,
    validate_verification,
)
from .lifecycle import (
    _change_lock,
    _validate_plan_decision_recommendation_binding,
    _validate_review_finding_boundaries,
    _validate_state,
    load_state,
)
from .remote_verification import _report_summary


MAX_RECEIPT_BYTES = 8_000_000
RECEIPT_KIND = "engineering-process-lifecycle-receipt"
BOOTSTRAP_AUTHORIZATION_KIND = "engineering-process-bootstrap-authorization"


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read receipt: {error}") from error
    if len(data) > MAX_RECEIPT_BYTES:
        raise ContractError(f"{path}: receipt exceeds {MAX_RECEIPT_BYTES} bytes")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: invalid UTF-8 JSON receipt: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{path}: receipt must be an object")
    return document


def _entry(project_root: Path, reference: dict[str, Any]) -> dict[str, Any]:
    relative = reference.get("path")
    source_digest = reference.get("digest")
    if not isinstance(relative, str) or not isinstance(source_digest, str):
        raise ContractError("lifecycle artifact reference is invalid")
    try:
        root = project_root.resolve(strict=True)
        path = (project_root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise ContractError(f"lifecycle artifact escapes project: {relative}") from error
    try:
        source_bytes = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot hash lifecycle artifact: {error}") from error
    document = read_json(path)
    source_text = source_bytes.decode("utf-8")
    actual_source_digest = f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
    if actual_source_digest != source_digest:
        raise ContractError(f"{path}: lifecycle artifact digest is stale")
    return {
        "sourceDigest": source_digest,
        "canonicalDigest": _canonical_digest(document),
        "sourceText": source_text,
    }


def _remote_entries(
    project_root: Path, state: dict[str, Any]
) -> dict[str, Any] | None:
    remote = state.get("remoteVerification")
    if remote is None:
        return None
    if not isinstance(remote.get("request"), dict) or not isinstance(
        remote.get("evidence"), dict
    ):
        raise ContractError("completed lifecycle remote evidence is incomplete")
    request_entry = _entry(project_root, remote["request"])
    index_entry = _entry(project_root, remote["evidence"])
    index = _validate_entry(index_entry, "remote verification index")
    source_entry = _entry(project_root, index["sourceEvidence"])
    artifacts: list[dict[str, Any]] = []
    for artifact in index["artifacts"]:
        artifacts.append(
            {
                "requirementId": artifact["requirementId"],
                "selectorId": artifact["selectorId"],
                "archive": artifact["archive"],
                "service": artifact["service"],
                "manifest": _entry(project_root, artifact["manifest"]),
                "verification": [
                    {
                        "profile": reference["profile"],
                        **_entry(project_root, reference),
                    }
                    for reference in artifact["verification"]
                ],
            }
        )
    return {
        "request": request_entry,
        "index": index_entry,
        "sourceEvidence": source_entry,
        "artifacts": artifacts,
    }


def _context_reservation_entry(
    project_root: Path, context_id: str
) -> dict[str, Any]:
    context_digest = hashlib.sha256(context_id.encode("utf-8")).hexdigest()
    path = (
        project_root
        / ".process"
        / "runs"
        / ".review-contexts"
        / f"{context_digest}.json"
    )
    try:
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    except OSError as error:
        raise ContractError(
            f"plan decision context reservation cannot be read: {error}"
        ) from error
    return _entry(
        project_root,
        {
            "path": path.relative_to(project_root).as_posix(),
            "digest": digest,
        },
    )


def _plan_decision_entries(
    project_root: Path, state: dict[str, Any]
) -> dict[str, Any] | None:
    decision = state.get("planDecision")
    if decision is None:
        return None
    result: dict[str, Any] = {
        "kind": decision["kind"],
        "authorized": decision["authorized"],
        "generatedInputs": [],
    }
    for field in (
        "assignment",
        "review",
        "recommendation",
        "recommendationAssignment",
        "recommendationReview",
        "resolution",
    ):
        reference = decision[field]
        result[field] = _entry(project_root, reference) if reference is not None else None
    result["contextReservation"] = None
    result["recommendationContextReservation"] = None
    if decision["kind"] == "process-generated":
        plan = read_json(project_root / state["plan"]["path"])
        result["generatedInputs"] = [
            {
                "path": source_input["path"],
                "artifact": _entry(
                    project_root,
                    {
                        "path": source_input["path"],
                        "digest": source_input["sha256"],
                    },
                ),
            }
            for source_input in plan["provenance"]["inputs"]
        ]
    if decision["assignment"] is not None:
        assignment = read_json(project_root / decision["assignment"]["path"])
        result["contextReservation"] = _context_reservation_entry(
            project_root, assignment["reviewer"]["contextId"]
        )
    if decision["recommendationAssignment"] is not None:
        assignment = read_json(
            project_root / decision["recommendationAssignment"]["path"]
        )
        result["recommendationContextReservation"] = _context_reservation_entry(
            project_root, assignment["reviewer"]["contextId"]
        )
    return result


def _review_loop_entries(
    project_root: Path, state: dict[str, Any]
) -> list[dict[str, Any]] | None:
    review_loop = state.get("reviewLoop")
    if review_loop is None:
        return None
    entries: list[dict[str, Any]] = []
    for escalation in review_loop["escalations"]:
        decision = escalation["decision"]
        if decision is None:
            continue
        entries.append(
            {
                "id": escalation["id"],
                **{
                    field: _entry(project_root, decision[field])
                    for field in (
                        "planDecisionAssignment",
                        "planDecisionReview",
                        "recommendation",
                        "recommendationAssignment",
                        "recommendationReview",
                        "resolution",
                    )
                },
                "contextReservation": _context_reservation_entry(
                    project_root,
                    read_json(
                        project_root / decision["planDecisionAssignment"]["path"]
                    )["reviewer"]["contextId"],
                ),
                "recommendationContextReservation": _context_reservation_entry(
                    project_root,
                    read_json(
                        project_root / decision["recommendationAssignment"]["path"]
                    )["reviewer"]["contextId"],
                ),
            }
        )
    return entries or None


def _export_evidence(
    project_root: Path,
    change_id: str,
    output: Path,
    *,
    kind: str,
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    state = load_state(project_root, change_id)
    if state["phase"] != "completed" or state["completion"] is None:
        raise ContractError(f"change {change_id} must be completed before evidence export")
    lock_path = project_root / ".process" / "process.lock"
    lock = validate_process_lock(read_json(lock_path), str(lock_path))
    artifacts = {
        "contract": _entry(project_root, state["contract"]),
        "plan": _entry(project_root, state["plan"]),
        "verification": [
            {
                "profile": reference["profile"],
                **_entry(project_root, reference),
            }
            for reference in state["verification"]
        ],
        "review": _entry(project_root, state["review"]),
        "completion": _entry(project_root, state["completion"]),
    }
    remote_entries = _remote_entries(project_root, state)
    if remote_entries is not None:
        artifacts["remoteVerification"] = remote_entries
    plan_decision_entries = _plan_decision_entries(project_root, state)
    if plan_decision_entries is not None:
        artifacts["planDecision"] = plan_decision_entries
    review_loop_entries = _review_loop_entries(project_root, state)
    if review_loop_entries is not None:
        artifacts["reviewLoop"] = review_loop_entries
    transition = state.get("authorityTransition")
    if transition is not None:
        artifacts["authorityTransition"] = {
            "request": _entry(project_root, transition["request"]),
            "candidateEvidence": _entry(
                project_root, transition["candidateEvidence"]
            ),
        }
    receipt: dict[str, Any] = {
        "schemaVersion": 2 if transition is not None else 1,
        "kind": kind,
        "process": {"version": lock.version, "digest": lock.digest},
        "project": state["project"],
        "changeId": state["changeId"],
        "cycle": state["cycle"],
        "checkpoint": _validate_entry(
            artifacts["completion"], "receipt.artifacts.completion"
        )["checkpoint"],
        "comparisonBase": state["comparisonBase"],
        "state": {
            "canonicalDigest": _canonical_digest(state),
            "document": state,
        },
        "artifacts": artifacts,
    }
    data = json.dumps(
        receipt, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_RECEIPT_BYTES:
        raise ContractError(
            f"lifecycle receipt exceeds {MAX_RECEIPT_BYTES} bytes: {len(data)}"
        )
    output = output.resolve()
    runs_root = (project_root / ".process" / "runs").resolve()
    try:
        output.relative_to(runs_root)
    except ValueError:
        pass
    else:
        raise ContractError("lifecycle receipts must be exported outside .process/runs")
    if output.exists():
        raise ContractError(f"{output}: refusing to replace an existing receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        validated = _validate_evidence(temporary, expected_kind=kind)
        temporary.replace(output)
    except ContractError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContractError(f"{output}: cannot export lifecycle receipt: {error}") from error
    return validated


def export_receipt(project_root: Path, change_id: str, output: Path) -> dict[str, Any]:
    return _export_evidence(
        project_root,
        change_id,
        output,
        kind=RECEIPT_KIND,
    )


def export_bootstrap_authorization(
    project_root: Path, change_id: str, output: Path
) -> dict[str, Any]:
    return _export_evidence(
        project_root,
        change_id,
        output,
        kind=BOOTSTRAP_AUTHORIZATION_KIND,
    )


def _require_exact(value: dict[str, Any], expected: set[str], path: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise ContractError(f"{path}: invalid fields ({'; '.join(detail)})")


def _validate_entry(entry: Any, path: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ContractError(f"{path}: must be an object")
    _require_exact(entry, {"sourceDigest", "canonicalDigest", "sourceText"}, path)
    source_text = entry["sourceText"]
    if not isinstance(source_text, str):
        raise ContractError(f"{path}.sourceText: must be a string")
    source_bytes = source_text.encode("utf-8")
    if len(source_bytes) > MAX_JSON_BYTES:
        raise ContractError(f"{path}.sourceText: exceeds the 1 MB artifact limit")
    try:
        document = json.loads(source_text)
    except json.JSONDecodeError as error:
        raise ContractError(f"{path}.sourceText: invalid JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{path}.sourceText: artifact must be an object")
    if entry["canonicalDigest"] != _canonical_digest(document):
        raise ContractError(f"{path}.canonicalDigest: does not match document")
    if (
        not isinstance(entry["sourceDigest"], str)
        or DIGEST_PATTERN.fullmatch(entry["sourceDigest"]) is None
    ):
        raise ContractError(f"{path}.sourceDigest: invalid digest")
    if entry["sourceDigest"] != f"sha256:{hashlib.sha256(source_bytes).hexdigest()}":
        raise ContractError(f"{path}.sourceDigest: does not match sourceText")
    return document


def _validate_plan_decision_receipt(
    value: Any,
    *,
    state: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    process: dict[str, str],
) -> None:
    path = "receipt.artifacts.planDecision"
    if not isinstance(value, dict):
        raise ContractError(f"{path}: must be an object")
    fields = {
        "kind",
        "authorized",
        "generatedInputs",
        "assignment",
        "review",
        "recommendation",
        "recommendationAssignment",
        "recommendationReview",
        "resolution",
        "contextReservation",
        "recommendationContextReservation",
    }
    _require_exact(value, fields, path)
    decision = state.get("planDecision")
    if (
        not isinstance(decision, dict)
        or value["kind"] != decision["kind"]
        or value["authorized"] is not True
        or decision["authorized"] is not True
        or plan.get("schemaVersion") != 3
        or plan.get("provenance", {}).get("kind") != decision["kind"]
    ):
        raise ContractError(f"{path}: does not match the authorized lifecycle state")
    documents: dict[str, dict[str, Any] | None] = {}
    for field in (
        "assignment",
        "review",
        "recommendation",
        "recommendationAssignment",
        "recommendationReview",
        "resolution",
    ):
        reference = decision[field]
        entry = value[field]
        if (reference is None) != (entry is None):
            raise ContractError(f"{path}.{field}: presence does not match lifecycle state")
        if entry is None:
            documents[field] = None
            continue
        if entry.get("sourceDigest") != reference["digest"]:
            raise ContractError(f"{path}.{field}: source digest does not match state")
        documents[field] = _validate_entry(entry, f"{path}.{field}")
    if decision["kind"] == "process-generated":
        if (
            any(documents.values())
            or value["contextReservation"] is not None
            or value["recommendationContextReservation"] is not None
        ):
            raise ContractError(f"{path}: generated plan carries semantic review evidence")
        generated_inputs = value["generatedInputs"]
        if not isinstance(generated_inputs, list) or not generated_inputs:
            raise ContractError(f"{path}: generated plan inputs are missing")
        input_documents: dict[str, dict[str, Any]] = {}
        input_digests: dict[str, str] = {}
        paths: list[str] = []
        for index, item in enumerate(generated_inputs):
            item_path = f"{path}.generatedInputs[{index}]"
            if not isinstance(item, dict):
                raise ContractError(f"{item_path}: must be an object")
            _require_exact(item, {"path", "artifact"}, item_path)
            relative = item["path"]
            if not isinstance(relative, str):
                raise ContractError(f"{item_path}.path: must be a string")
            paths.append(relative)
            document = _validate_entry(item["artifact"], f"{item_path}.artifact")
            input_documents[relative] = document
            input_digests[relative] = item["artifact"]["sourceDigest"]
        provenance_inputs = plan["provenance"]["inputs"]
        if paths != [item["path"] for item in provenance_inputs] or any(
            input_digests[item["path"]] != item["sha256"]
            for item in provenance_inputs
        ):
            raise ContractError(f"{path}: generated inputs do not match plan provenance")
        from .release_candidate import validate_generated_release_lifecycle_documents

        validate_generated_release_lifecycle_documents(
            project=state["project"],
            contract=contract,
            plan=plan,
            input_documents=input_documents,
            input_digests=input_digests,
            authority=process,
        )
        return

    if value["generatedInputs"] != []:
        raise ContractError(f"{path}: authored plan cannot carry generated inputs")

    assignment = documents["assignment"]
    review = documents["review"]
    if assignment is None or review is None:
        raise ContractError(f"{path}: authored plan lacks fresh assessment evidence")
    validate_plan_decision_review_assignment(assignment, f"{path}.assignment")
    validate_plan_decision_review(review, f"{path}.review")
    if (
        assignment["changeId"] != state["changeId"]
        or assignment["authority"] != process
        or assignment["contractSha256"] != canonical_json_digest(contract)
        or assignment["planSha256"] != canonical_json_digest(plan)
        or review["assignmentSha256"] != canonical_json_digest(assignment)
        or review["reviewer"] != assignment["reviewer"]
        or review["contractSha256"] != assignment["contractSha256"]
        or review["planSha256"] != assignment["planSha256"]
    ):
        raise ContractError(f"{path}: authored assessment chain is stale")
    context = _validate_entry(value["contextReservation"], f"{path}.contextReservation")
    if (
        canonical_json_digest(context) != assignment["contextReservationSha256"]
        or context.get("actorId") != assignment["reviewer"]["actorId"]
        or context.get("kind") != assignment["reviewer"]["kind"]
        or context.get("changeId") != state["changeId"]
        or context.get("cycle") != assignment["cycle"]
    ):
        raise ContractError(f"{path}.contextReservation: does not match assignment")
    if any(
        actor.get("actorId") == assignment["reviewer"]["actorId"]
        or actor.get("contextId") == assignment["reviewer"]["contextId"]
        for actor in state.get("implementationActors", [])
    ):
        raise ContractError(f"{path}: plan reviewer appears as an implementation actor")
    chain_fields = (
        "recommendation",
        "recommendationAssignment",
        "recommendationReview",
        "resolution",
    )
    if review["verdict"] == "clear":
        if any(documents[field] is not None for field in chain_fields):
            raise ContractError(f"{path}: clear assessment carries a recommendation chain")
        if value["recommendationContextReservation"] is not None:
            raise ContractError(f"{path}: clear assessment carries a recommendation context")
        return
    if any(documents[field] is None for field in chain_fields):
        raise ContractError(f"{path}: decision-required assessment lacks a complete chain")
    recommendation = documents["recommendation"]
    recommendation_assignment = documents["recommendationAssignment"]
    recommendation_review = documents["recommendationReview"]
    resolution = documents["resolution"]
    assert recommendation is not None
    assert recommendation_assignment is not None
    assert recommendation_review is not None
    assert resolution is not None
    classifications = validate_recommendation(recommendation, f"{path}.recommendation")
    validate_recommendation_review_assignment(
        recommendation_assignment, f"{path}.recommendationAssignment"
    )
    validate_recommendation_review(
        recommendation_review, f"{path}.recommendationReview"
    )
    validate_recommendation_resolution(resolution, f"{path}.resolution")
    _validate_plan_decision_recommendation_binding(recommendation, review)
    recommendation_digest = canonical_json_digest(recommendation)
    assignment_digest = canonical_json_digest(recommendation_assignment)
    recommendation_review_digest = canonical_json_digest(recommendation_review)
    recommendation_context = _validate_entry(
        value["recommendationContextReservation"],
        f"{path}.recommendationContextReservation",
    )
    expected_invariants = [item["id"] for item in recommendation["invariants"]]
    reviewed_invariants = [
        item["invariantId"]
        for item in recommendation_review["invariantAssessments"]
    ]
    expected_options = [item["id"] for item in recommendation["options"]]
    reviewed_options = [
        item["optionId"] for item in recommendation_review["optionAssessments"]
    ]
    if (
        recommendation_assignment["decisionId"] != recommendation["decisionId"]
        or recommendation_review["decisionId"] != recommendation["decisionId"]
        or resolution["decisionId"] != recommendation["decisionId"]
        or recommendation_assignment["recommendationSha256"] != recommendation_digest
        or recommendation_assignment["coordinator"] != recommendation["coordinator"]
        or canonical_json_digest(recommendation_context)
        != recommendation_assignment["contextReservationSha256"]
        or recommendation_context.get("actorId")
        != recommendation_assignment["reviewer"]["actorId"]
        or recommendation_context.get("kind")
        != recommendation_assignment["reviewer"]["kind"]
        or recommendation_context.get("changeId")
        != f"recommendation-{recommendation['decisionId']}"
        or recommendation_context.get("cycle") != 1
        or recommendation_review["recommendationSha256"] != recommendation_digest
        or recommendation_review["assignmentSha256"] != assignment_digest
        or recommendation_review["reviewer"] != recommendation_assignment["reviewer"]
        or recommendation_review["verdict"] != "approved"
        or reviewed_invariants != expected_invariants
        or reviewed_options != expected_options
        or resolution["recommendationSha256"] != recommendation_digest
        or resolution["assignmentSha256"] != assignment_digest
        or resolution["reviewSha256"] != recommendation_review_digest
        or classifications.get(resolution["selectedOptionId"]) != "valid"
    ):
        raise ContractError(f"{path}: owner-decision chain is stale or invalid")


def _validate_review_loop_receipt(
    value: Any,
    *,
    state: dict[str, Any],
    contract: dict[str, Any],
    plan: dict[str, Any],
    process: dict[str, str],
) -> None:
    path = "receipt.artifacts.reviewLoop"
    if not isinstance(value, list) or not value:
        raise ContractError(f"{path}: must be a non-empty array")
    review_loop = state.get("reviewLoop")
    if review_loop is None:
        raise ContractError(f"{path}: lifecycle state has no review loop")
    expected = {
        item["id"]: item
        for item in review_loop["escalations"]
        if item["decision"] is not None
    }
    observed_ids: list[str] = []
    source_fields = {
        "assignment": "planDecisionAssignment",
        "review": "planDecisionReview",
        "recommendation": "recommendation",
        "recommendationAssignment": "recommendationAssignment",
        "recommendationReview": "recommendationReview",
        "resolution": "resolution",
    }
    for index, raw_entry in enumerate(value):
        entry_path = f"{path}[{index}]"
        if not isinstance(raw_entry, dict):
            raise ContractError(f"{entry_path}: must be an object")
        _require_exact(
            raw_entry,
            {
                "id",
                *source_fields.values(),
                "contextReservation",
                "recommendationContextReservation",
            },
            entry_path,
        )
        identifier = raw_entry["id"]
        escalation = expected.get(identifier)
        if escalation is None:
            raise ContractError(f"{entry_path}.id: does not match lifecycle state")
        observed_ids.append(identifier)
        decision = escalation["decision"]
        assert decision is not None
        decision_state = {
            "kind": "authored",
            "authorized": True,
            **{
                target: decision[source]
                for target, source in source_fields.items()
            },
        }
        synthetic_state = {**state, "planDecision": decision_state}
        synthetic_artifact = {
            "kind": "authored",
            "authorized": True,
            "generatedInputs": [],
            **{
                target: raw_entry[source]
                for target, source in source_fields.items()
            },
            "contextReservation": raw_entry["contextReservation"],
            "recommendationContextReservation": raw_entry[
                "recommendationContextReservation"
            ],
        }
        _validate_plan_decision_receipt(
            synthetic_artifact,
            state=synthetic_state,
            contract=contract,
            plan=plan,
            process=process,
        )
    if observed_ids != sorted(expected):
        raise ContractError(f"{path}: ids must match state in sorted order")


def _validate_remote_receipt(
    remote: Any,
    *,
    state: dict[str, Any],
    contract: dict[str, Any],
    checkpoint: str,
    fingerprint: str,
) -> None:
    if not isinstance(remote, dict):
        raise ContractError("receipt.artifacts.remoteVerification: must be an object")
    _require_exact(
        remote,
        {"request", "index", "sourceEvidence", "artifacts"},
        "receipt.artifacts.remoteVerification",
    )
    request = _validate_entry(
        remote["request"], "receipt.artifacts.remoteVerification.request"
    )
    validate_remote_verification_request(request, "receipt remote request")
    index = _validate_entry(
        remote["index"], "receipt.artifacts.remoteVerification.index"
    )
    source_evidence = _validate_entry(
        remote["sourceEvidence"],
        "receipt.artifacts.remoteVerification.sourceEvidence",
    )
    validate_remote_verification_evidence(
        source_evidence, "receipt remote source evidence"
    )
    if (
        request["changeId"] != state["changeId"]
        or request["cycle"] != state["cycle"]
        or request["project"] != state["project"]
        or request["checkpoint"] != checkpoint
        or request["comparisonBase"] != state["comparisonBase"]
        or request["workspaceFingerprint"] != fingerprint
        or index.get("requestSha256") != canonical_json_digest(request)
        or source_evidence["requestSha256"] != canonical_json_digest(request)
    ):
        raise ContractError("receipt remote verification identity is inconsistent")
    expected_index_keys = {
        "schemaVersion",
        "kind",
        "requestSha256",
        "checkpoint",
        "comparisonBase",
        "workspaceFingerprint",
        "sourceEvidence",
        "artifacts",
    }
    if (
        not isinstance(index, dict)
        or set(index) != expected_index_keys
        or index["schemaVersion"] != 1
        or index["kind"]
        != "engineering-process-ingested-remote-verification"
        or index["checkpoint"] != checkpoint
        or index["comparisonBase"] != state["comparisonBase"]
        or index["workspaceFingerprint"] != fingerprint
    ):
        raise ContractError("receipt remote verification index is invalid")
    if (
        not isinstance(index["sourceEvidence"], dict)
        or index["sourceEvidence"].get("digest")
        != remote["sourceEvidence"]["sourceDigest"]
    ):
        raise ContractError(
            "receipt remote source evidence does not match the ingested index"
        )
    artifacts = remote["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ContractError("receipt remote verification artifacts are empty")
    if len(artifacts) != len(index["artifacts"]):
        raise ContractError("receipt remote verification artifact count mismatch")
    identities: list[tuple[str, str]] = []
    for position, (artifact, index_artifact) in enumerate(
        zip(artifacts, index["artifacts"], strict=True)
    ):
        path = f"receipt.artifacts.remoteVerification.artifacts[{position}]"
        if not isinstance(artifact, dict):
            raise ContractError(f"{path}: must be an object")
        _require_exact(
            artifact,
            {
                "requirementId",
                "selectorId",
                "archive",
                "service",
                "manifest",
                "verification",
            },
            path,
        )
        identity = (artifact["requirementId"], artifact["selectorId"])
        identities.append(identity)
        if (
            identity
            != (index_artifact["requirementId"], index_artifact["selectorId"])
            or artifact["archive"] != index_artifact["archive"]
            or artifact["service"] != index_artifact["service"]
        ):
            raise ContractError(f"{path}: does not match ingested index")
        manifest = _validate_entry(artifact["manifest"], f"{path}.manifest")
        if (
            not isinstance(index_artifact.get("manifest"), dict)
            or artifact["manifest"]["sourceDigest"]
            != index_artifact["manifest"].get("digest")
        ):
            raise ContractError(f"{path}.manifest: does not match ingested index")
        if (
            manifest.get("status") != "passed"
            or manifest.get("checkpoint") != checkpoint
            or manifest.get("comparisonBase") != state["comparisonBase"]
            or manifest.get("workspaceFingerprint") != fingerprint
        ):
            raise ContractError(f"{path}.manifest: stale or failed manifest")
        reports = artifact["verification"]
        if not isinstance(reports, list) or not reports:
            raise ContractError(f"{path}.verification: must not be empty")
        index_reports = index_artifact.get("verification")
        manifest_reports = manifest.get("reports")
        if (
            not isinstance(index_reports, list)
            or not isinstance(manifest_reports, list)
            or len(reports) != len(index_reports)
            or len(reports) != len(manifest_reports)
        ):
            raise ContractError(
                f"{path}.verification: index or manifest coverage mismatch"
            )
        profiles: list[str] = []
        for report_index, (raw_report, index_report, manifest_report) in enumerate(
            zip(reports, index_reports, manifest_reports, strict=True)
        ):
            report_path = f"{path}.verification[{report_index}]"
            if not isinstance(raw_report, dict):
                raise ContractError(f"{report_path}: must be an object")
            profile = raw_report.get("profile")
            entry = dict(raw_report)
            entry.pop("profile", None)
            report = _validate_entry(entry, report_path)
            validate_verification(report, report_path)
            if (
                profile != report.get("profile")
                or not isinstance(index_report, dict)
                or profile != index_report.get("profile")
                or not isinstance(index_report.get("path"), str)
                or not isinstance(index_report.get("digest"), str)
                or raw_report.get("sourceDigest") != index_report.get("digest")
                or report.get("status") != "passed"
                or report.get("checkpoint") != checkpoint
                or report.get("workspaceFingerprint") != fingerprint
            ):
                raise ContractError(f"{report_path}: stale or failed report")
            source_bytes = raw_report["sourceText"].encode("utf-8")
            expected_manifest_report = {
                "path": Path(index_report["path"]).name,
                "bytes": len(source_bytes),
                "sha256": f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
                **_report_summary(report),
            }
            if manifest_report != expected_manifest_report:
                raise ContractError(
                    f"{report_path}: does not match the remote manifest"
                )
            profiles.append(profile)
        requirement = next(
            item for item in request["requirements"] if item["id"] == identity[0]
        )
        if profiles != requirement["profiles"]:
            raise ContractError(f"{path}.verification: profile coverage mismatch")
    expected_identities = [
        (requirement["id"], selector["id"])
        for requirement in request["requirements"]
        for selector in requirement["selectors"]
    ]
    if identities != expected_identities:
        raise ContractError("receipt remote verification selector coverage mismatch")
    required_evidence = contract.get("requiredEvidence", [])
    if required_evidence != [item["id"] for item in request["requirements"]]:
        raise ContractError("receipt remote requirements do not match contract")
    state_remote = state.get("remoteVerification")
    if (
        not isinstance(state_remote, dict)
        or state_remote.get("requiredEvidence") != required_evidence
        or state_remote.get("request", {}).get("digest")
        != remote["request"]["sourceDigest"]
        or state_remote.get("evidence", {}).get("digest")
        != remote["index"]["sourceDigest"]
    ):
        raise ContractError("receipt remote references do not match lifecycle state")


def _validate_evidence(path: Path, *, expected_kind: str) -> dict[str, Any]:
    receipt = _read_receipt(path)
    _require_exact(
        receipt,
        {
            "schemaVersion",
            "kind",
            "process",
            "project",
            "changeId",
            "cycle",
            "checkpoint",
            "comparisonBase",
            "state",
            "artifacts",
        },
        "receipt",
    )
    if receipt["schemaVersion"] not in {1, 2} or receipt["kind"] != expected_kind:
        raise ContractError("receipt: unsupported schemaVersion or kind")
    if (
        not isinstance(receipt["project"], str)
        or NAME_PATTERN.fullmatch(receipt["project"]) is None
        or not isinstance(receipt["changeId"], str)
        or PROFILE_PATTERN.fullmatch(receipt["changeId"]) is None
    ):
        raise ContractError("receipt: invalid project or change id")
    if (
        not isinstance(receipt["checkpoint"], str)
        or re.fullmatch(r"[0-9a-f]{40,64}", receipt["checkpoint"]) is None
    ):
        raise ContractError("receipt.checkpoint: invalid commit id")
    if (
        isinstance(receipt["cycle"], bool)
        or not isinstance(receipt["cycle"], int)
        or receipt["cycle"] < 1
    ):
        raise ContractError("receipt.cycle: must be a positive integer")
    process = receipt["process"]
    if not isinstance(process, dict):
        raise ContractError("receipt.process: must be an object")
    _require_exact(process, {"version", "digest"}, "receipt.process")
    if (
        not isinstance(process["version"], str)
        or SEMVER_PATTERN.fullmatch(process["version"]) is None
        or not isinstance(process["digest"], str)
        or DIGEST_PATTERN.fullmatch(process["digest"]) is None
    ):
        raise ContractError("receipt.process: invalid version or digest")

    state_entry = receipt["state"]
    if not isinstance(state_entry, dict):
        raise ContractError("receipt.state: must be an object")
    _require_exact(state_entry, {"canonicalDigest", "document"}, "receipt.state")
    state = state_entry["document"]
    if state_entry["canonicalDigest"] != _canonical_digest(state):
        raise ContractError("receipt.state.canonicalDigest: does not match document")
    _validate_state(state, path)
    if state.get("phase") != "completed":
        raise ContractError("receipt.state: lifecycle is not completed")
    for field in ("project", "changeId", "cycle", "comparisonBase"):
        if state.get(field) != receipt[field]:
            raise ContractError(f"receipt.{field}: does not match lifecycle state")

    artifacts = receipt["artifacts"]
    if not isinstance(artifacts, dict):
        raise ContractError("receipt.artifacts: must be an object")
    required_artifacts = {
        "contract",
        "plan",
        "verification",
        "review",
        "completion",
    }
    optional_artifacts = {
        "authorityTransition",
        "planDecision",
        "remoteVerification",
        "reviewLoop",
    }
    missing_artifacts = required_artifacts - set(artifacts)
    unknown_artifacts = set(artifacts) - required_artifacts - optional_artifacts
    if missing_artifacts or unknown_artifacts:
        raise ContractError(
            "receipt.artifacts: invalid fields"
            + (
                "; missing " + ", ".join(sorted(missing_artifacts))
                if missing_artifacts
                else ""
            )
            + (
                "; unknown " + ", ".join(sorted(unknown_artifacts))
                if unknown_artifacts
                else ""
            )
        )
    contract = _validate_entry(artifacts["contract"], "receipt.artifacts.contract")
    plan = _validate_entry(artifacts["plan"], "receipt.artifacts.plan")
    review = _validate_entry(artifacts["review"], "receipt.artifacts.review")
    completion = _validate_entry(
        artifacts["completion"], "receipt.artifacts.completion"
    )
    validate_change(contract, "receipt contract")
    validate_plan(plan, "receipt plan")
    validate_review(review, "receipt review")
    validate_completion(completion, "receipt completion")
    state_transition = state.get("authorityTransition")
    if (state_transition is not None) != ("authorityTransition" in artifacts):
        raise ContractError(
            "receipt authority transition presence does not match lifecycle state"
        )
    if state_transition is not None:
        if receipt["schemaVersion"] != 2:
            raise ContractError(
                "authority-transition lifecycle receipts require schemaVersion 2"
            )
        transition_artifacts = artifacts["authorityTransition"]
        if not isinstance(transition_artifacts, dict):
            raise ContractError("receipt.artifacts.authorityTransition: must be an object")
        _require_exact(
            transition_artifacts,
            {"request", "candidateEvidence"},
            "receipt.artifacts.authorityTransition",
        )
        request = _validate_entry(
            transition_artifacts["request"],
            "receipt.artifacts.authorityTransition.request",
        )
        candidate_evidence = _validate_entry(
            transition_artifacts["candidateEvidence"],
            "receipt.artifacts.authorityTransition.candidateEvidence",
        )
        from .transition import (
            validate_authority_transition_evidence,
            validate_authority_transition_request,
        )

        validate_authority_transition_request(request, "receipt transition request")
        validate_authority_transition_evidence(
            candidate_evidence, "receipt transition candidate evidence"
        )
        if (
            state_transition["request"]["digest"]
            != transition_artifacts["request"]["sourceDigest"]
            or state_transition["candidateEvidence"]["digest"]
            != transition_artifacts["candidateEvidence"]["sourceDigest"]
            or candidate_evidence["requestSha256"]
            != canonical_json_digest(request)
        ):
            raise ContractError(
                "receipt authority transition references are inconsistent"
            )
    elif receipt["schemaVersion"] != 1:
        raise ContractError(
            "ordinary lifecycle receipts must retain schemaVersion 1"
        )
    state_plan_decision = state.get("planDecision")
    if plan.get("schemaVersion") == 3 and state_plan_decision is None:
        raise ContractError(
            "receipt schema-3 plan is missing required plan decision state"
        )
    if (state_plan_decision is not None) != ("planDecision" in artifacts):
        raise ContractError(
            "receipt plan decision evidence presence does not match lifecycle state"
        )
    if state_plan_decision is not None:
        _validate_plan_decision_receipt(
            artifacts["planDecision"],
            state=state,
            contract=contract,
            plan=plan,
            process=process,
        )
    state_review_loop = state.get("reviewLoop")
    decided_escalations = (
        [
            item
            for item in state_review_loop["escalations"]
            if item["decision"] is not None
        ]
        if state_review_loop is not None
        else []
    )
    if bool(decided_escalations) != ("reviewLoop" in artifacts):
        raise ContractError(
            "receipt review-loop evidence presence does not match lifecycle state"
        )
    if decided_escalations:
        _validate_review_loop_receipt(
            artifacts["reviewLoop"],
            state=state,
            contract=contract,
            plan=plan,
            process=process,
        )
    required_review_schema = (
        4
        if state_transition is not None
        else (3 if contract["schemaVersion"] >= 3 else 2)
    )
    if review["schemaVersion"] != required_review_schema:
        raise ContractError("receipt review schema does not match the change contract")
    if required_review_schema >= 3:
        accepted_quality = {
            item["dimension"]: item for item in contract["quality"]["assessments"]
        }
        reviewed_quality = {
            item["dimension"]: item for item in review["quality"]["assessments"]
        }
        if set(accepted_quality) != set(reviewed_quality):
            raise ContractError("receipt review quality dimensions do not match contract")
        for dimension, accepted in accepted_quality.items():
            reviewed = reviewed_quality[dimension]
            expected_status = (
                "verified"
                if accepted["status"] == "applicable"
                else "not-applicable-confirmed"
            )
            if (
                reviewed["status"] != expected_status
                or reviewed["criteria"] != accepted["criteria"]
            ):
                raise ContractError(
                    f"receipt review quality assessment for {dimension} is inconsistent"
                )
    _validate_review_finding_boundaries(contract, review["findings"])
    if plan.get("contractDigest") != artifacts["contract"]["sourceDigest"]:
        raise ContractError("receipt plan does not bind the contract digest")
    if (
        contract.get("id") != receipt["changeId"]
        or plan.get("changeId") != receipt["changeId"]
        or review.get("changeId") != receipt["changeId"]
        or review.get("cycle") != receipt["cycle"]
        or review.get("comparisonBase") != receipt["comparisonBase"]
    ):
        raise ContractError("receipt artifact change identity is inconsistent")
    if review["verdict"] != "approved":
        raise ContractError("receipt review is not approved")
    verification = artifacts["verification"]
    if not isinstance(verification, list) or not verification:
        raise ContractError("receipt.artifacts.verification: must not be empty")
    profiles: list[str] = []
    checkpoint = receipt["checkpoint"]
    fingerprint = completion.get("workspaceFingerprint")
    remote_required = bool(contract.get("requiredEvidence"))
    if remote_required != ("remoteVerification" in artifacts):
        raise ContractError(
            "receipt remote verification presence does not match contract"
        )
    if remote_required:
        _validate_remote_receipt(
            artifacts["remoteVerification"],
            state=state,
            contract=contract,
            checkpoint=checkpoint,
            fingerprint=fingerprint,
        )
    for index, raw_entry in enumerate(verification):
        if not isinstance(raw_entry, dict):
            raise ContractError(f"receipt.artifacts.verification[{index}]: must be an object")
        profile = raw_entry.get("profile")
        entry = dict(raw_entry)
        entry.pop("profile", None)
        report = _validate_entry(entry, f"receipt.artifacts.verification[{index}]")
        validate_verification(
            report, f"receipt.artifacts.verification[{index}].document"
        )
        if not isinstance(profile, str) or report.get("profile") != profile:
            raise ContractError(f"receipt.artifacts.verification[{index}]: profile mismatch")
        if (
            report.get("status") != "passed"
            or report.get("checkpoint") != checkpoint
            or report.get("workspaceFingerprint") != fingerprint
        ):
            raise ContractError(f"receipt.artifacts.verification[{index}]: stale or failed")
        profiles.append(profile)
    if sorted(profiles) != sorted(contract["requiredProfiles"]):
        raise ContractError("receipt verification profiles do not match the contract")
    if (
        completion.get("changeId") != receipt["changeId"]
        or completion.get("cycle") != receipt["cycle"]
        or completion.get("checkpoint") != checkpoint
        or review.get("checkpoint") != checkpoint
        or review.get("workspaceFingerprint") != fingerprint
    ):
        raise ContractError("receipt completion/review identity is inconsistent")
    for name in ("contract", "plan", "review"):
        if completion.get(name) != state.get(name):
            raise ContractError(f"receipt completion reference for {name} is inconsistent")
    if completion.get("planDecision") != state.get("planDecision"):
        raise ContractError(
            "receipt completion plan decision reference is inconsistent"
        )
    if completion.get("reviewLoop") != state.get("reviewLoop"):
        raise ContractError(
            "receipt completion review-loop evidence is inconsistent"
        )
    if completion.get("authorityTransition") != state_transition:
        raise ContractError(
            "receipt completion authority transition reference is inconsistent"
        )
    if completion.get("verification") != state.get("verification"):
        raise ContractError("receipt completion verification references are inconsistent")
    expected_remote_completion = (
        state["remoteVerification"]["evidence"]
        if state.get("remoteVerification") is not None
        else None
    )
    if completion.get("remoteVerification") != expected_remote_completion:
        raise ContractError(
            "receipt completion remote verification reference is inconsistent"
        )
    state_refs = {
        "contract": state.get("contract"),
        "plan": state.get("plan"),
        "review": state.get("review"),
        "completion": state.get("completion"),
    }
    for name, reference in state_refs.items():
        if not isinstance(reference, dict) or reference.get("digest") != artifacts[name]["sourceDigest"]:
            raise ContractError(f"receipt state reference for {name} is inconsistent")
    if [item.get("digest") for item in state.get("verification", [])] != [
        item["sourceDigest"] for item in verification
    ]:
        raise ContractError("receipt state verification references are inconsistent")
    return {
        "changeId": receipt["changeId"],
        "project": receipt["project"],
        "cycle": receipt["cycle"],
        "checkpoint": checkpoint,
        "comparisonBase": receipt["comparisonBase"],
        "workspaceFingerprint": fingerprint,
        "processVersion": process["version"],
        "processDigest": process["digest"],
        "stateCanonicalDigest": state_entry["canonicalDigest"],
        "sha256": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
    }


def validate_receipt(path: Path) -> dict[str, Any]:
    return _validate_evidence(path, expected_kind=RECEIPT_KIND)


def validate_bootstrap_authorization(path: Path) -> dict[str, Any]:
    return _validate_evidence(
        path,
        expected_kind=BOOTSTRAP_AUTHORIZATION_KIND,
    )


def prune_completed_run(
    project_root: Path, change_id: str, receipt_path: Path, *, apply: bool
) -> dict[str, Any]:
    receipt = validate_receipt(receipt_path)
    with _change_lock(project_root, change_id):
        return _prune_completed_run_unlocked(
            project_root,
            change_id,
            receipt,
            receipt_path=receipt_path,
            apply=apply,
        )


def _prune_completed_run_unlocked(
    project_root: Path,
    change_id: str,
    receipt: dict[str, Any],
    *,
    receipt_path: Path,
    apply: bool,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    if receipt["changeId"] != change_id or receipt["project"] != state["project"]:
        raise ContractError("receipt does not identify the requested lifecycle run")
    if state["phase"] != "completed" or state["completion"] is None:
        raise ContractError("only completed lifecycle runs may be pruned")
    completion = _validate_entry(
        _entry(project_root, state["completion"]), "current completion"
    )
    if receipt["checkpoint"] != completion.get("checkpoint"):
        raise ContractError("receipt checkpoint does not match current completion")
    if receipt["stateCanonicalDigest"] != _canonical_digest(state):
        raise ContractError("receipt does not match the current completed lifecycle state")
    run_root = (project_root / ".process" / "runs" / change_id).resolve(strict=True)
    expected_parent = (project_root / ".process" / "runs").resolve(strict=True)
    if run_root.parent != expected_parent:
        raise ContractError("lifecycle prune target is not a direct run directory")
    try:
        receipt_path.resolve(strict=True).relative_to(run_root)
    except ValueError:
        pass
    else:
        raise ContractError("the retained receipt must stay outside the pruned run")
    result = {**receipt, "target": str(run_root), "applied": apply}
    if not apply:
        return result
    quarantine = expected_parent / f".pruning-{change_id}-{uuid.uuid4().hex}"
    try:
        run_root.replace(quarantine)
        shutil.rmtree(quarantine)
    except OSError as error:
        raise ContractError(
            "cannot prune completed lifecycle run safely; no restored run is "
            "claimed because deletion may be partial. Retain the validated receipt "
            f"for recovery and inspect quarantine {quarantine}: {error}"
        ) from error
    return result
