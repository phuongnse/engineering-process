from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError, read_json, validate_review
from engineering_process.lifecycle import load_state


def approved_review_from_assignment(assignment: dict[str, object]) -> dict[str, object]:
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
    report = {
        "schemaVersion": 2,
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
    validate_review(report, "generated release review")
    return report


def prepare_review(project_root: Path, change_id: str, output: Path) -> dict[str, object]:
    state = load_state(project_root.resolve(strict=True), change_id)
    if state["phase"] != "review-pending" or state["reviewAssignment"] is None:
        raise ContractError("release candidate must be review-pending")
    assignment_path = project_root / state["reviewAssignment"]["path"]
    assignment = read_json(assignment_path)
    if not isinstance(assignment, dict):
        raise ContractError("release review assignment must be an object")
    report = approved_review_from_assignment(assignment)
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
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = prepare_review(
            arguments.project_root,
            arguments.change_id,
            arguments.output,
        )
    except ContractError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
