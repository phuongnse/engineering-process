from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid
from typing import Any, Callable

from .contracts import (
    ContractError,
    MAX_JSON_BYTES,
    RECOMMENDATION_RESOLUTION_CONTROLS,
    canonical_json_digest,
    validate_recommendation,
    validate_recommendation_resolution,
    validate_recommendation_review,
)


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
    recommendation_path: Path,
    review_path: Path,
    resolution_path: Path | None = None,
) -> dict[str, Any]:
    _chain_size((recommendation_path, review_path, resolution_path))
    recommendation = _stable_document(
        recommendation_path,
        label="recommendation",
        validator=validate_recommendation,
    )
    classifications = validate_recommendation(
        recommendation, str(recommendation_path)
    )
    review = _stable_document(
        review_path,
        label="recommendation review",
        validator=validate_recommendation_review,
    )
    recommendation_digest = canonical_json_digest(recommendation)
    if review["decisionId"] != recommendation["decisionId"]:
        raise ContractError("recommendation review decision id does not match")
    if review["recommendationSha256"] != recommendation_digest:
        raise ContractError(
            "recommendation review does not bind the canonical recommendation digest"
        )
    coordinator = recommendation["coordinator"]
    reviewer = review["reviewer"]
    if coordinator["actorId"] == reviewer["actorId"]:
        raise ContractError(
            "recommendation reviewer actor must differ from the coordinator"
        )
    if coordinator["contextId"] == reviewer["contextId"]:
        raise ContractError(
            "recommendation reviewer context must differ from the coordinator"
        )
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


def _write_new_artifact(document: dict[str, Any], output: Path) -> str:
    validate_recommendation_resolution(document, str(output))
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ContractError("recommendation resolution exceeds the JSON size limit")
    output = output.resolve()
    if output.exists():
        raise ContractError(f"{output}: refusing to replace a recommendation resolution")
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
        raise ContractError(
            f"{output}: cannot write recommendation resolution: {error}"
        ) from error
    return canonical_json_digest(document)


def create_recommendation_resolution(
    recommendation_path: Path,
    review_path: Path,
    *,
    selected_option_id: str,
    owner_id: str,
    owner_evidence_sha256: str,
    selection_rationale_sha256: str,
    output: Path,
) -> dict[str, Any]:
    chain = validate_recommendation_chain(recommendation_path, review_path)
    if not chain["allowed"]:
        raise ContractError("blocked recommendation has no selectable option")
    if selected_option_id not in chain["validOptionIds"]:
        raise ContractError("owner resolution must select a derived valid option")
    document = {
        "schemaVersion": 1,
        "kind": "engineering-process-recommendation-resolution",
        "decisionId": chain["decisionId"],
        "recommendationSha256": chain["recommendationSha256"],
        "reviewSha256": chain["reviewSha256"],
        "selectedOptionId": selected_option_id,
        "owner": {
            "ownerId": owner_id,
            "evidenceSha256": owner_evidence_sha256,
        },
        "selectionRationaleSha256": selection_rationale_sha256,
        "controls": dict(RECOMMENDATION_RESOLUTION_CONTROLS),
    }
    digest = _write_new_artifact(document, output)
    return {
        "decisionId": chain["decisionId"],
        "selectedOptionId": selected_option_id,
        "resolutionSha256": digest,
        "output": str(output.resolve()),
        "phase": "resolved",
    }
