"""Consumer-owned project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ProcessError, read_json, validate_document
from .distribution import schemas_root


PROJECT_SCHEMA_VERSION = 1
DEFAULT_RETENTION = {
    "maxCompletedReceipts": 256,
    "maxCompletedReceiptBytes": 64 * 1024 * 1024,
}
PACK_CAPABILITIES = {
    ("desktop-media", 1): set("application-correctness authoritative-input-integrity cross-platform-portability dependency-audit dependency-security incident-recovery independent-security-review key-custody linux-release-security media-pipeline-integrity package-security recovery-integrity recovery-mechanism-integrity release-integrity runtime-delivery-integrity update-integrity workspace-security".split()),
    ("library-cli", 1): set("adoption-integrity compatibility correctness distribution-integrity installability portability runtime-safety".split()),
    ("operations", 1): set("auditability automation-correctness bounded-execution least-privilege policy-integrity recovery target-selection-integrity".split()),
}


def project_path(project_root: Path) -> Path:
    return project_root / ".process" / "project.json"


def load_project(project_root: Path, process_root: Path) -> dict[str, Any]:
    project = normalize_project(read_json(project_path(project_root)), process_root)
    path = project_root / ".process" / "readiness.json"
    if not path.is_file():
        return project
    if "readiness" in project:
        raise ProcessError("readiness must use either project.json or readiness.json, not both")
    project["readiness"] = read_json(path)
    validate_document(project, "project", schema_root=schemas_root(process_root), source=str(path))
    readiness_summary(project)
    return project


def normalize_project(value: Any, process_root: Path) -> dict[str, Any]:
    """Validate the single current consumer project contract."""
    validate_document(
        value,
        "project",
        schema_root=schemas_root(process_root),
        source="project configuration",
    )
    normalized = value

    _validate_impact_profiles(normalized)
    missing = sorted(set(required_profiles(normalized)) - set(normalized["profiles"]))
    if missing:
        raise ProcessError("project requires unknown profiles: " + ", ".join(missing))
    readiness_summary(normalized)
    return normalized


def readiness_summary(project: dict[str, Any]) -> dict[str, Any] | None:
    readiness = project.get("readiness")
    if readiness is None:
        return None
    entries = readiness["capabilities"]
    capabilities = {item["id"]: item for item in entries}
    if len(capabilities) != len(entries):
        raise ProcessError("readiness capability ids must be unique")
    packs = [(item["id"], item["version"]) for item in readiness["packs"]]
    unsupported = [f"{name}@{version}" for name, version in packs if (name, version) not in PACK_CAPABILITIES]
    if unsupported:
        raise ProcessError("unsupported readiness pack versions: " + ", ".join(unsupported))
    required = set().union(*(PACK_CAPABILITIES[pack] for pack in packs))
    missing = sorted(required - capabilities.keys())
    if missing:
        raise ProcessError("readiness packs require missing capabilities: " + ", ".join(missing))
    available = set(project["profiles"])
    mandatory_profiles = set(required_profiles(project))
    coverage: dict[str, Any] = {}
    planned: list[str] = []
    for capability_id, item in sorted(capabilities.items()):
        if item["state"] == "planned":
            coverage[capability_id] = {"state": "planned", "gap": item["gap"]}
            planned.append(capability_id)
            continue
        evidence = set(item["evidenceProfiles"])
        unknown = sorted(evidence - available)
        if unknown:
            raise ProcessError(f"readiness capability {capability_id} references unknown profiles: " + ", ".join(unknown))
        if evidence.isdisjoint(mandatory_profiles):
            raise ProcessError(f"readiness capability {capability_id} relies only on optional profiles: " + ", ".join(sorted(evidence)))
        coverage[capability_id] = {"state": "enforced", "evidence": {
            name: [check["id"] for check in project["profiles"][name]] for name in item["evidenceProfiles"]
        }}
    if readiness["stage"] == "production" and planned:
        raise ProcessError("production readiness cannot contain planned capabilities: " + ", ".join(planned))
    return {"target": readiness["target"], "stage": readiness["stage"], "packs": readiness["packs"], "capabilities": coverage, "plannedCapabilities": planned}


def required_profiles(project: dict[str, Any]) -> tuple[str, ...]:
    return tuple(project["lifecycle"]["requiredProfiles"])


def impact_profiles(project: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return the validated consumer impact profile map, or an empty map."""
    policy = project.get("impactProfiles")
    if policy is None:
        return {}
    return policy["profiles"]


def impact_assurance_profiles(project: dict[str, Any]) -> tuple[str, ...]:
    """Return profiles explicitly allowed to satisfy final verification."""
    policy = project.get("impactProfiles")
    if not isinstance(policy, dict):
        return ()
    return tuple(policy.get("finalProfiles", ()))


def _validate_impact_profiles(project: dict[str, Any]) -> None:
    """Validate semantic references inside the consumer-owned impact policy."""
    policy = project.get("impactProfiles")
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ProcessError("impactProfiles must be an object")
    impact_profiles = policy.get("profiles")
    if not isinstance(impact_profiles, dict):
        raise ProcessError("impactProfiles.profiles must be an object")
    configured = set(project["profiles"])
    for profile, checks in impact_profiles.items():
        if profile not in configured:
            raise ProcessError(
                f"impactProfiles references unknown project profile: {profile}"
            )
        identifiers = [check["id"] for check in checks]
        if len(identifiers) != len(set(identifiers)):
            raise ProcessError(
                f"impact profile {profile} contains duplicate unit ids"
            )
    final_profiles = tuple(policy.get("finalProfiles", ()))
    if len(final_profiles) != len(set(final_profiles)):
        raise ProcessError("impact finalProfiles must be unique")
    unknown_final = sorted(set(final_profiles) - set(project["lifecycle"]["requiredProfiles"]))
    if unknown_final:
        raise ProcessError(
            "impact finalProfiles must be required lifecycle profiles: "
            + ", ".join(unknown_final)
        )
    missing_final = sorted(set(final_profiles) - set(impact_profiles))
    if missing_final:
        raise ProcessError(
            "impact finalProfiles reference profiles without impact units: "
            + ", ".join(missing_final)
        )
    for profile in final_profiles:
        if not any(unit.get("scope", "matched") == "global" for unit in impact_profiles[profile]):
            raise ProcessError(
                f"impact final profile {profile} requires an explicit global unit"
            )


def require_consumer_evidence(project: dict[str, Any]) -> bool:
    policy = project["lifecycle"].get("processChanges", {})
    return bool(policy.get("requireConsumerEvidence", False))


def publication_required(project: dict[str, Any]) -> bool:
    policy = project["lifecycle"].get("publication", {})
    return bool(policy.get("required", False))


def retention_policy(project: dict[str, Any]) -> dict[str, int]:
    """Return the consumer-owned bounds for retained completion receipts."""
    configured = project.get("lifecycle", {}).get("retention", {})
    return {
        name: int(configured.get(name, default))
        for name, default in DEFAULT_RETENTION.items()
    }


def accepted_issue_url_prefix(project: dict[str, Any]) -> str | None:
    return project["lifecycle"].get("processChanges", {}).get(
        "acceptedIssueUrlPrefix"
    )
