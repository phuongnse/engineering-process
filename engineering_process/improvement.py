from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import uuid
from typing import Any, Callable
from datetime import UTC, datetime

from .contracts import (
    ContractError,
    MAX_JSON_BYTES,
    canonical_json_digest,
    read_json,
    validate_improvement_catalog,
    validate_improvement_disposition,
    validate_improvement_reproduction,
    validate_improvement_resolution,
    validate_improvement_signal,
    validate_process_lock,
    validate_release,
)


MAX_IMPROVEMENT_CHAIN_BYTES = 12_000_000


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load(
    path: Path,
    validator: Callable[[Any, str], None],
    label: str,
) -> dict[str, Any]:
    document, _data = _stable_json_document(path, label=label)
    validator(document, str(path))
    if not isinstance(document, dict):
        raise ContractError(f"{label}: must be an object")
    return document


def _stable_json_document(
    path: Path,
    *,
    label: str,
    limit: int = MAX_JSON_BYTES,
) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label}: must be a regular non-symlink file")
        if before.st_size > limit:
            raise ContractError(f"{label}: exceeds {limit} bytes")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ContractError(f"{label}: changed while opening")
            content = bytearray()
            while len(content) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            data = bytes(content)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except ContractError:
        raise
    except OSError as error:
        raise ContractError(f"{label}: cannot read {path}: {error}") from error
    if len(data) > limit:
        raise ContractError(f"{label}: exceeds {limit} bytes")
    if (
        len(data) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"{label}: changed while reading")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label}: invalid UTF-8 JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{label}: must be an object")
    return document, data


def _chain_bytes(paths: tuple[Path | None, ...]) -> int:
    total = 0
    for path in paths:
        if path is None:
            continue
        try:
            total += path.stat().st_size
        except OSError as error:
            raise ContractError(f"{path}: cannot inspect improvement artifact: {error}") from error
        if total > MAX_IMPROVEMENT_CHAIN_BYTES:
            raise ContractError(
                "improvement artifact chain exceeds "
                f"{MAX_IMPROVEMENT_CHAIN_BYTES} bytes"
            )
    return total


def _catalog_entry(
    catalog: dict[str, Any], invariant_id: str
) -> dict[str, Any] | None:
    for entry in catalog["entries"]:
        if entry["id"] == invariant_id:
            return entry
    return None


def validate_improvement_chain(
    signal_path: Path,
    disposition_path: Path | None = None,
    resolution_path: Path | None = None,
    reproduction_path: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    if disposition_path is None and any(
        path is not None for path in (resolution_path, reproduction_path)
    ):
        raise ContractError(
            "improvement resolution or reproduction requires a disposition"
        )
    if resolution_path is None and reproduction_path is not None:
        raise ContractError("improvement reproduction requires a resolution")
    if disposition_path is not None and catalog_path is None:
        raise ContractError(
            "improvement disposition validation requires its exact producer catalog"
        )
    _chain_bytes(
        (
            signal_path,
            disposition_path,
            resolution_path,
            reproduction_path,
            catalog_path,
        )
    )
    signal = _load(
        signal_path, validate_improvement_signal, "improvement signal"
    )
    signal_digest = canonical_json_digest(signal)
    target = signal["target"]
    result: dict[str, Any] = {
        "signalId": signal["signalId"],
        "signalSha256": signal_digest,
        "phase": "signal-exported",
        "invariantId": signal["claim"]["proposedInvariantId"],
        "recurrence": "unassessed",
        "nextOwner": target["project"],
        "closed": False,
    }

    catalog: dict[str, Any] | None = None
    if catalog_path is not None:
        catalog = _load(
            catalog_path, validate_improvement_catalog, "improvement catalog"
        )
        if catalog["producer"] != target:
            raise ContractError(
                "improvement catalog producer does not match signal target"
            )

    if disposition_path is None:
        return result
    disposition = _load(
        disposition_path,
        validate_improvement_disposition,
        "improvement disposition",
    )
    if disposition["signalSha256"] != signal_digest:
        raise ContractError(
            "improvement disposition does not bind the canonical signal digest"
        )
    producer = disposition["producer"]
    if (
        producer["project"] != target["project"]
        or producer["repository"] != target["repository"]
    ):
        raise ContractError(
            "improvement disposition producer does not match signal target"
        )
    invariant_id = disposition["canonicalInvariantId"]
    recurrence = disposition["recurrence"]
    if catalog is not None:
        if disposition["catalogSha256"] != canonical_json_digest(catalog):
            raise ContractError(
                "improvement disposition does not bind the supplied catalog"
            )
        entry = _catalog_entry(catalog, invariant_id)
        if recurrence == "new" and entry is not None:
            raise ContractError(
                "new improvement disposition uses a cataloged invariant"
            )
        if recurrence in {"duplicate", "recurrence"} and entry is None:
            raise ContractError(
                "duplicate or recurring improvement disposition lacks a catalog entry"
            )
        if recurrence == "recurrence" and entry is not None and entry["status"] != "resolved":
            raise ContractError(
                "recurring improvement disposition requires a resolved catalog invariant"
            )
        if recurrence == "duplicate" and entry is not None and entry["status"] != "active":
            raise ContractError(
                "duplicate improvement disposition requires an active catalog invariant"
            )
        if (
            recurrence == "duplicate"
            and entry is not None
            and disposition["linkedChangeId"] != entry["activeChangeId"]
        ):
            raise ContractError(
                "duplicate improvement disposition must link the catalog active change"
            )
    result.update(
        {
            "phase": (
                "producer-rejected"
                if disposition["decision"] == "rejected"
                else "producer-disposition"
            ),
            "dispositionSha256": canonical_json_digest(disposition),
            "invariantId": invariant_id,
            "recurrence": recurrence,
            "linkedChangeId": disposition["linkedChangeId"],
            "nextOwner": (
                signal["source"]["project"]
                if disposition["decision"] == "rejected"
                else producer["project"]
            ),
        }
    )
    if disposition["decision"] == "rejected":
        if resolution_path is not None:
            raise ContractError("rejected improvement signal cannot have a resolution")
        return result

    if resolution_path is None:
        return result
    resolution = _load(
        resolution_path, validate_improvement_resolution, "improvement resolution"
    )
    disposition_digest = canonical_json_digest(disposition)
    if (
        resolution["signalSha256"] != signal_digest
        or resolution["dispositionSha256"] != disposition_digest
    ):
        raise ContractError(
            "improvement resolution does not bind signal and disposition digests"
        )
    if resolution["canonicalInvariantId"] != invariant_id:
        raise ContractError(
            "improvement resolution invariant does not match disposition"
        )
    lifecycle = resolution["producerLifecycle"]
    if (
        lifecycle["project"] != producer["project"]
        or lifecycle["changeId"] != disposition["linkedChangeId"]
    ):
        raise ContractError(
            "improvement resolution lifecycle does not match producer disposition"
        )
    release = resolution["release"]
    if release["repository"] != producer["repository"]:
        raise ContractError(
            "improvement resolution release does not match producer repository"
        )
    resolution_digest = canonical_json_digest(resolution)
    result.update(
        {
            "phase": "producer-released",
            "resolutionSha256": resolution_digest,
            "release": release,
            "nextOwner": signal["source"]["project"],
        }
    )

    if reproduction_path is None:
        return result
    reproduction = _load(
        reproduction_path,
        validate_improvement_reproduction,
        "improvement reproduction",
    )
    if (
        reproduction["signalSha256"] != signal_digest
        or reproduction["dispositionSha256"] != disposition_digest
        or reproduction["resolutionSha256"] != resolution_digest
    ):
        raise ContractError(
            "improvement reproduction does not bind the complete artifact chain"
        )
    if reproduction["canonicalInvariantId"] != invariant_id:
        raise ContractError(
            "improvement reproduction invariant does not match disposition"
        )
    consumer = reproduction["consumer"]
    source = signal["source"]
    if (
        consumer["project"] != source["project"]
        or consumer["repository"] != source["repository"]
    ):
        raise ContractError(
            "improvement reproduction consumer does not match signal source"
        )
    reproduced_release = reproduction["release"]
    for field in (
        "repository",
        "version",
        "tag",
        "releaseName",
        "commit",
        "artifactSetSha256",
    ):
        if reproduced_release[field] != release[field]:
            raise ContractError(
                f"improvement reproduction release {field} does not match resolution"
            )
    if consumer["process"]["version"] != release["version"]:
        raise ContractError(
            "improvement reproduction consumer has not adopted the resolved release"
        )
    result.update(
        {
            "phase": "closed",
            "reproductionSha256": canonical_json_digest(reproduction),
            "nextOwner": None,
            "closed": True,
        }
    )
    return result


def write_improvement_artifact(
    document: dict[str, Any],
    output: Path,
    validator: Callable[[Any, str], None],
) -> str:
    validator(document, str(output))
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(data) > 1_000_000:
        raise ContractError("improvement artifact exceeds the 1 MB limit")
    output = output.resolve()
    if output.exists():
        raise ContractError(f"{output}: refusing to replace an improvement artifact")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContractError(f"{output}: cannot write improvement artifact: {error}") from error
    return canonical_json_digest(document)


def _process_identity(project_root: Path) -> dict[str, str]:
    lock_path = project_root / ".process" / "process.lock"
    lock = validate_process_lock(read_json(lock_path), str(lock_path))
    return {"version": lock.version, "digest": lock.digest}


def export_improvement_signal(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    source_repository: str,
    affected_surfaces: list[str],
    reference: str | None,
    output: Path,
    actor_id: str,
    context_id: str,
    actor_kind: str,
) -> dict[str, Any]:
    from .lifecycle import bind_improvement_chain, load_state
    from .runner import source_state

    project_root = project_root.resolve(strict=True)
    state = load_state(project_root, change_id)
    case = next(
        (item for item in state["improvements"] if item["id"] == case_id),
        None,
    )
    if case is None:
        raise ContractError(f"change {change_id} has no improvement case {case_id}")
    classification = case["classification"]
    if (
        classification is None
        or classification["disposition"] != "shared-escalation"
        or case["role"] != "consumer"
    ):
        raise ContractError(
            "only a classified shared consumer case can export a signal"
        )
    if case["signal"] is not None:
        raise ContractError(f"improvement case {case_id} already exported a signal")
    target = classification["target"]
    if target is None:
        raise ContractError("shared improvement classification lacks a producer target")
    evidence_reference = case["evidence"]
    evidence_path = (project_root / evidence_reference["path"]).resolve(strict=True)
    try:
        evidence_path.relative_to(project_root)
    except ValueError as error:
        raise ContractError("improvement evidence escapes the consumer project") from error
    evidence_document, evidence_data = _stable_json_document(
        evidence_path, label="improvement evidence"
    )
    if (
        "sha256:" + hashlib.sha256(evidence_data).hexdigest()
        != evidence_reference["digest"]
    ):
        raise ContractError("improvement evidence digest is stale")
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
    ):
        raise ContractError(
            "improvement signal export requires a clean immutable consumer checkpoint"
        )
    evidence_checkpoint = evidence_document.get("checkpoint")
    evidence_fingerprint = evidence_document.get("workspaceFingerprint")
    checkpoint = (
        evidence_checkpoint
        if isinstance(evidence_checkpoint, str)
        else source["checkpoint"]
    )
    fingerprint = (
        evidence_fingerprint
        if isinstance(evidence_fingerprint, str)
        else source["fingerprint"]
    )
    checks = evidence_document.get("checks")
    first_failed = None
    if isinstance(checks, list):
        first_failed = next(
            (
                check
                for check in checks
                if isinstance(check, dict) and check.get("status") != "passed"
            ),
            None,
        )
    command_digest = None
    diagnostic_digest = None
    if isinstance(first_failed, dict):
        raw_command_digest = first_failed.get("commandSha256")
        if (
            isinstance(raw_command_digest, str)
            and len(raw_command_digest) == 64
        ):
            command_digest = f"sha256:{raw_command_digest}"
        diagnostics = first_failed.get("diagnostics")
        if isinstance(diagnostics, dict):
            matches = diagnostics.get("matches")
            if isinstance(matches, list) and matches and isinstance(matches[0], dict):
                raw_diagnostic_digest = matches[0].get("lineSha256")
                if (
                    isinstance(raw_diagnostic_digest, str)
                    and len(raw_diagnostic_digest) == 64
                ):
                    diagnostic_digest = f"sha256:{raw_diagnostic_digest}"
    surfaces = sorted(set(affected_surfaces))
    signal = {
        "schemaVersion": 1,
        "kind": "engineering-process-improvement-signal",
        "signalId": case_id,
        "createdAt": _timestamp(),
        "source": {
            "project": state["project"],
            "repository": source_repository,
            "checkpoint": checkpoint,
            "workspaceFingerprint": fingerprint,
            "process": _process_identity(project_root),
            "changeId": change_id,
            "cycle": case["sourceCycle"],
        },
        "target": target,
        "trigger": {
            "kind": case["trigger"],
            "status": (
                "changes-requested"
                if case["trigger"] == "review-finding"
                else "failed"
            ),
        },
        "claim": {
            "ownerBoundary": classification["ownerBoundary"],
            "reusableClass": classification["reusableClass"],
            "proposedInvariantId": classification["invariantId"],
            "rationaleSha256": classification["rationaleSha256"],
            "affectedSurfaces": surfaces,
        },
        "evidence": {
            "kind": (
                "review-report"
                if case["trigger"] == "review-finding"
                else "verification-report"
            ),
            "artifactSha256": evidence_reference["digest"],
            "artifactBytes": len(evidence_data),
            "commandSha256": command_digest,
            "diagnosticSha256": diagnostic_digest,
            "reference": reference,
        },
        "controls": {
            "rawOutputIncluded": False,
            "environmentIncluded": False,
            "secretsIncluded": False,
            "grantsAuthority": False,
        },
    }
    signal_digest = write_improvement_artifact(
        signal, output, validate_improvement_signal
    )
    state = bind_improvement_chain(
        project_root,
        change_id,
        case_id,
        signal_path=output,
        catalog_path=None,
        disposition_path=None,
        resolution_path=None,
        reproduction_path=None,
        expected_canonical_digests={"signal": signal_digest},
        chain_phase="signal-exported",
        actor_id=actor_id,
        context_id=context_id,
        kind=actor_kind,
    )
    return {
        "signalId": case_id,
        "signalSha256": signal_digest,
        "output": str(output.resolve()),
        "phase": next(
            item["phase"] for item in state["improvements"] if item["id"] == case_id
        ),
    }


def observe_improvement_signal(
    project_root: Path,
    *,
    signal_id: str,
    source_repository: str,
    target_project: str,
    target_repository: str,
    trigger_kind: str,
    trigger_status: str,
    owner_boundary: str,
    reusable_class: str,
    invariant_id: str,
    rationale_sha256: str,
    affected_surfaces: list[str],
    evidence_kind: str,
    evidence_path: Path,
    reference: str | None,
    change_id: str | None,
    cycle: int | None,
    output: Path,
) -> dict[str, Any]:
    from .runner import source_state

    project_root = project_root.resolve(strict=True)
    project_document = read_json(project_root / ".process" / "project.json")
    if not isinstance(project_document, dict) or not isinstance(
        project_document.get("project"), str
    ):
        raise ContractError("consumer project manifest lacks a project id")
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
    ):
        raise ContractError(
            "external improvement observation requires a clean immutable checkout"
        )
    evidence_path = evidence_path.resolve(strict=True)
    evidence_document, evidence_data = _stable_json_document(
        evidence_path, label="external improvement evidence"
    )
    checkpoint = evidence_document.get("checkpoint", source["checkpoint"])
    fingerprint = evidence_document.get(
        "workspaceFingerprint", source["fingerprint"]
    )
    checks = evidence_document.get("checks")
    first_failed = None
    if isinstance(checks, list):
        first_failed = next(
            (
                check
                for check in checks
                if isinstance(check, dict) and check.get("status") != "passed"
            ),
            None,
        )
    command_digest = None
    diagnostic_digest = None
    if isinstance(first_failed, dict):
        raw_command_digest = first_failed.get("commandSha256")
        if isinstance(raw_command_digest, str) and len(raw_command_digest) == 64:
            command_digest = f"sha256:{raw_command_digest}"
        diagnostics = first_failed.get("diagnostics")
        if isinstance(diagnostics, dict):
            matches = diagnostics.get("matches")
            if isinstance(matches, list) and matches and isinstance(matches[0], dict):
                raw_diagnostic_digest = matches[0].get("lineSha256")
                if isinstance(raw_diagnostic_digest, str) and len(raw_diagnostic_digest) == 64:
                    diagnostic_digest = f"sha256:{raw_diagnostic_digest}"
    evidence_bytes = len(evidence_data)
    evidence_sha256 = "sha256:" + hashlib.sha256(evidence_data).hexdigest()
    signal = {
        "schemaVersion": 1,
        "kind": "engineering-process-improvement-signal",
        "signalId": signal_id,
        "createdAt": _timestamp(),
        "source": {
            "project": project_document["project"],
            "repository": source_repository,
            "checkpoint": checkpoint,
            "workspaceFingerprint": fingerprint,
            "process": _process_identity(project_root),
            "changeId": change_id,
            "cycle": cycle,
        },
        "target": {
            "project": target_project,
            "repository": target_repository,
        },
        "trigger": {"kind": trigger_kind, "status": trigger_status},
        "claim": {
            "ownerBoundary": owner_boundary,
            "reusableClass": reusable_class,
            "proposedInvariantId": invariant_id,
            "rationaleSha256": rationale_sha256,
            "affectedSurfaces": sorted(set(affected_surfaces)),
        },
        "evidence": {
            "kind": evidence_kind,
            "artifactSha256": evidence_sha256,
            "artifactBytes": evidence_bytes,
            "commandSha256": command_digest,
            "diagnosticSha256": diagnostic_digest,
            "reference": reference,
        },
        "controls": {
            "rawOutputIncluded": False,
            "environmentIncluded": False,
            "secretsIncluded": False,
            "grantsAuthority": False,
        },
    }
    digest = write_improvement_artifact(signal, output, validate_improvement_signal)
    return {
        "signalId": signal_id,
        "signalSha256": digest,
        "output": str(output.resolve()),
        "phase": "signal-exported",
        "nextOwner": target_project,
    }


def create_improvement_disposition(
    project_root: Path,
    signal_path: Path,
    catalog_path: Path,
    *,
    producer_repository: str,
    decision: str,
    owner_boundary: str,
    reusable_class: str,
    invariant_id: str,
    linked_change_id: str | None,
    rationale_sha256: str,
    exception_approved_by: str | None,
    exception_evidence_sha256: str | None,
    output: Path,
) -> dict[str, Any]:
    from .lifecycle import load_state
    from .runner import source_state

    project_root = project_root.resolve(strict=True)
    signal = _load(
        signal_path, validate_improvement_signal, "improvement signal"
    )
    catalog = _load(
        catalog_path, validate_improvement_catalog, "improvement catalog"
    )
    project_path = project_root / ".process" / "project.json"
    project = read_json(project_path)
    project_id = project.get("project") if isinstance(project, dict) else None
    if not isinstance(project_id, str):
        raise ContractError("producer project manifest lacks a project id")
    expected_target = {
        "project": project_id,
        "repository": producer_repository,
    }
    if signal["target"] != expected_target or catalog["producer"] != expected_target:
        raise ContractError(
            "signal target and catalog producer must match the disposition producer"
        )
    entry = _catalog_entry(catalog, invariant_id)
    recurrence = (
        "new"
        if entry is None
        else "duplicate"
        if entry["status"] == "active"
        else "recurrence"
    )
    if decision == "duplicate" and recurrence != "duplicate":
        raise ContractError(
            "duplicate disposition requires an active catalog invariant"
        )
    if decision == "accepted" and recurrence == "duplicate":
        raise ContractError(
            "an active catalog invariant must use a duplicate disposition"
        )
    if decision == "rejected":
        recurrence = "not-applicable"
        linked_change_id = None
    elif linked_change_id is None:
        raise ContractError(
            "accepted or duplicate disposition requires a linked producer change"
        )
    else:
        linked_state = load_state(project_root, linked_change_id)
        if linked_state["project"] != project_id:
            raise ContractError(
                "linked improvement change belongs to another producer project"
            )
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
    ):
        raise ContractError(
            "improvement disposition requires a clean immutable producer checkpoint"
        )
    exception = None
    if exception_approved_by is not None or exception_evidence_sha256 is not None:
        if exception_approved_by is None or exception_evidence_sha256 is None:
            raise ContractError(
                "improvement exception requires approver and evidence digest"
            )
        exception = {
            "approvedBy": exception_approved_by,
            "evidenceSha256": exception_evidence_sha256,
        }
    shared_proof = owner_boundary == "shared-process" and decision != "rejected"
    disposition = {
        "schemaVersion": 1,
        "kind": "engineering-process-improvement-disposition",
        "createdAt": _timestamp(),
        "signalSha256": canonical_json_digest(signal),
        "catalogSha256": canonical_json_digest(catalog),
        "catalogStatus": entry["status"] if entry is not None else "absent",
        "producer": {
            "project": project_id,
            "repository": producer_repository,
            "checkpoint": source["checkpoint"],
            "process": _process_identity(project_root),
        },
        "decision": decision,
        "ownerBoundary": owner_boundary,
        "reusableClass": reusable_class,
        "canonicalInvariantId": invariant_id,
        "recurrence": recurrence,
        "linkedChangeId": linked_change_id,
        "rationaleSha256": rationale_sha256,
        "exception": exception,
        "requiredProof": {
            "producerLifecycle": shared_proof,
            "immutableRelease": shared_proof,
            "consumerReproduction": shared_proof,
        },
        "controls": {
            "grantsImplementation": False,
            "grantsMerge": False,
            "grantsRelease": False,
            "grantsAdoption": False,
        },
    }
    disposition_digest = write_improvement_artifact(
        disposition, output, validate_improvement_disposition
    )
    validate_improvement_chain(
        signal_path,
        output,
        catalog_path=catalog_path,
    )
    return {
        "signalId": signal["signalId"],
        "dispositionSha256": disposition_digest,
        "decision": decision,
        "recurrence": recurrence,
        "invariantId": invariant_id,
        "linkedChangeId": linked_change_id,
        "output": str(output.resolve()),
    }


def create_improvement_resolution(
    project_root: Path,
    signal_path: Path,
    disposition_path: Path,
    catalog_path: Path,
    lifecycle_receipt_path: Path,
    release_contract_path: Path,
    release_receipt_path: Path | None,
    release_authorization_path: Path | None,
    artifact_root: Path,
    artifact_attestation_path: Path,
    *,
    release_repository: str,
    release_tag: str,
    release_name: str,
    release_commit: str,
    regression_evidence: list[str],
    output: Path,
) -> dict[str, Any]:
    from .evidence import validate_receipt
    from .artifact_attestation import validate_distribution_attestation
    from .release import validate_release_checkpoint

    signal = _load(
        signal_path, validate_improvement_signal, "improvement signal"
    )
    disposition = _load(
        disposition_path,
        validate_improvement_disposition,
        "improvement disposition",
    )
    if disposition["decision"] not in {"accepted", "duplicate"}:
        raise ContractError("rejected improvement signal cannot be resolved")
    if not all(disposition["requiredProof"].values()):
        raise ContractError(
            "improvement resolution requires the complete shared proof contract"
        )
    validate_improvement_chain(
        signal_path,
        disposition_path,
        catalog_path=catalog_path,
    )
    receipt_document, receipt_bytes_before = _stable_json_document(
        lifecycle_receipt_path,
        label="producer improvement lifecycle receipt",
        limit=8_000_000,
    )
    receipt = validate_receipt(lifecycle_receipt_path)
    _receipt_document_after, receipt_bytes_after = _stable_json_document(
        lifecycle_receipt_path,
        label="producer improvement lifecycle receipt",
        limit=8_000_000,
    )
    if receipt_bytes_after != receipt_bytes_before:
        raise ContractError(
            "producer improvement lifecycle receipt changed during validation"
        )
    producer = disposition["producer"]
    if (
        receipt["project"] != producer["project"]
        or receipt["changeId"] != disposition["linkedChangeId"]
    ):
        raise ContractError(
            "improvement lifecycle receipt does not match producer disposition"
        )
    receipt_artifacts = receipt_document.get("artifacts")
    completion_entry = (
        receipt_artifacts.get("completion")
        if isinstance(receipt_artifacts, dict)
        else None
    )
    completion_text = (
        completion_entry.get("sourceText")
        if isinstance(completion_entry, dict)
        else None
    )
    try:
        completion_document = json.loads(completion_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise ContractError(
            "producer lifecycle receipt lacks a valid completion artifact"
        ) from error
    improvements = completion_document.get("improvements")
    expected_signal_digest = canonical_json_digest(signal)
    expected_disposition_digest = canonical_json_digest(disposition)
    matching_cases = [
        case
        for case in improvements
        if isinstance(improvements, list)
        and isinstance(case, dict)
        and case.get("role") == "producer"
        and case.get("phase") == "producer-completed"
        and case.get("invariantId") == disposition["canonicalInvariantId"]
        and case.get("signalCanonicalSha256") == expected_signal_digest
        and case.get("catalogCanonicalSha256") == disposition["catalogSha256"]
        and case.get("dispositionCanonicalSha256")
        == expected_disposition_digest
    ] if isinstance(improvements, list) else []
    if len(matching_cases) != 1:
        raise ContractError(
            "producer lifecycle receipt does not contain the reviewed ingested improvement case"
        )
    release_document = read_json(release_contract_path)
    release_contract = validate_release(
        release_document, str(release_contract_path)
    )
    if release_contract.version == release_contract.previous_version:
        raise ContractError("improvement resolution requires a new immutable release")
    if release_contract_path.resolve(strict=True) != (
        project_root.resolve(strict=True) / "release.json"
    ):
        raise ContractError(
            "improvement resolution must use the producer root release contract"
        )
    release_validation = validate_release_checkpoint(
        project_root.resolve(strict=True),
        tag=release_tag,
        release_name=release_name,
        commit=release_commit,
        main_ref="origin/main",
        receipt_path=release_receipt_path,
        authorization_path=release_authorization_path,
        reviewed_commit=None,
    )
    if release_validation.get("version") not in {None, release_contract.version}:
        raise ContractError(
            "validated immutable release does not match the release contract version"
        )
    _attestation_document, attestation_bytes_before = _stable_json_document(
        artifact_attestation_path,
        label="producer distribution attestation",
        limit=256_000,
    )
    validate_distribution_attestation(
        project_root,
        artifact_root,
        artifact_attestation_path,
        receipt_path=release_receipt_path,
        authorization_path=release_authorization_path,
        checkpoint=release_commit,
    )
    _attestation_after, attestation_bytes_after = _stable_json_document(
        artifact_attestation_path,
        label="producer distribution attestation",
        limit=256_000,
    )
    if attestation_bytes_after != attestation_bytes_before:
        raise ContractError(
            "producer distribution attestation changed during validation"
        )
    artifact_set_sha256 = "sha256:" + hashlib.sha256(
        attestation_bytes_before
    ).hexdigest()
    digests = sorted(set(regression_evidence))
    if not digests:
        raise ContractError("improvement resolution requires regression evidence")
    receipt_sha256 = "sha256:" + hashlib.sha256(receipt_bytes_before).hexdigest()
    resolution = {
        "schemaVersion": 1,
        "kind": "engineering-process-improvement-resolution",
        "resolvedAt": _timestamp(),
        "signalSha256": canonical_json_digest(signal),
        "dispositionSha256": canonical_json_digest(disposition),
        "canonicalInvariantId": disposition["canonicalInvariantId"],
        "producerLifecycle": {
            "project": receipt["project"],
            "changeId": receipt["changeId"],
            "checkpoint": receipt["checkpoint"],
            "receiptSha256": receipt_sha256,
        },
        "release": {
            "repository": release_repository,
            "version": release_contract.version,
            "tag": release_tag,
            "releaseName": release_name,
            "commit": release_commit,
            "artifactSetSha256": artifact_set_sha256,
        },
        "regressionEvidence": digests,
    }
    digest = write_improvement_artifact(
        resolution, output, validate_improvement_resolution
    )
    validate_improvement_chain(
        signal_path,
        disposition_path,
        output,
        catalog_path=catalog_path,
    )
    return {
        "signalId": signal["signalId"],
        "resolutionSha256": digest,
        "invariantId": disposition["canonicalInvariantId"],
        "releaseVersion": release_contract.version,
        "releaseCommit": release_commit,
        "output": str(output.resolve()),
        "phase": "producer-released",
        "nextOwner": signal["source"]["project"],
    }


def create_improvement_reproduction(
    project_root: Path,
    signal_path: Path,
    disposition_path: Path,
    catalog_path: Path,
    resolution_path: Path,
    lifecycle_receipt_path: Path,
    *,
    consumer_repository: str,
    reference: str | None,
    output: Path,
) -> dict[str, Any]:
    from .runner import source_state
    from .evidence import validate_receipt

    project_root = project_root.resolve(strict=True)
    chain = validate_improvement_chain(
        signal_path,
        disposition_path,
        resolution_path,
        catalog_path=catalog_path,
    )
    signal = _load(
        signal_path, validate_improvement_signal, "improvement signal"
    )
    disposition = _load(
        disposition_path,
        validate_improvement_disposition,
        "improvement disposition",
    )
    resolution = _load(
        resolution_path, validate_improvement_resolution, "improvement resolution"
    )
    project_document = read_json(project_root / ".process" / "project.json")
    if not isinstance(project_document, dict) or not isinstance(
        project_document.get("project"), str
    ):
        raise ContractError("consumer project manifest lacks a project id")
    expected_source = signal["source"]
    if (
        project_document["project"] != expected_source["project"]
        or consumer_repository != expected_source["repository"]
    ):
        raise ContractError(
            "improvement reproduction consumer does not match signal source"
        )
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
    ):
        raise ContractError(
            "improvement reproduction requires a clean immutable consumer checkpoint"
        )
    process = _process_identity(project_root)
    release = resolution["release"]
    if process["version"] != release["version"]:
        raise ContractError(
            "consumer has not adopted the immutable resolved process release"
        )
    receipt = validate_receipt(lifecycle_receipt_path)
    if (
        receipt["project"] != project_document["project"]
        or receipt["checkpoint"] != source["checkpoint"]
        or receipt["workspaceFingerprint"] != source["fingerprint"]
        or receipt["processVersion"] != process["version"]
        or receipt["processDigest"] != process["digest"]
    ):
        raise ContractError(
            "consumer lifecycle receipt does not match the adopted reproduction checkpoint"
        )
    receipt_document, evidence_data = _stable_json_document(
        lifecycle_receipt_path,
        label="consumer reproduction lifecycle receipt",
        limit=8_000_000,
    )
    artifacts = receipt_document.get("artifacts")
    verification = (
        artifacts.get("verification") if isinstance(artifacts, dict) else None
    )
    if not isinstance(verification, list) or not verification:
        raise ContractError(
            "consumer lifecycle receipt lacks verification profiles"
        )
    profiles = sorted(
        {
            item["profile"]
            for item in verification
            if isinstance(item, dict) and isinstance(item.get("profile"), str)
        }
    )
    if len(profiles) != len(verification):
        raise ContractError(
            "consumer lifecycle receipt verification profiles are invalid"
        )
    evidence_bytes = len(evidence_data)
    evidence_sha256 = "sha256:" + hashlib.sha256(evidence_data).hexdigest()
    reproduction = {
        "schemaVersion": 1,
        "kind": "engineering-process-improvement-reproduction",
        "completedAt": _timestamp(),
        "signalSha256": canonical_json_digest(signal),
        "dispositionSha256": canonical_json_digest(disposition),
        "resolutionSha256": canonical_json_digest(resolution),
        "canonicalInvariantId": disposition["canonicalInvariantId"],
        "consumer": {
            "project": project_document["project"],
            "repository": consumer_repository,
            "checkpoint": source["checkpoint"],
            "workspaceFingerprint": source["fingerprint"],
            "process": process,
        },
        "release": {
            "repository": release["repository"],
            "version": release["version"],
            "tag": release["tag"],
            "releaseName": release["releaseName"],
            "commit": release["commit"],
            "artifactSetSha256": release["artifactSetSha256"],
        },
        "evidence": {
            "kind": "lifecycle-receipt",
            "status": "passed",
            "artifactSha256": evidence_sha256,
            "artifactBytes": evidence_bytes,
            "changeId": receipt["changeId"],
            "cycle": receipt["cycle"],
            "profiles": profiles,
            "reference": reference,
        },
    }
    digest = write_improvement_artifact(
        reproduction, output, validate_improvement_reproduction
    )
    closed = validate_improvement_chain(
        signal_path,
        disposition_path,
        resolution_path,
        output,
        catalog_path,
    )
    return {
        **chain,
        "reproductionSha256": digest,
        "output": str(output.resolve()),
        "phase": closed["phase"],
        "closed": closed["closed"],
        "nextOwner": closed["nextOwner"],
    }


def attach_improvement_chain(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    signal_path: Path,
    disposition_path: Path | None,
    resolution_path: Path | None,
    reproduction_path: Path | None,
    catalog_path: Path | None,
    actor_id: str,
    context_id: str,
    actor_kind: str,
) -> dict[str, Any]:
    from .lifecycle import bind_improvement_chain

    chain = validate_improvement_chain(
        signal_path,
        disposition_path,
        resolution_path,
        reproduction_path,
        catalog_path,
    )
    disposition = (
        _load(
            disposition_path,
            validate_improvement_disposition,
            "improvement disposition",
        )
        if disposition_path is not None
        else None
    )
    expected_digests = {"signal": chain["signalSha256"]}
    if disposition is not None:
        expected_digests.update(
            {
                "catalog": disposition["catalogSha256"],
                "disposition": chain["dispositionSha256"],
            }
        )
    if resolution_path is not None:
        expected_digests["resolution"] = chain["resolutionSha256"]
    if reproduction_path is not None:
        expected_digests["reproduction"] = chain["reproductionSha256"]
    state = bind_improvement_chain(
        project_root,
        change_id,
        case_id,
        signal_path=signal_path,
        catalog_path=catalog_path,
        disposition_path=disposition_path,
        resolution_path=resolution_path,
        reproduction_path=reproduction_path,
        expected_canonical_digests=expected_digests,
        chain_phase=chain["phase"],
        actor_id=actor_id,
        context_id=context_id,
        kind=actor_kind,
    )
    case = next(
        item for item in state["improvements"] if item["id"] == case_id
    )
    return {**chain, "lifecyclePhase": state["phase"], "casePhase": case["phase"]}


def ingest_improvement_signal(
    project_root: Path,
    change_id: str,
    *,
    signal_path: Path,
    disposition_path: Path,
    catalog_path: Path,
    actor_id: str,
    context_id: str,
    actor_kind: str,
) -> dict[str, Any]:
    from .lifecycle import register_producer_improvement_case

    chain = validate_improvement_chain(
        signal_path,
        disposition_path,
        catalog_path=catalog_path,
    )
    signal = _load(
        signal_path, validate_improvement_signal, "improvement signal"
    )
    disposition = _load(
        disposition_path,
        validate_improvement_disposition,
        "improvement disposition",
    )
    if disposition["decision"] not in {"accepted", "duplicate"}:
        raise ContractError("only accepted or duplicate signals enter producer work")
    if disposition["linkedChangeId"] != change_id:
        raise ContractError(
            "improvement disposition does not link the selected producer change"
        )
    state = register_producer_improvement_case(
        project_root,
        change_id,
        signal_path=signal_path,
        catalog_path=catalog_path,
        disposition_path=disposition_path,
        expected_canonical_digests={
            "signal": chain["signalSha256"],
            "catalog": disposition["catalogSha256"],
            "disposition": chain["dispositionSha256"],
        },
        signal_id=signal["signalId"],
        canonical_invariant_id=disposition["canonicalInvariantId"],
        owner_boundary=disposition["ownerBoundary"],
        reusable_class=disposition["reusableClass"],
        rationale_sha256=disposition["rationaleSha256"],
        actor_id=actor_id,
        context_id=context_id,
        kind=actor_kind,
    )
    case = state["improvements"][-1]
    return {
        **chain,
        "changeId": change_id,
        "caseId": case["id"],
        "casePhase": case["phase"],
    }
