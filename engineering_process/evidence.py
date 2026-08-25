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
    validate_process_lock,
    validate_remote_verification_evidence,
    validate_remote_verification_request,
    validate_review,
    validate_verification,
)
from .lifecycle import _change_lock, _validate_state, load_state


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
    receipt: dict[str, Any] = {
        "schemaVersion": 1,
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
        _validate_entry(artifact["manifest"], f"{path}.manifest")
        reports = artifact["verification"]
        if not isinstance(reports, list) or not reports:
            raise ContractError(f"{path}.verification: must not be empty")
        profiles: list[str] = []
        for report_index, raw_report in enumerate(reports):
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
                or report.get("status") != "passed"
                or report.get("checkpoint") != checkpoint
                or report.get("workspaceFingerprint") != fingerprint
            ):
                raise ContractError(f"{report_path}: stale or failed report")
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
    if receipt["schemaVersion"] != 1 or receipt["kind"] != expected_kind:
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
    optional_artifacts = {"remoteVerification"}
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
    required_review_schema = 3 if contract["schemaVersion"] == 3 else 2
    if review["schemaVersion"] != required_review_schema:
        raise ContractError("receipt review schema does not match the change contract")
    if required_review_schema == 3:
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
