from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from .contracts import (
    ContractError,
    MAX_JSON_BYTES,
    RECOMMENDATION_RESOLUTION_CONTROLS,
    canonical_json_digest,
    validate_recommendation,
    validate_recommendation_resolution,
    validate_recommendation_review,
    validate_recommendation_review_assignment,
)
from .lifecycle import reserve_review_context, review_context_reservation


MAX_RECOMMENDATION_CHAIN_BYTES = 3_000_000


def _stable_document(
    path: Path,
    *,
    label: str,
    validator: Callable[[Any, str], Any],
) -> dict[str, Any]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label}: must be a regular non-symlink file")
        if before.st_size > MAX_JSON_BYTES:
            raise ContractError(f"{label}: exceeds {MAX_JSON_BYTES} bytes")
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
            while len(content) <= MAX_JSON_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, MAX_JSON_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
        finally:
            os.close(descriptor)
        after = path.lstat()
    except ContractError:
        raise
    except OSError as error:
        raise ContractError(f"{label}: cannot read {path}: {error}") from error
    data = bytes(content)
    if len(data) > MAX_JSON_BYTES:
        raise ContractError(f"{label}: exceeds {MAX_JSON_BYTES} bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractError(f"{label}: UTF-8 BOM is not allowed")
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
    validator(document, str(path))
    return document


def _chain_size(paths: tuple[Path | None, ...]) -> None:
    total = 0
    for path in paths:
        if path is None:
            continue
        try:
            total += path.lstat().st_size
        except OSError as error:
            raise ContractError(
                f"cannot inspect recommendation artifact {path}: {error}"
            ) from error
        if total > MAX_RECOMMENDATION_CHAIN_BYTES:
            raise ContractError(
                "recommendation artifact chain exceeds "
                f"{MAX_RECOMMENDATION_CHAIN_BYTES} bytes"
            )


def validate_recommendation_chain(
    project_root: Path,
    recommendation_path: Path,
    assignment_path: Path,
    review_path: Path,
    resolution_path: Path | None = None,
) -> dict[str, Any]:
    _chain_size(
        (recommendation_path, assignment_path, review_path, resolution_path)
    )
    recommendation = _stable_document(
        recommendation_path,
        label="recommendation",
        validator=validate_recommendation,
    )
    classifications = validate_recommendation(
        recommendation, str(recommendation_path)
    )
    assignment = _stable_document(
        assignment_path,
        label="recommendation review assignment",
        validator=validate_recommendation_review_assignment,
    )
    review = _stable_document(
        review_path,
        label="recommendation review",
        validator=validate_recommendation_review,
    )
    recommendation_digest = canonical_json_digest(recommendation)
    if assignment["decisionId"] != recommendation["decisionId"]:
        raise ContractError("recommendation assignment decision id does not match")
    if assignment["recommendationSha256"] != recommendation_digest:
        raise ContractError(
            "recommendation assignment does not bind the canonical recommendation digest"
        )
    if assignment["coordinator"] != recommendation["coordinator"]:
        raise ContractError(
            "recommendation assignment coordinator does not match recommendation"
        )
    reservation = review_context_reservation(
        project_root, assignment["reviewer"]["contextId"]
    )
    if (
        canonical_json_digest(reservation)
        != assignment["contextReservationSha256"]
    ):
        raise ContractError(
            "recommendation assignment does not bind the project context reservation"
        )
    if (
        reservation["actorId"] != assignment["reviewer"]["actorId"]
        or reservation["kind"] != assignment["reviewer"]["kind"]
        or reservation["changeId"]
        != f"recommendation-{recommendation['decisionId']}"
        or reservation["cycle"] != 1
    ):
        raise ContractError(
            "recommendation assignment reviewer does not match context reservation"
        )
    if review["decisionId"] != recommendation["decisionId"]:
        raise ContractError("recommendation review decision id does not match")
    if review["recommendationSha256"] != recommendation_digest:
        raise ContractError(
            "recommendation review does not bind the canonical recommendation digest"
        )
    assignment_digest = canonical_json_digest(assignment)
    if review["assignmentSha256"] != assignment_digest:
        raise ContractError(
            "recommendation review does not bind the canonical assignment digest"
        )
    if review["reviewer"] != assignment["reviewer"]:
        raise ContractError("recommendation review actor does not match assignment")
    expected_invariants = [item["id"] for item in recommendation["invariants"]]
    reviewed_invariants = [
        item["invariantId"] for item in review["invariantAssessments"]
    ]
    if reviewed_invariants != expected_invariants:
        raise ContractError(
            "recommendation review must assess every governing invariant exactly once"
        )
    expected_options = [item["id"] for item in recommendation["options"]]
    reviewed_options = [item["optionId"] for item in review["optionAssessments"]]
    if reviewed_options != expected_options:
        raise ContractError(
            "recommendation review must assess every option exactly once"
        )
    if review["verdict"] != "approved":
        raise ContractError(
            "recommendation chain requires an approved independent challenge"
        )

    valid_options = sorted(
        option_id
        for option_id, classification in classifications.items()
        if classification == "valid"
    )
    recommendation_state = recommendation["recommendation"]
    allowed = recommendation_state["status"] == "recommended"
    result: dict[str, Any] = {
        "decisionId": recommendation["decisionId"],
        "recommendationSha256": recommendation_digest,
        "assignmentSha256": assignment_digest,
        "reviewSha256": canonical_json_digest(review),
        "risk": recommendation["risk"],
        "validOptionIds": valid_options,
        "recommendedOptionId": recommendation_state["optionId"],
        "phase": "recommendation-approved" if allowed else "blocked",
        "allowed": allowed,
        "nextOwner": "project-owner" if allowed else "decision-owner",
    }
    if resolution_path is None:
        return result
    if not allowed:
        raise ContractError("a blocked recommendation cannot have an owner resolution")
    resolution = _stable_document(
        resolution_path,
        label="recommendation resolution",
        validator=validate_recommendation_resolution,
    )
    if resolution["decisionId"] != recommendation["decisionId"]:
        raise ContractError("recommendation resolution decision id does not match")
    if resolution["recommendationSha256"] != recommendation_digest:
        raise ContractError(
            "recommendation resolution does not bind the canonical recommendation digest"
        )
    if resolution["assignmentSha256"] != assignment_digest:
        raise ContractError(
            "recommendation resolution does not bind the canonical assignment digest"
        )
    review_digest = canonical_json_digest(review)
    if resolution["reviewSha256"] != review_digest:
        raise ContractError(
            "recommendation resolution does not bind the canonical review digest"
        )
    selected = resolution["selectedOptionId"]
    if classifications.get(selected) != "valid":
        raise ContractError(
            "recommendation resolution must select a derived valid option"
        )
    result.update(
        {
            "phase": "resolved",
            "resolutionSha256": canonical_json_digest(resolution),
            "selectedOptionId": selected,
            "nextOwner": None,
        }
    )
    return result


def _write_new_artifact(
    document: dict[str, Any],
    output: Path,
    *,
    label: str,
    validator: Callable[[Any, str], Any],
) -> tuple[str, Path]:
    validator(document, str(output))
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ContractError(f"{label} exceeds the JSON size limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as error:
        raise ContractError(f"{label} parent cannot be resolved: {error}") from error
    destination = parent / output.name
    try:
        parent_before = parent.stat()
    except OSError as error:
        raise ContractError(f"{parent}: cannot inspect {label} parent: {error}") from error
    if not stat.S_ISDIR(parent_before.st_mode):
        raise ContractError(f"{parent}: {label} parent must be a directory")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    opened: os.stat_result | None = None
    try:
        try:
            descriptor = os.open(destination, flags, 0o600)
        except FileExistsError as error:
            raise ContractError(f"{destination}: refusing to replace {label}") from error
        except OSError as error:
            raise ContractError(
                f"{destination}: cannot create exclusive {label}: {error}"
            ) from error
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ContractError(f"{destination}: {label} must be a regular file")
        with os.fdopen(descriptor, "w+b") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            parent_after = parent.stat()
            final = destination.lstat()
            if (
                parent_after.st_dev != parent_before.st_dev
                or parent_after.st_ino != parent_before.st_ino
                or stat.S_IFMT(parent_after.st_mode)
                != stat.S_IFMT(parent_before.st_mode)
            ):
                raise ContractError(f"{destination}: {label} parent identity changed")
            if (
                not stat.S_ISREG(final.st_mode)
                or final.st_dev != opened.st_dev
                or final.st_ino != opened.st_ino
            ):
                raise ContractError(f"{destination}: {label} final identity changed")
            stream.seek(0)
            if stream.read(len(data) + 1) != data:
                raise ContractError(f"{destination}: {label} final bytes changed")
            final_open = os.fstat(stream.fileno())
            if (
                final_open.st_dev != opened.st_dev
                or final_open.st_ino != opened.st_ino
                or final_open.st_size != len(data)
            ):
                raise ContractError(f"{destination}: {label} final metadata changed")
            if os.name == "posix":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_descriptor = os.open(parent, directory_flags)
                try:
                    directory = os.fstat(directory_descriptor)
                    if (
                        directory.st_dev != parent_before.st_dev
                        or directory.st_ino != parent_before.st_ino
                    ):
                        raise ContractError(
                            f"{destination}: {label} parent identity changed"
                        )
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
    except ContractError:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if opened is not None:
            try:
                current = destination.lstat()
                if (
                    current.st_dev == opened.st_dev
                    and current.st_ino == opened.st_ino
                ):
                    destination.unlink()
            except OSError:
                pass
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if opened is not None:
            try:
                current = destination.lstat()
                if (
                    current.st_dev == opened.st_dev
                    and current.st_ino == opened.st_ino
                ):
                    destination.unlink()
            except OSError:
                pass
        raise ContractError(f"{destination}: cannot write {label}: {error}") from error
    return canonical_json_digest(document), destination


def _actor(actor_id: str, context_id: str, kind: str) -> dict[str, str]:
    for value, label in ((actor_id, "actor id"), (context_id, "context id")):
        if not value or value != value.strip() or len(value) > 256:
            raise ContractError(f"recommendation reviewer {label} is invalid")
    if kind not in {"agent", "human"}:
        raise ContractError("recommendation reviewer kind must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def start_recommendation_review(
    project_root: Path,
    recommendation_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    method: str,
    attested_by: str,
    evidence: str,
) -> dict[str, Any]:
    recommendation = _stable_document(
        recommendation_path,
        label="recommendation",
        validator=validate_recommendation,
    )
    reviewer = _actor(actor_id, context_id, kind)
    coordinator = recommendation["coordinator"]
    if kind == "agent" and method != "isolated-context":
        raise ContractError("agent recommendation review requires isolated-context")
    if kind == "human" and method != "separate-person":
        raise ContractError("human recommendation review requires separate-person")
    participants = {
        coordinator["actorId"],
        coordinator["contextId"],
        reviewer["actorId"],
        reviewer["contextId"],
    }
    if not attested_by or attested_by != attested_by.strip() or len(attested_by) > 256:
        raise ContractError("recommendation review attester is invalid")
    if attested_by in participants:
        raise ContractError("recommendation review cannot be participant-attested")
    if not evidence or evidence != evidence.strip() or len(evidence) > 2000:
        raise ContractError("recommendation review independence evidence is invalid")
    if coordinator["actorId"] == reviewer["actorId"]:
        raise ContractError("recommendation reviewer actor must differ from coordinator")
    if coordinator["contextId"] == reviewer["contextId"]:
        raise ContractError("recommendation reviewer context must differ from coordinator")
    decision_id = recommendation["decisionId"]
    reservation = reserve_review_context(
        project_root,
        {"changeId": f"recommendation-{decision_id}", "cycle": 1},
        reviewer,
    )
    recommendation_digest = canonical_json_digest(recommendation)
    assignment = {
        "schemaVersion": 1,
        "kind": "engineering-process-recommendation-review-assignment",
        "decisionId": decision_id,
        "recommendationSha256": recommendation_digest,
        "coordinator": coordinator,
        "reviewer": reviewer,
        "independence": {
            "method": method,
            "attestedBy": attested_by,
            "evidence": evidence,
        },
        "contextReservationSha256": canonical_json_digest(reservation),
    }
    context_digest = reservation["contextDigest"].removeprefix("sha256:")
    assignment_path = (
        project_root
        / ".process"
        / "runs"
        / "recommendations"
        / decision_id
        / (
            "review-request-"
            f"{recommendation_digest.removeprefix('sha256:')[:16]}-"
            f"{context_digest[:16]}.json"
        )
    )
    assignment_digest, written = _write_new_artifact(
        assignment,
        assignment_path,
        label="recommendation review assignment",
        validator=validate_recommendation_review_assignment,
    )
    return {
        "decisionId": decision_id,
        "recommendationSha256": recommendation_digest,
        "assignmentSha256": assignment_digest,
        "assignment": str(written.relative_to(project_root.resolve())),
        "reviewer": reviewer,
        "phase": "review-pending",
    }


def create_recommendation_resolution(
    project_root: Path,
    recommendation_path: Path,
    assignment_path: Path,
    review_path: Path,
    *,
    selected_option_id: str,
    owner_id: str,
    owner_evidence_sha256: str,
    selection_rationale_sha256: str,
    output: Path,
) -> dict[str, Any]:
    chain = validate_recommendation_chain(
        project_root, recommendation_path, assignment_path, review_path
    )
    if not chain["allowed"]:
        raise ContractError("blocked recommendation has no selectable option")
    if selected_option_id not in chain["validOptionIds"]:
        raise ContractError("owner resolution must select a derived valid option")
    document = {
        "schemaVersion": 1,
        "kind": "engineering-process-recommendation-resolution",
        "decisionId": chain["decisionId"],
        "recommendationSha256": chain["recommendationSha256"],
        "assignmentSha256": chain["assignmentSha256"],
        "reviewSha256": chain["reviewSha256"],
        "selectedOptionId": selected_option_id,
        "owner": {
            "ownerId": owner_id,
            "evidenceSha256": owner_evidence_sha256,
        },
        "selectionRationaleSha256": selection_rationale_sha256,
        "controls": dict(RECOMMENDATION_RESOLUTION_CONTROLS),
    }
    digest, written = _write_new_artifact(
        document,
        output,
        label="recommendation resolution",
        validator=validate_recommendation_resolution,
    )
    return {
        "decisionId": chain["decisionId"],
        "selectedOptionId": selected_option_id,
        "resolutionSha256": digest,
        "output": str(written),
        "phase": "resolved",
    }
