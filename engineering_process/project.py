"""Consumer-owned project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ProcessError, read_json, validate_document
from .distribution import schemas_root


PROJECT_SCHEMA_VERSION = 5
PACK_CAPABILITIES = {
    "library-cli": set("adoption-integrity compatibility correctness distribution-integrity installability portability runtime-safety".split()),
    "operations": set("auditability automation-correctness bounded-execution least-privilege policy-integrity recovery target-selection-integrity".split()),
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
    """Convert released pre-1.0 manifests to the small 1.0 contract."""
    version = value.get("schemaVersion") if isinstance(value, dict) else None
    if version == PROJECT_SCHEMA_VERSION:
        validate_document(
            value,
            "project",
            schema_root=schemas_root(process_root),
            source="project configuration",
        )
        normalized = value
    elif version in {1, 2, 3, 4}:
        validate_document(
            value,
            "project-legacy",
            schema_root=schemas_root(process_root),
            source="legacy project configuration",
        )
        lifecycle = value["lifecycle"]
        profiles = value["profiles"]
        required = lifecycle["requiredProfiles"]
        normalized_profiles: dict[str, list[dict[str, Any]]] = {}
        for profile_name, checks in profiles.items():
            normalized_checks: list[dict[str, Any]] = []
            for check in checks:
                normalized_check = {
                    key: check[key]
                    for key in ("id", "run", "timeoutSeconds", "maxOutputBytes", "cwd")
                    if key in check
                }
                normalized_checks.append(normalized_check)
            normalized_profiles[profile_name] = normalized_checks
        setup_actions = value.get("environment", {}).get("setupActions", [])
        normalized_setup: list[dict[str, Any]] = []
        for action in setup_actions:
            if "run" not in action:
                if action.get("kind") == "managed-tool" and action.get("tool"):
                    continue
                raise ProcessError(
                    "legacy setup action is neither a managed tool nor a command"
                )
            normalized_setup.append(
                {
                    key: action[key]
                    for key in (
                        "id",
                        "run",
                        "timeoutSeconds",
                        "maxOutputBytes",
                        "cwd",
                    )
                    if key in action
                }
            )
        normalized = {
            "schemaVersion": PROJECT_SCHEMA_VERSION,
            "project": value["project"],
            "lifecycle": {
                "requiredProfiles": required,
                **(
                    {"processChanges": {"requireConsumerEvidence": True}}
                    if value["project"] == "engineering-process"
                    else {}
                ),
            },
            "profiles": normalized_profiles,
            **({"setup": normalized_setup} if normalized_setup else {}),
        }
    else:
        validate_document(
            value,
            "project",
            schema_root=schemas_root(process_root),
            source="project configuration",
        )
        raise AssertionError("project schema accepted an unsupported version")

    validate_document(
        normalized,
        "project",
        schema_root=schemas_root(process_root),
        source="normalized project configuration",
    )
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
    required = set().union(*(PACK_CAPABILITIES[pack] for pack in readiness["packs"]))
    missing = sorted(required - capabilities.keys())
    if missing:
        raise ProcessError("readiness packs require missing capabilities: " + ", ".join(missing))
    available = set(project["profiles"])
    mandatory_profiles = set(required_profiles(project))
    coverage: dict[str, Any] = {}
    for capability_id, item in sorted(capabilities.items()):
        evidence = set(item["evidenceProfiles"])
        unknown = sorted(evidence - available)
        if unknown:
            raise ProcessError(f"readiness capability {capability_id} references unknown profiles: " + ", ".join(unknown))
        optional = sorted(evidence - mandatory_profiles)
        if optional:
            raise ProcessError(f"readiness capability {capability_id} relies on optional profiles: " + ", ".join(optional))
        coverage[capability_id] = {
            name: [check["id"] for check in project["profiles"][name]]
            for name in item["evidenceProfiles"]
        }
    return {"target": readiness["target"], "packs": readiness["packs"], "capabilities": coverage}


def required_profiles(project: dict[str, Any]) -> tuple[str, ...]:
    return tuple(project["lifecycle"]["requiredProfiles"])


def require_consumer_evidence(project: dict[str, Any]) -> bool:
    policy = project["lifecycle"].get("processChanges", {})
    return bool(policy.get("requireConsumerEvidence", False))
