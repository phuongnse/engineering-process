"""Canonical production-engineering invariant assessments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ProcessError, load_and_validate
from .distribution import schemas_root, skills_root


PLAN_SCHEMA_VERSION = 5
REVIEW_SCHEMA_VERSION = 7


def load_invariant_floor(process_root: Path) -> dict[str, Any]:
    path = skills_root(process_root) / "production-engineering" / "invariants.json"
    floor = load_and_validate(
        path,
        "production-engineering",
        schema_root=schemas_root(process_root),
    )
    identifiers = [item["id"] for item in floor["invariants"]]
    if len(identifiers) != len(set(identifiers)):
        raise ProcessError(f"{path}: invariant ids must be unique")
    return floor


def _canonical_ids(process_root: Path) -> list[str]:
    return [item["id"] for item in load_invariant_floor(process_root)["invariants"]]


def _require_canonical_assessments(
    document: dict[str, Any],
    process_root: Path,
    *,
    source: str,
) -> list[dict[str, Any]]:
    assessments = document["productionEngineering"]
    expected = _canonical_ids(process_root)
    actual = [item["id"] for item in assessments]
    if actual != expected:
        raise ProcessError(
            f"{source}: productionEngineering must assess the canonical invariants "
            "once and in order: " + ", ".join(expected)
        )
    return assessments


def validate_plan_assessments(plan: dict[str, Any], process_root: Path) -> None:
    if plan["schemaVersion"] != PLAN_SCHEMA_VERSION:
        return
    assessments = _require_canonical_assessments(
        plan,
        process_root,
        source="plan",
    )
    work_items = {item["id"] for item in plan["workItems"]}
    for assessment in assessments:
        missing = sorted(set(assessment["evidenceWorkItems"]) - work_items)
        if missing:
            raise ProcessError(
                f"plan: invariant {assessment['id']} references unknown work items: "
                + ", ".join(missing)
            )


def validate_review_assessments(review: dict[str, Any], process_root: Path) -> None:
    if review["schemaVersion"] != REVIEW_SCHEMA_VERSION:
        return
    assessments = _require_canonical_assessments(
        review,
        process_root,
        source="review",
    )
    findings = {finding["id"]: finding for finding in review["findings"]}
    linked: list[str] = []
    for assessment in assessments:
        if assessment["status"] != "violated":
            continue
        finding_id = assessment["findingId"]
        linked.append(finding_id)
        finding = findings.get(finding_id)
        if finding is None or finding["severity"] != "blocking":
            raise ProcessError(
                f"review: violated invariant {assessment['id']} must link to a blocking finding"
            )
        if finding["origin"] not in {
            "production-invariant",
            "remediation-regression",
            "critical-late",
        }:
            raise ProcessError(
                f"review: finding {finding_id} does not record an invariant-compatible origin"
            )
    if len(linked) != len(set(linked)):
        raise ProcessError("review: each violated invariant must link to a distinct finding")
    unlinked = sorted(
        finding_id
        for finding_id, finding in findings.items()
        if finding["origin"] == "production-invariant" and finding_id not in linked
    )
    if unlinked:
        raise ProcessError(
            "review: production-invariant findings must be linked from an invariant assessment: "
            + ", ".join(unlinked)
        )
    if review["verdict"] == "approved" and linked:
        raise ProcessError("review: approval cannot contain a violated production invariant")
