"""Consumer-owned project configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ProcessError, read_json, validate_document
from .distribution import schemas_root


PROJECT_SCHEMA_VERSION = 5


def project_path(project_root: Path) -> Path:
    return project_root / ".process" / "project.json"


def load_project(project_root: Path, process_root: Path) -> dict[str, Any]:
    return normalize_project(read_json(project_path(project_root)), process_root)


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
    return normalized


def required_profiles(project: dict[str, Any]) -> tuple[str, ...]:
    return tuple(project["lifecycle"]["requiredProfiles"])


def require_consumer_evidence(project: dict[str, Any]) -> bool:
    policy = project["lifecycle"].get("processChanges", {})
    return bool(policy.get("requireConsumerEvidence", False))
