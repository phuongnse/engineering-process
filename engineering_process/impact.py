"""Strict, consumer-owned change-impact resolution for fast feedback."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable

from .contracts import ProcessError, digest_json, validate_document
from .distribution import schemas_root
from .project import impact_assurance_profiles, impact_profiles
from .repository import changed_paths


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def resolve_impact_selection(
    project_root: Path,
    process_root: Path,
    project: dict[str, Any],
    state: dict[str, Any],
    *,
    profiles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Resolve every changed path to explicit consumer verification units.

    This function is intentionally fail-closed. It never converts an unknown
    path into a full profile request.
    """
    required = tuple(state["contract"]["document"]["requiredProfiles"])
    requested = tuple(required if profiles is None else profiles)
    invalid = sorted(set(requested) - set(required))
    if invalid:
        raise ProcessError(
            "impact selection may request only accepted required profiles: "
            + ", ".join(invalid)
        )

    base = state.get("comparisonBaseCommit")
    if not isinstance(base, str) or not base:
        raise ProcessError("impact selection requires a pinned comparison base")
    paths = changed_paths(project_root, base)
    policy = impact_profiles(project)
    assurance_profiles = tuple(impact_assurance_profiles(project))
    policy_digest = digest_json(project.get("impactProfiles", {}))
    selected: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []

    for profile in requested:
        units = policy.get(profile) if isinstance(policy, dict) else None
        if not isinstance(units, list) or not units:
            requirements.append(
                {
                    "profile": profile,
                    "status": "unavailable",
                    "selectedUnits": [],
                    "unresolvedPaths": [],
                    "reason": "the consumer has not declared an impact policy for this profile",
                }
            )
            unresolved.extend({"profile": profile, "path": path} for path in paths)
            continue

        matched: dict[str, set[str]] = {unit["id"]: set() for unit in units}
        missing: list[str] = []
        for path in paths:
            matches = [unit for unit in units if _matches(path, unit["paths"])]
            if not matches:
                missing.append(path)
                unresolved.append({"profile": profile, "path": path})
                continue
            for unit in matches:
                matched[unit["id"]].add(path)

        # Only an explicit universal pattern can subsume narrower units. A
        # partial global pattern remains subject to the ordinary unresolved-
        # path boundary.
        global_ids = {
            unit["id"]
            for unit in units
            if (
                unit.get("scope", "matched") == "global"
                and "**" in unit["paths"]
                and matched[unit["id"]]
            )
        }

        selected_for_profile = [
            {
                "profile": profile,
                "id": unit["id"],
                "matchedPaths": sorted(matched[unit["id"]]),
            }
            for unit in units
            if matched[unit["id"]]
            and (not global_ids or unit["id"] in global_ids)
        ]
        selected.extend(selected_for_profile)
        requirements.append(
            {
                "profile": profile,
                "status": "unresolved" if missing else "ready",
                "selectedUnits": selected_for_profile,
                "unresolvedPaths": sorted(missing),
                **(
                    {
                        "reason": "one or more changed paths have no declared verification unit"
                    }
                    if missing
                    else {}
                ),
            }
        )

    unresolved = sorted(
        unresolved,
        key=lambda item: (item["profile"], item["path"]),
    )
    status = "ready"
    if unresolved or not paths:
        status = "unresolved" if policy else "unavailable"
    selection: dict[str, Any] = {
        "schemaVersion": 1,
        "changeId": state["changeId"],
        "status": status,
        "comparisonBaseCommit": base,
        "policyDigest": policy_digest,
        "assuranceProfiles": [
            profile for profile in requested if profile in assurance_profiles
        ],
        "changedPaths": list(paths),
        "requirements": requirements,
        "selectedUnits": selected,
        "unresolvedPaths": unresolved,
    }
    if status != "ready":
        if not paths:
            reason = "the candidate has no changed path from the pinned comparison base"
        elif not policy:
            reason = "the consumer has not declared a current impact policy"
        else:
            reason = "every requested profile must resolve every changed path before affected execution"
        selection["resolution"] = {
            "action": "inspect-diff-and-update-impact-policy",
            "reason": reason,
            "paths": list(paths),
        }
    return validate_document(
        selection,
        "verification-impact-selection",
        schema_root=schemas_root(process_root),
        source="verification impact selection",
    )


def unavailable_impact_selection(
    project_root: Path,
    process_root: Path,
    state: dict[str, Any],
    reason: str,
    *,
    profiles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an actionable unavailable result when policy loading fails."""
    required = tuple(state["contract"]["document"]["requiredProfiles"])
    requested = tuple(required if profiles is None else profiles)
    invalid = sorted(set(requested) - set(required))
    if invalid:
        raise ProcessError(
            "impact selection may request only accepted required profiles: "
            + ", ".join(invalid)
        )
    paths = changed_paths(project_root, state["comparisonBaseCommit"])
    unresolved = [
        {"profile": profile, "path": path}
        for profile in requested
        for path in paths
    ]
    selection = {
        "schemaVersion": 1,
        "changeId": state["changeId"],
        "status": "unavailable",
        "comparisonBaseCommit": state["comparisonBaseCommit"],
        "changedPaths": list(paths),
        "requirements": [
            {
                "profile": profile,
                "status": "unavailable",
                "selectedUnits": [],
                "unresolvedPaths": list(paths),
                "reason": "the consumer impact policy could not be loaded",
            }
            for profile in requested
        ],
        "selectedUnits": [],
        "unresolvedPaths": unresolved,
        "resolution": {
            "action": "inspect-diff-and-update-impact-policy",
            "reason": f"the consumer impact policy is invalid or unsupported: {reason}"[:2000],
            "paths": list(paths),
        },
    }
    return validate_document(
        selection,
        "verification-impact-selection",
        schema_root=schemas_root(process_root),
        source="verification impact selection",
    )


def impact_unit_lookup(
    project: dict[str, Any], selection: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve selected output ids back to the consumer-owned command objects."""
    policies = impact_profiles(project)
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for profile, units in policies.items():
        for unit in units:
            lookup[(profile, unit["id"])] = unit
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for selected in selection["selectedUnits"]:
        key = (selected["profile"], selected["id"])
        if key not in lookup:
            raise ProcessError(
                f"impact policy changed while resolving unit {key[0]}:{key[1]}"
            )
        result[key] = lookup[key]
    return result
