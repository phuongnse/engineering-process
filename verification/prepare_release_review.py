from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (
    ContractError,
    read_json,
    validate_change,
    validate_review,
)
from engineering_process.lifecycle import load_state

TRUSTED_VERIFIER_REPOSITORY = "phuongnse/renovate-ops"
TRUSTED_VERIFIER_SHA = "f22b05f7813d5868f2a728f203a59afa5d6f18d2"


def approved_review_from_assignment(
    assignment: dict[str, object],
    independent_evidence: dict[str, object],
    contract: dict[str, object] | None = None,
) -> dict[str, object]:
    required = {
        "changeId",
        "cycle",
        "checkpoint",
        "workspaceFingerprint",
        "comparisonBase",
        "reviewer",
        "independence",
    }
    missing = sorted(required - set(assignment))
    if missing:
        raise ContractError(
            "release review assignment is missing: " + ", ".join(missing)
        )
    expected_evidence = {
        "status": "passed",
        "governanceMode": "single-maintainer",
        "verificationKind": "independent-automated",
        "repository": "phuongnse/engineering-process",
        "headSha": assignment["checkpoint"],
        "verifierRepository": TRUSTED_VERIFIER_REPOSITORY,
        "verifierSha": TRUSTED_VERIFIER_SHA,
    }
    for key, expected in expected_evidence.items():
        if independent_evidence.get(key) != expected:
            raise ContractError(
                f"independent verification evidence has invalid {key}"
            )
    schema_version = 2
    quality: dict[str, object] | None = None
    if contract is not None:
        validate_change(contract, "registered release contract")
        if contract["id"] != assignment["changeId"]:
            raise ContractError(
                "registered release contract does not match the review assignment"
            )
        schema_version = int(contract["schemaVersion"])
        if schema_version == 3:
            assessments = []
            for accepted in contract["quality"]["assessments"]:
                dimension = accepted["dimension"]
                applicable = accepted["status"] == "applicable"
                assessments.append(
                    {
                        "dimension": dimension,
                        "status": (
                            "verified" if applicable else "not-applicable-confirmed"
                        ),
                        "criteria": accepted["criteria"],
                        "evidence": (
                            "The independent verifier passed the exact assigned "
                            f"checkpoint; review confirms the {dimension} acceptance "
                            "criteria."
                            if applicable
                            else "Independent review confirms the contract's "
                            f"{dimension} not-applicable rationale: "
                            f"{accepted['rationale']}"
                        ),
                    }
                )
            quality = {
                "standard": contract["quality"]["standard"],
                "assessments": assessments,
            }
    report = {
        "schemaVersion": schema_version,
        "changeId": assignment["changeId"],
        "cycle": assignment["cycle"],
        "checkpoint": assignment["checkpoint"],
        "workspaceFingerprint": assignment["workspaceFingerprint"],
        "comparisonBase": assignment["comparisonBase"],
        "reviewer": assignment["reviewer"],
        "independence": assignment["independence"],
        "verdict": "approved",
        "findings": [],
    }
    if quality is not None:
        report["quality"] = quality
    validate_review(report, "generated release review")
    return report


def _assignment_contract(
    project_root: Path, assignment: dict[str, object]
) -> dict[str, object]:
    reference = assignment.get("contract")
    if not isinstance(reference, dict):
        raise ContractError("release review assignment contract is missing")
    if set(reference) != {"digest", "path"}:
        raise ContractError("release review assignment contract reference is invalid")
    relative_path = reference.get("path")
    expected_digest = reference.get("digest")
    if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
        raise ContractError("release review assignment contract reference is invalid")
    root = project_root.resolve(strict=True)
    try:
        contract_path = (root / relative_path).resolve(strict=True)
        contract_path.relative_to(root)
        contract_bytes = contract_path.read_bytes()
    except (OSError, ValueError) as error:
        raise ContractError(
            f"cannot read registered release contract: {error}"
        ) from error
    actual_digest = f"sha256:{hashlib.sha256(contract_bytes).hexdigest()}"
    if actual_digest != expected_digest:
        raise ContractError("registered release contract digest does not match assignment")
    contract = read_json(contract_path)
    if not isinstance(contract, dict):
        raise ContractError("registered release contract must be an object")
    validate_change(contract, "registered release contract")
    return contract


def prepare_review(
    project_root: Path,
    change_id: str,
    independent_evidence_path: Path,
    output: Path,
) -> dict[str, object]:
    state = load_state(project_root.resolve(strict=True), change_id)
    if state["phase"] != "review-pending" or state["reviewAssignment"] is None:
        raise ContractError("release candidate must be review-pending")
    assignment_path = project_root / state["reviewAssignment"]["path"]
    assignment = read_json(assignment_path)
    if not isinstance(assignment, dict):
        raise ContractError("release review assignment must be an object")
    independent_evidence = read_json(independent_evidence_path)
    if not isinstance(independent_evidence, dict):
        raise ContractError("independent verification evidence must be an object")
    contract = _assignment_contract(project_root, assignment)
    report = approved_review_from_assignment(
        assignment, independent_evidence, contract
    )
    if output.exists():
        raise ContractError(f"{output}: refusing to replace existing review report")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ContractError(f"cannot write release review report: {error}") from error
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--independent-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = prepare_review(
            arguments.project_root,
            arguments.change_id,
            arguments.independent_evidence,
            arguments.output,
        )
    except ContractError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
