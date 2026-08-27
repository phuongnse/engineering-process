from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .contracts import (
    ContractError,
    DIGEST_PATTERN,
    FINAL_SEMVER_PATTERN,
    NAME_PATTERN,
    PROFILE_PATTERN,
    REPOSITORY_PATTERN,
    _exact_keys,
    _object,
    _string,
    _string_list,
    canonical_json_digest,
    read_json,
    validate_process_lock,
)
from .bounded_process import run_bounded_process
from .distribution import distribution_digest
from .git import run_git
from .runner import source_state


REQUEST_KIND = "engineering-process-authority-transition-request"
EVIDENCE_KIND = "engineering-process-authority-transition-evidence"
BOOTSTRAP_INTENT_KIND = "engineering-process-bootstrap-adoption-intent"
BOOTSTRAP_CONSUMPTION_KIND = "engineering-process-bootstrap-adoption-consumption"
POLICY_KIND = "engineering-process-protected-transition-policy"

MAX_TRANSITION_PATHS = 512
MAX_TRANSITION_ARTIFACTS = 16
MATERIALIZATION_TIMEOUT_SECONDS = 300
MATERIALIZATION_OUTPUT_BYTES = 128_000


def _digest(value: Any, path: str) -> str:
    digest = _string(value, path, max_length=71)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(f"{path}: must be a lowercase SHA-256 digest")
    return digest


def _git_oid(value: Any, path: str) -> str:
    oid = _string(value, path, max_length=40)
    if re.fullmatch(r"[0-9a-f]{40}", oid) is None:
        raise ContractError(f"{path}: must be a full lowercase Git commit")
    return oid


def _version(value: Any, path: str) -> str:
    version = _string(value, path, max_length=64)
    if FINAL_SEMVER_PATTERN.fullmatch(version) is None:
        raise ContractError(f"{path}: must be final SemVer X.Y.Z")
    return version


def _identifier(value: Any, path: str) -> str:
    identifier = _string(value, path, max_length=64)
    if PROFILE_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}: invalid identifier")
    return identifier


def _repository(value: Any, path: str) -> str:
    repository = _string(value, path, max_length=256)
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ContractError(f"{path}: invalid repository")
    return repository


def _timestamp(value: Any, path: str) -> str:
    text = _string(value, path, max_length=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"{path}: must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ContractError(f"{path}: must include a timezone")
    return text


def _actor(value: Any, path: str) -> dict[str, str]:
    actor = _object(value, path)
    _exact_keys(actor, required={"actorId", "contextId", "kind"}, path=path)
    actor_id = _string(actor["actorId"], f"{path}.actorId", max_length=256)
    context_id = _string(
        actor["contextId"], f"{path}.contextId", max_length=256
    )
    if actor["kind"] not in {"agent", "human"}:
        raise ContractError(f"{path}.kind: must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": actor["kind"]}


def _authority(value: Any, path: str) -> dict[str, str]:
    authority = _object(value, path)
    _exact_keys(authority, required={"version", "digest"}, path=path)
    return {
        "version": _version(authority["version"], f"{path}.version"),
        "digest": _digest(authority["digest"], f"{path}.digest"),
    }


def _relative_path(value: Any, path: str) -> str:
    relative = _string(value, path, max_length=512)
    candidate = PurePosixPath(relative)
    if (
        "\\" in relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != relative
        or relative.endswith("/")
    ):
        raise ContractError(f"{path}: must be a portable contained file path")
    return relative


def _artifact_bindings(value: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_TRANSITION_ARTIFACTS:
        raise ContractError(
            f"{path}: must contain 2 to {MAX_TRANSITION_ARTIFACTS} artifacts"
        )
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _object(raw, item_path)
        _exact_keys(
            item, required={"name", "sha256", "sizeBytes"}, path=item_path
        )
        name = _string(item["name"], f"{item_path}.name", max_length=200)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
            raise ContractError(f"{item_path}.name: invalid artifact name")
        size = item["sizeBytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1_000_000_000:
            raise ContractError(f"{item_path}.sizeBytes: invalid bounded size")
        result.append(
            {
                "name": name,
                "sha256": _digest(item["sha256"], f"{item_path}.sha256"),
                "sizeBytes": size,
            }
        )
    if len(result) != len({json.dumps(item, sort_keys=True) for item in result}):
        raise ContractError(f"{path}: must contain unique artifacts")
    return result


def _action_pin_changes(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 256:
        raise ContractError(f"{path}: must contain at most 256 action-pin changes")
    result: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _object(raw, item_path)
        _exact_keys(
            item,
            required={
                "path",
                "repository",
                "previousCommit",
                "targetCommit",
                "previousReleaseTag",
                "targetReleaseTag",
            },
            path=item_path,
        )
        relative = _relative_path(item["path"], f"{item_path}.path")
        if (
            PurePosixPath(relative).parent != PurePosixPath(".github/workflows")
            or not relative.endswith((".yml", ".yaml"))
        ):
            raise ContractError(f"{item_path}.path: must name a workflow file")
        repository = _repository(item["repository"], f"{item_path}.repository")
        previous = _git_oid(item["previousCommit"], f"{item_path}.previousCommit")
        target = _git_oid(item["targetCommit"], f"{item_path}.targetCommit")
        previous_tag = _string(
            item["previousReleaseTag"],
            f"{item_path}.previousReleaseTag",
            max_length=80,
        )
        target_tag = _string(
            item["targetReleaseTag"],
            f"{item_path}.targetReleaseTag",
            max_length=80,
        )
        if re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", previous_tag) is None or re.fullmatch(
            r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            target_tag,
        ) is None:
            raise ContractError(f"{item_path}: action release tags must be final SemVer tags")
        result.append(
            {
                "path": relative,
                "repository": repository,
                "previousCommit": previous,
                "targetCommit": target,
                "previousReleaseTag": previous_tag,
                "targetReleaseTag": target_tag,
            }
        )
    if len(result) != len({json.dumps(item, sort_keys=True) for item in result}):
        raise ContractError(f"{path}: must contain unique action-pin entries")
    return result


def validate_authority_transition_request(
    document: Any, path: str = "authority-transition-request"
) -> dict[str, Any]:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "kind",
            "project",
            "changeId",
            "cycle",
            "source",
            "target",
            "candidate",
            "registeredBy",
            "registeredAt",
        },
        path=path,
    )
    if value["schemaVersion"] != 1 or value["kind"] != REQUEST_KIND:
        raise ContractError(f"{path}: invalid schemaVersion or kind")
    project = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(project) is None:
        raise ContractError(f"{path}.project: invalid project")
    change_id = _identifier(value["changeId"], f"{path}.changeId")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")

    source_path = f"{path}.source"
    source = _object(value["source"], source_path)
    _exact_keys(
        source,
        required={
            "authority",
            "checkpoint",
            "workspaceFingerprint",
            "processLockSha256",
            "requirementsLockSha256",
        },
        path=source_path,
    )
    source_authority = _authority(source["authority"], f"{source_path}.authority")

    target_path = f"{path}.target"
    target = _object(value["target"], target_path)
    _exact_keys(
        target,
        required={
            "repository",
            "version",
            "tag",
            "commit",
            "processDigest",
            "releaseContractSha256",
            "distributionAttestationSha256",
            "lifecycleReceiptSha256",
            "artifacts",
        },
        path=target_path,
    )
    target_version = _version(target["version"], f"{target_path}.version")

    candidate_path = f"{path}.candidate"
    candidate = _object(value["candidate"], candidate_path)
    _exact_keys(
        candidate,
        required={
            "baseCheckpoint",
            "selectedSkills",
            "projectMigrationSha256",
            "requirementsInputSha256",
            "requirementsLockSha256",
            "projectManifestSha256",
            "actionPinsSha256",
            "actionPins",
            "expectedChangedPaths",
            "expiresAt",
        },
        path=candidate_path,
    )
    selected_skills = _string_list(
        candidate["selectedSkills"],
        f"{candidate_path}.selectedSkills",
        pattern=PROFILE_PATTERN,
        maximum=256,
    )
    if not selected_skills or len(selected_skills) != len(set(selected_skills)):
        raise ContractError(
            f"{candidate_path}.selectedSkills: must be non-empty and unique"
        )
    changed_paths = [
        _relative_path(item, f"{candidate_path}.expectedChangedPaths[{index}]")
        for index, item in enumerate(candidate["expectedChangedPaths"])
    ] if isinstance(candidate["expectedChangedPaths"], list) else []
    if (
        not changed_paths
        or len(changed_paths) > MAX_TRANSITION_PATHS
        or len(changed_paths) != len(set(changed_paths))
    ):
        raise ContractError(
            f"{candidate_path}.expectedChangedPaths: must be non-empty, unique, and bounded"
        )
    migration_digest = candidate["projectMigrationSha256"]
    if migration_digest is not None:
        _digest(migration_digest, f"{candidate_path}.projectMigrationSha256")

    action_pins = _action_pin_changes(
        candidate["actionPins"], f"{candidate_path}.actionPins"
    )
    return {
        "schemaVersion": 1,
        "kind": REQUEST_KIND,
        "project": project,
        "changeId": change_id,
        "cycle": cycle,
        "source": {
            "authority": source_authority,
            "checkpoint": _git_oid(source["checkpoint"], f"{source_path}.checkpoint"),
            "workspaceFingerprint": _digest(
                source["workspaceFingerprint"], f"{source_path}.workspaceFingerprint"
            ),
            "processLockSha256": _digest(
                source["processLockSha256"], f"{source_path}.processLockSha256"
            ),
            "requirementsLockSha256": _digest(
                source["requirementsLockSha256"],
                f"{source_path}.requirementsLockSha256",
            ),
        },
        "target": {
            "repository": _repository(target["repository"], f"{target_path}.repository"),
            "version": target_version,
            "tag": target["tag"],
            "commit": _git_oid(target["commit"], f"{target_path}.commit"),
            "processDigest": _digest(
                target["processDigest"], f"{target_path}.processDigest"
            ),
            "releaseContractSha256": _digest(
                target["releaseContractSha256"],
                f"{target_path}.releaseContractSha256",
            ),
            "distributionAttestationSha256": _digest(
                target["distributionAttestationSha256"],
                f"{target_path}.distributionAttestationSha256",
            ),
            "lifecycleReceiptSha256": _digest(
                target["lifecycleReceiptSha256"],
                f"{target_path}.lifecycleReceiptSha256",
            ),
            "artifacts": _artifact_bindings(target["artifacts"], f"{target_path}.artifacts"),
        },
        "candidate": {
            "baseCheckpoint": _git_oid(
                candidate["baseCheckpoint"], f"{candidate_path}.baseCheckpoint"
            ),
            "selectedSkills": selected_skills,
            "projectMigrationSha256": migration_digest,
            "requirementsInputSha256": _digest(
                candidate["requirementsInputSha256"],
                f"{candidate_path}.requirementsInputSha256",
            ),
            "requirementsLockSha256": _digest(
                candidate["requirementsLockSha256"],
                f"{candidate_path}.requirementsLockSha256",
            ),
            "projectManifestSha256": _digest(
                candidate["projectManifestSha256"],
                f"{candidate_path}.projectManifestSha256",
            ),
            "actionPinsSha256": _digest(
                candidate["actionPinsSha256"],
                f"{candidate_path}.actionPinsSha256",
            ),
            "actionPins": action_pins,
            "expectedChangedPaths": changed_paths,
            "expiresAt": _timestamp(candidate["expiresAt"], f"{candidate_path}.expiresAt"),
        },
        "registeredBy": _actor(value["registeredBy"], f"{path}.registeredBy"),
        "registeredAt": _timestamp(value["registeredAt"], f"{path}.registeredAt"),
    }


def _require_candidate_semantics(
    candidate: dict[str, Any],
    artifacts: list[dict[str, Any]],
    *,
    path: str,
    artifacts_path: str,
) -> None:
    artifact_names = [item["name"] for item in artifacts]
    if artifact_names != sorted(set(artifact_names)):
        raise ContractError(f"{artifacts_path}: must be sorted by name and unique")
    selected = candidate["selectedSkills"]
    if selected != sorted(selected):
        raise ContractError(f"{path}.selectedSkills: must use canonical order")
    changed = candidate["expectedChangedPaths"]
    if changed != sorted(changed):
        raise ContractError(f"{path}.expectedChangedPaths: must use canonical order")
    action_pins = candidate["actionPins"]
    identities: list[tuple[str, str]] = []
    for index, item in enumerate(action_pins):
        item_path = f"{path}.actionPins[{index}]"
        if (
            PurePosixPath(item["path"]).parent
            != PurePosixPath(".github/workflows")
            or not item["path"].endswith((".yml", ".yaml"))
        ):
            raise ContractError(f"{item_path}.path: must name a workflow file")
        if item["previousCommit"] == item["targetCommit"]:
            raise ContractError(f"{item_path}: action pin must change commit")
        identities.append((item["path"], item["repository"]))
    if len(identities) != len(set(identities)) or identities != sorted(identities):
        raise ContractError(
            f"{path}.actionPins: must use unique canonical path/repository order"
        )


def require_authority_transition_request_semantics(
    request: dict[str, Any], *, path: str = "authority-transition-request"
) -> None:
    source = request["source"]
    target = request["target"]
    if target["tag"] != f"v{target['version']}":
        raise ContractError(f"{path}.target.tag: must be v{target['version']}")
    if target["version"] == source["authority"]["version"]:
        raise ContractError(
            f"{path}.target.version: must differ from source authority"
        )
    if target["processDigest"] == source["authority"]["digest"]:
        raise ContractError(
            f"{path}.target.processDigest: must differ from source authority"
        )
    if target["commit"] == source["checkpoint"]:
        raise ContractError(f"{path}.target.commit: must differ from source checkpoint")
    _require_candidate_semantics(
        request["candidate"],
        target["artifacts"],
        path=f"{path}.candidate",
        artifacts_path=f"{path}.target.artifacts",
    )


def validate_authority_transition_evidence(
    document: Any, path: str = "authority-transition-evidence"
) -> dict[str, Any]:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "kind",
            "project",
            "changeId",
            "cycle",
            "requestSha256",
            "sourceAuthority",
            "targetAuthority",
            "candidate",
            "bindings",
            "materialization",
            "generatedBy",
            "generatedAt",
        },
        path=path,
    )
    if value["schemaVersion"] != 1 or value["kind"] != EVIDENCE_KIND:
        raise ContractError(f"{path}: invalid schemaVersion or kind")
    project = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(project) is None:
        raise ContractError(f"{path}.project: invalid project")
    change_id = _identifier(value["changeId"], f"{path}.changeId")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ContractError(f"{path}.cycle: must be positive")
    source_authority = _authority(value["sourceAuthority"], f"{path}.sourceAuthority")
    target_authority = _authority(value["targetAuthority"], f"{path}.targetAuthority")

    candidate_path = f"{path}.candidate"
    candidate = _object(value["candidate"], candidate_path)
    _exact_keys(
        candidate,
        required={
            "baseCheckpoint",
            "checkpoint",
            "tree",
            "workspaceFingerprint",
            "workingTreeDirty",
            "changedPaths",
        },
        path=candidate_path,
    )
    if candidate["workingTreeDirty"] is not False:
        raise ContractError(f"{candidate_path}.workingTreeDirty: must be false")
    changed_paths = [
        _relative_path(item, f"{candidate_path}.changedPaths[{index}]")
        for index, item in enumerate(candidate["changedPaths"])
    ] if isinstance(candidate["changedPaths"], list) else []
    if len(changed_paths) != len(set(changed_paths)) or len(changed_paths) > MAX_TRANSITION_PATHS:
        raise ContractError(f"{candidate_path}.changedPaths: must be unique and bounded")

    bindings_path = f"{path}.bindings"
    bindings = _object(value["bindings"], bindings_path)
    binding_fields = {
        "requirementsInputSha256",
        "requirementsLockSha256",
        "processLockSha256",
        "projectManifestSha256",
        "projectMigrationSha256",
        "managedDistributionSha256",
        "actionPinsSha256",
    }
    _exact_keys(bindings, required=binding_fields, path=bindings_path)
    validated_bindings: dict[str, str | None] = {}
    for field in sorted(binding_fields):
        item = bindings[field]
        if field == "projectMigrationSha256" and item is None:
            validated_bindings[field] = None
        else:
            validated_bindings[field] = _digest(item, f"{bindings_path}.{field}")

    materialization_path = f"{path}.materialization"
    materialization = _object(value["materialization"], materialization_path)
    _exact_keys(
        materialization,
        required={
            "status",
            "applyTree",
            "idempotentTree",
            "checkRuns",
            "rollback",
            "issues",
        },
        path=materialization_path,
    )
    issues = materialization["issues"]
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise ContractError(f"{materialization_path}.issues: must be an array of strings")
    rollback_path = f"{materialization_path}.rollback"
    rollback = _object(materialization["rollback"], rollback_path)
    _exact_keys(
        rollback,
        required={"status", "beforeTree", "afterTree", "probe"},
        path=rollback_path,
    )
    rollback_before = _git_oid(
        rollback["beforeTree"], f"{rollback_path}.beforeTree"
    )
    rollback_after = _git_oid(
        rollback["afterTree"], f"{rollback_path}.afterTree"
    )
    if (
        materialization["status"] != "passed"
        or materialization["checkRuns"] != 2
        or rollback.get("status") != "restored"
        or rollback.get("probe") != "after-authority-write"
        or issues
    ):
        raise ContractError(
            f"{materialization_path}: requires observed apply/check/idempotence and rollback"
        )
    apply_tree = _git_oid(
        materialization["applyTree"], f"{materialization_path}.applyTree"
    )
    idempotent_tree = _git_oid(
        materialization["idempotentTree"],
        f"{materialization_path}.idempotentTree",
    )
    candidate_tree = _git_oid(
        candidate["tree"], f"{candidate_path}.tree"
    )

    return {
        "schemaVersion": 1,
        "kind": EVIDENCE_KIND,
        "project": project,
        "changeId": change_id,
        "cycle": cycle,
        "requestSha256": _digest(value["requestSha256"], f"{path}.requestSha256"),
        "sourceAuthority": source_authority,
        "targetAuthority": target_authority,
        "candidate": {
            "baseCheckpoint": _git_oid(candidate["baseCheckpoint"], f"{candidate_path}.baseCheckpoint"),
            "checkpoint": _git_oid(candidate["checkpoint"], f"{candidate_path}.checkpoint"),
            "tree": candidate_tree,
            "workspaceFingerprint": _digest(candidate["workspaceFingerprint"], f"{candidate_path}.workspaceFingerprint"),
            "workingTreeDirty": False,
            "changedPaths": changed_paths,
        },
        "bindings": validated_bindings,
        "materialization": {
            "status": "passed",
            "applyTree": apply_tree,
            "idempotentTree": idempotent_tree,
            "checkRuns": 2,
            "rollback": {
                "status": "restored",
                "beforeTree": rollback_before,
                "afterTree": rollback_after,
                "probe": "after-authority-write",
            },
            "issues": [],
        },
        "generatedBy": _actor(value["generatedBy"], f"{path}.generatedBy"),
        "generatedAt": _timestamp(value["generatedAt"], f"{path}.generatedAt"),
    }


def require_authority_transition_evidence_semantics(
    evidence: dict[str, Any], *, path: str = "authority-transition-evidence"
) -> None:
    candidate = evidence["candidate"]
    materialization = evidence["materialization"]
    rollback = materialization["rollback"]
    if candidate["changedPaths"] != sorted(candidate["changedPaths"]):
        raise ContractError(f"{path}.candidate.changedPaths: must use canonical order")
    if evidence["sourceAuthority"] == evidence["targetAuthority"]:
        raise ContractError(f"{path}: source and target authorities must differ")
    if candidate["baseCheckpoint"] == candidate["checkpoint"]:
        raise ContractError(f"{path}.candidate: checkpoint must advance from base")
    if (
        materialization["applyTree"] != candidate["tree"]
        or materialization["idempotentTree"] != candidate["tree"]
    ):
        raise ContractError(
            f"{path}.materialization: apply/idempotent trees must equal candidate tree"
        )
    if rollback["beforeTree"] != rollback["afterTree"]:
        raise ContractError(
            f"{path}.materialization.rollback: input tree was not restored"
        )


def _bootstrap_common(value: dict[str, Any], path: str, *, kind: str) -> dict[str, Any]:
    if value["schemaVersion"] != 1 or value["kind"] != kind:
        raise ContractError(f"{path}: invalid schemaVersion or kind")
    project = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(project) is None:
        raise ContractError(f"{path}.project: invalid project")
    return {"project": project, "repository": _repository(value["repository"], f"{path}.repository")}


def validate_bootstrap_adoption_intent(
    document: Any, path: str = "bootstrap-adoption-intent"
) -> dict[str, Any]:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion", "kind", "project", "repository", "changeId",
            "sourceAuthority", "targetRelease", "candidate", "policySha256",
            "ownerResolutionSha256", "expiresAt"
        },
        path=path,
    )
    common = _bootstrap_common(value, path, kind=BOOTSTRAP_INTENT_KIND)
    target_path = f"{path}.targetRelease"
    target = _object(value["targetRelease"], target_path)
    _exact_keys(
        target,
        required={"version", "tag", "commit", "processDigest", "releaseContractSha256", "distributionAttestationSha256", "artifacts"},
        path=target_path,
    )
    target_version = _version(target["version"], f"{target_path}.version")
    candidate_path = f"{path}.candidate"
    candidate = _object(value["candidate"], candidate_path)
    _exact_keys(
        candidate,
        required={"requirementsInputSha256", "requirementsLockSha256", "projectManifestSha256", "projectMigrationSha256", "actionPinsSha256", "actionPins", "selectedSkills", "expectedChangedPaths"},
        path=candidate_path,
    )
    selected = _string_list(candidate["selectedSkills"], f"{candidate_path}.selectedSkills", pattern=PROFILE_PATTERN, maximum=256)
    if not selected or len(selected) != len(set(selected)):
        raise ContractError(f"{candidate_path}.selectedSkills: must be non-empty and unique")
    changed = [_relative_path(item, f"{candidate_path}.expectedChangedPaths[{index}]") for index, item in enumerate(candidate["expectedChangedPaths"])] if isinstance(candidate["expectedChangedPaths"], list) else []
    if not changed or len(changed) != len(set(changed)) or len(changed) > MAX_TRANSITION_PATHS:
        raise ContractError(f"{candidate_path}.expectedChangedPaths: invalid")
    migration = candidate["projectMigrationSha256"]
    if migration is not None:
        _digest(migration, f"{candidate_path}.projectMigrationSha256")
    return {
        "schemaVersion": 1,
        "kind": BOOTSTRAP_INTENT_KIND,
        **common,
        "changeId": _identifier(value["changeId"], f"{path}.changeId"),
        "sourceAuthority": _authority(value["sourceAuthority"], f"{path}.sourceAuthority"),
        "targetRelease": {
            "version": target_version,
            "tag": target["tag"],
            "commit": _git_oid(target["commit"], f"{target_path}.commit"),
            "processDigest": _digest(target["processDigest"], f"{target_path}.processDigest"),
            "releaseContractSha256": _digest(target["releaseContractSha256"], f"{target_path}.releaseContractSha256"),
            "distributionAttestationSha256": _digest(target["distributionAttestationSha256"], f"{target_path}.distributionAttestationSha256"),
            "artifacts": _artifact_bindings(target["artifacts"], f"{target_path}.artifacts"),
        },
        "candidate": {
            "requirementsInputSha256": _digest(candidate["requirementsInputSha256"], f"{candidate_path}.requirementsInputSha256"),
            "requirementsLockSha256": _digest(candidate["requirementsLockSha256"], f"{candidate_path}.requirementsLockSha256"),
            "projectManifestSha256": _digest(candidate["projectManifestSha256"], f"{candidate_path}.projectManifestSha256"),
            "projectMigrationSha256": migration,
            "actionPinsSha256": _digest(candidate["actionPinsSha256"], f"{candidate_path}.actionPinsSha256"),
            "actionPins": _action_pin_changes(
                candidate["actionPins"], f"{candidate_path}.actionPins"
            ),
            "selectedSkills": selected,
            "expectedChangedPaths": changed,
        },
        "policySha256": _digest(value["policySha256"], f"{path}.policySha256"),
        "ownerResolutionSha256": _digest(value["ownerResolutionSha256"], f"{path}.ownerResolutionSha256"),
        "expiresAt": _timestamp(value["expiresAt"], f"{path}.expiresAt"),
    }


def require_bootstrap_adoption_intent_semantics(
    intent: dict[str, Any], *, path: str = "bootstrap-adoption-intent"
) -> None:
    target = intent["targetRelease"]
    if target["tag"] != f"v{target['version']}":
        raise ContractError(f"{path}.targetRelease.tag: must be v{target['version']}")
    if target["version"] == intent["sourceAuthority"]["version"]:
        raise ContractError(
            f"{path}.targetRelease.version: must differ from source authority"
        )
    if target["processDigest"] == intent["sourceAuthority"]["digest"]:
        raise ContractError(
            f"{path}.targetRelease.processDigest: must differ from source authority"
        )
    _require_candidate_semantics(
        intent["candidate"],
        target["artifacts"],
        path=f"{path}.candidate",
        artifacts_path=f"{path}.targetRelease.artifacts",
    )


def validate_protected_transition_policy(
    document: Any, path: str = "protected-transition-policy"
) -> dict[str, Any]:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion", "kind", "project", "repository", "sourceAuthority",
            "target", "intentPath", "verifier", "workflow", "merge",
            "authorizationTransport", "singleUse", "postMergeMutation", "expiresAt"
        },
        path=path,
    )
    common = _bootstrap_common(value, path, kind=POLICY_KIND)
    target_path = f"{path}.target"
    target = _object(value["target"], target_path)
    _exact_keys(target, required={"version", "tag", "commit", "processDigest"}, path=target_path)
    target_version = _version(target["version"], f"{target_path}.version")
    verifier_path = f"{path}.verifier"
    verifier = _object(value["verifier"], verifier_path)
    _exact_keys(verifier, required={"repository", "commit", "entrypoint"}, path=verifier_path)
    workflow_path = f"{path}.workflow"
    workflow = _object(value["workflow"], workflow_path)
    _exact_keys(
        workflow,
        required={
            "repository",
            "path",
            "checkContext",
            "checkAppId",
            "consumptionContext",
        },
        path=workflow_path,
    )
    check_app_id = workflow["checkAppId"]
    if (
        isinstance(check_app_id, bool)
        or not isinstance(check_app_id, int)
        or not 1 <= check_app_id <= 9_223_372_036_854_775_807
    ):
        raise ContractError(f"{workflow_path}.checkAppId: invalid GitHub App id")
    merge_path = f"{path}.merge"
    merge = _object(value["merge"], merge_path)
    _exact_keys(merge, required={"method", "requireCurrentBase", "requireExactHead", "requireRequiredChecks"}, path=merge_path)
    if merge != {
        "method": "protected-auto-merge",
        "requireCurrentBase": True,
        "requireExactHead": True,
        "requireRequiredChecks": True,
    }:
        raise ContractError(f"{merge_path}: must require exact protected auto-merge gates")
    transport_path = f"{path}.authorizationTransport"
    transport = _object(value["authorizationTransport"], transport_path)
    _exact_keys(
        transport,
        required={"kind", "evidenceKind", "maxEncodedBytes"},
        path=transport_path,
    )
    if transport != {
        "kind": "host-callback-gzip-base64",
        "evidenceKind": "bootstrap-authorization",
        "maxEncodedBytes": 60_000,
    }:
        raise ContractError(
            f"{transport_path}: must use the bounded authenticated host callback"
        )
    if value["singleUse"] is not True or value["postMergeMutation"] is not False:
        raise ContractError(f"{path}: must be single-use with no post-merge mutation")
    verifier_commit = _git_oid(verifier["commit"], f"{verifier_path}.commit")
    return {
        "schemaVersion": 1,
        "kind": POLICY_KIND,
        **common,
        "sourceAuthority": _authority(value["sourceAuthority"], f"{path}.sourceAuthority"),
        "target": {
            "version": target_version,
            "tag": target["tag"],
            "commit": _git_oid(target["commit"], f"{target_path}.commit"),
            "processDigest": _digest(target["processDigest"], f"{target_path}.processDigest"),
        },
        "intentPath": _relative_path(value["intentPath"], f"{path}.intentPath"),
        "verifier": {
            "repository": _repository(verifier["repository"], f"{verifier_path}.repository"),
            "commit": verifier_commit,
            "entrypoint": _relative_path(verifier["entrypoint"], f"{verifier_path}.entrypoint"),
        },
        "workflow": {
            "repository": _repository(workflow["repository"], f"{workflow_path}.repository"),
            "path": _relative_path(workflow["path"], f"{workflow_path}.path"),
            "checkContext": _string(workflow["checkContext"], f"{workflow_path}.checkContext", max_length=128),
            "checkAppId": check_app_id,
            "consumptionContext": _string(
                workflow["consumptionContext"],
                f"{workflow_path}.consumptionContext",
                max_length=128,
            ),
        },
        "merge": merge,
        "authorizationTransport": transport,
        "singleUse": True,
        "postMergeMutation": False,
        "expiresAt": _timestamp(value["expiresAt"], f"{path}.expiresAt"),
    }


def require_protected_transition_policy_semantics(
    policy: dict[str, Any], *, path: str = "protected-transition-policy"
) -> None:
    target = policy["target"]
    if target["tag"] != f"v{target['version']}":
        raise ContractError(f"{path}.target.tag: must be v{target['version']}")
    if target["version"] == policy["sourceAuthority"]["version"]:
        raise ContractError(f"{path}.target.version: must differ from source authority")
    if target["processDigest"] == policy["sourceAuthority"]["digest"]:
        raise ContractError(f"{path}.target.processDigest: must differ from source authority")
    if policy["verifier"]["commit"] == target["commit"]:
        raise ContractError(
            f"{path}.verifier.commit: target release cannot be its own verifier"
        )
    if (
        policy["verifier"]["repository"] != policy["repository"]
        or policy["workflow"]["repository"] != policy["repository"]
    ):
        raise ContractError(f"{path}: verifier and workflow repositories must match policy")


def _service_decimal(value: Any, path: str) -> str:
    text = _string(value, path, max_length=64)
    if re.fullmatch(r"[1-9][0-9]{0,63}", text) is None:
        raise ContractError(f"{path}: invalid service id")
    return text


def _service_attempt(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ContractError(f"{path}: invalid run attempt")
    return value


def _service_url(value: Any, path: str, *, run_id: str, attempt: int) -> str:
    url = _string(value, path, max_length=2_048)
    if not url.startswith("https://") or not url.endswith(
        f"/actions/runs/{run_id}/attempts/{attempt}"
    ):
        raise ContractError(f"{path}: does not bind the run id and attempt")
    return url


def _validate_transition_validation_service(
    document: Any,
    *,
    policy: dict[str, Any],
    protected_base: str,
) -> dict[str, Any]:
    path = "authority-transition validation service"
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "kind",
            "repository",
            "workflowPath",
            "workflowSha",
            "runId",
            "runAttempt",
            "runUrl",
            "event",
            "headSha",
            "checkContext",
            "checkAppId",
        },
        path=path,
    )
    if (
        value["schemaVersion"] != 1
        or value["kind"]
        != "engineering-process-transition-validation-service"
    ):
        raise ContractError(f"{path}: invalid schemaVersion or kind")
    run_id = _service_decimal(value["runId"], f"{path}.runId")
    run_attempt = _service_attempt(value["runAttempt"], f"{path}.runAttempt")
    service = {
        "schemaVersion": 1,
        "kind": "engineering-process-transition-validation-service",
        "repository": _repository(value["repository"], f"{path}.repository"),
        "workflowPath": _relative_path(
            value["workflowPath"], f"{path}.workflowPath"
        ),
        "workflowSha": _git_oid(value["workflowSha"], f"{path}.workflowSha"),
        "runId": run_id,
        "runAttempt": run_attempt,
        "runUrl": _service_url(
            value["runUrl"], f"{path}.runUrl", run_id=run_id, attempt=run_attempt
        ),
        "event": _string(value["event"], f"{path}.event", max_length=64),
        "headSha": _git_oid(value["headSha"], f"{path}.headSha"),
        "checkContext": _string(
            value["checkContext"], f"{path}.checkContext", max_length=128
        ),
        "checkAppId": value["checkAppId"],
    }
    if (
        service["repository"] != policy["repository"]
        or service["workflowPath"] != policy["workflow"]["path"]
        or service["workflowSha"] != protected_base
        or service["event"] != "workflow_dispatch"
        or service["headSha"] != protected_base
        or service["checkContext"] != policy["workflow"]["checkContext"]
        or service["checkAppId"] != policy["workflow"]["checkAppId"]
    ):
        raise ContractError(
            "authority-transition validation service does not match protected policy"
        )
    return service


def _validate_transition_consumption_service(
    document: Any,
    *,
    expected: Any,
    validation: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any]:
    path = "authority-transition consumption service"
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "kind",
            "repository",
            "workflowPath",
            "workflowSha",
            "runId",
            "runAttempt",
            "runUrl",
            "event",
            "headSha",
            "runStatus",
            "runConclusion",
            "checkContext",
            "checkAppId",
            "checkRunId",
            "checkHeadSha",
            "checkConclusion",
        },
        path=path,
    )
    if (
        value["schemaVersion"] != 1
        or value["kind"]
        != "engineering-process-transition-consumption-service"
        or not isinstance(expected, dict)
    ):
        raise ContractError(f"{path}: invalid schemaVersion, kind, or expected service")
    common = {
        key: value[key]
        for key in (
            "repository",
            "workflowPath",
            "workflowSha",
            "runId",
            "runAttempt",
            "runUrl",
            "event",
            "headSha",
            "checkContext",
            "checkAppId",
        )
    }
    expected_common = {
        key: expected[key]
        for key in common
    }
    if common != expected_common:
        raise ContractError(
            "authority-transition consumption service does not match validation service"
        )
    if (
        artifact.get("runId") != expected["runId"]
        or artifact.get("runAttempt") != expected["runAttempt"]
        or artifact.get("runUrl") != expected["runUrl"]
        or artifact.get("name")
        != f"authority-transition-validation-{validation['headCheckpoint']}"
    ):
        raise ContractError(
            "authority-transition validation artifact is not owned by the validation run"
        )
    status = _string(value["runStatus"], f"{path}.runStatus", max_length=32)
    conclusion = value["runConclusion"]
    if not (
        (status == "in_progress" and conclusion is None)
        or (status == "completed" and conclusion == "success")
    ):
        raise ContractError(f"{path}: validation workflow is not successful or active")
    check_run_id = _service_decimal(value["checkRunId"], f"{path}.checkRunId")
    check_head = _git_oid(value["checkHeadSha"], f"{path}.checkHeadSha")
    if (
        value["checkConclusion"] != "success"
        or check_head != validation["headCheckpoint"]
        or value["checkContext"] != validation["checkContext"]
        or value["checkAppId"] != expected["checkAppId"]
    ):
        raise ContractError(
            "authority-transition completion check is not policy-authenticated"
        )
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-transition-consumption-service",
        **common,
        "runStatus": status,
        "runConclusion": conclusion,
        "checkRunId": check_run_id,
        "checkHeadSha": check_head,
        "checkConclusion": "success",
    }


def validate_bootstrap_adoption_consumption(
    document: Any, path: str = "bootstrap-adoption-consumption"
) -> dict[str, Any]:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion", "kind", "project", "repository", "policySha256",
            "intentSha256", "authorizationSha256", "baseCheckpoint", "headCheckpoint",
            "headTree", "mergeCheckpoint", "checkContext", "validationArtifact",
            "validationService", "consumedAt"
        },
        path=path,
    )
    common = _bootstrap_common(value, path, kind=BOOTSTRAP_CONSUMPTION_KIND)
    validation_path = f"{path}.validationArtifact"
    validation = _object(value["validationArtifact"], validation_path)
    _exact_keys(
        validation,
        required={"artifactId", "name", "digest", "runId", "runAttempt", "runUrl"},
        path=validation_path,
    )
    artifact_id = _string(validation["artifactId"], f"{validation_path}.artifactId", max_length=64)
    run_id = _string(validation["runId"], f"{validation_path}.runId", max_length=64)
    if re.fullmatch(r"[1-9][0-9]{0,63}", artifact_id) is None or re.fullmatch(r"[1-9][0-9]{0,63}", run_id) is None:
        raise ContractError(f"{validation_path}: invalid service ids")
    run_attempt = validation["runAttempt"]
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or not 1 <= run_attempt <= 1000:
        raise ContractError(f"{validation_path}.runAttempt: invalid attempt")
    run_url = _string(validation["runUrl"], f"{validation_path}.runUrl", max_length=2048)
    if not run_url.startswith("https://"):
        raise ContractError(f"{validation_path}.runUrl: must use HTTPS")
    service_path = f"{path}.validationService"
    service = _object(value["validationService"], service_path)
    _exact_keys(
        service,
        required={
            "schemaVersion", "kind", "repository", "workflowPath", "workflowSha",
            "runId", "runAttempt", "runUrl", "event", "headSha", "runStatus",
            "runConclusion", "checkContext", "checkAppId", "checkRunId",
            "checkHeadSha", "checkConclusion",
        },
        path=service_path,
    )
    if (
        service["schemaVersion"] != 1
        or service["kind"]
        != "engineering-process-transition-consumption-service"
    ):
        raise ContractError(f"{service_path}: invalid schemaVersion or kind")
    service_run_id = _service_decimal(service["runId"], f"{service_path}.runId")
    service_attempt = _service_attempt(
        service["runAttempt"], f"{service_path}.runAttempt"
    )
    service_run_url = _string(
        service["runUrl"], f"{service_path}.runUrl", max_length=2_048
    )
    if not service_run_url.startswith("https://"):
        raise ContractError(f"{service_path}.runUrl: must use HTTPS")
    service_status = _string(
        service["runStatus"], f"{service_path}.runStatus", max_length=32
    )
    if service_status not in {"in_progress", "completed"} or service[
        "runConclusion"
    ] not in {None, "success"}:
        raise ContractError(f"{service_path}: invalid validation run state fields")
    check_app_id = service["checkAppId"]
    if (
        isinstance(check_app_id, bool)
        or not isinstance(check_app_id, int)
        or not 1 <= check_app_id <= 9_223_372_036_854_775_807
    ):
        raise ContractError(f"{service_path}.checkAppId: invalid GitHub App id")
    service_repository = _repository(
        service["repository"], f"{service_path}.repository"
    )
    service_workflow_path = _relative_path(
        service["workflowPath"], f"{service_path}.workflowPath"
    )
    service_workflow_sha = _git_oid(
        service["workflowSha"], f"{service_path}.workflowSha"
    )
    service_head = _git_oid(service["headSha"], f"{service_path}.headSha")
    service_check_head = _git_oid(
        service["checkHeadSha"], f"{service_path}.checkHeadSha"
    )
    service_check_context = _string(
        service["checkContext"], f"{service_path}.checkContext", max_length=128
    )
    if service["event"] != "workflow_dispatch" or service[
        "checkConclusion"
    ] != "success":
        raise ContractError(f"{service_path}: invalid event or check conclusion")
    return {
        "schemaVersion": 1,
        "kind": BOOTSTRAP_CONSUMPTION_KIND,
        **common,
        "policySha256": _digest(value["policySha256"], f"{path}.policySha256"),
        "intentSha256": _digest(value["intentSha256"], f"{path}.intentSha256"),
        "authorizationSha256": _digest(value["authorizationSha256"], f"{path}.authorizationSha256"),
        "baseCheckpoint": _git_oid(value["baseCheckpoint"], f"{path}.baseCheckpoint"),
        "headCheckpoint": _git_oid(value["headCheckpoint"], f"{path}.headCheckpoint"),
        "headTree": _git_oid(value["headTree"], f"{path}.headTree"),
        "mergeCheckpoint": _git_oid(value["mergeCheckpoint"], f"{path}.mergeCheckpoint"),
        "checkContext": _string(value["checkContext"], f"{path}.checkContext", max_length=128),
        "validationArtifact": {
            "artifactId": artifact_id,
            "name": _string(validation["name"], f"{validation_path}.name", max_length=128),
            "digest": _digest(validation["digest"], f"{validation_path}.digest"),
            "runId": run_id,
            "runAttempt": run_attempt,
            "runUrl": run_url,
        },
        "validationService": {
            "schemaVersion": 1,
            "kind": "engineering-process-transition-consumption-service",
            "repository": service_repository,
            "workflowPath": service_workflow_path,
            "workflowSha": service_workflow_sha,
            "runId": service_run_id,
            "runAttempt": service_attempt,
            "runUrl": service_run_url,
            "event": "workflow_dispatch",
            "headSha": service_head,
            "runStatus": service_status,
            "runConclusion": service["runConclusion"],
            "checkContext": service_check_context,
            "checkAppId": check_app_id,
            "checkRunId": _service_decimal(
                service["checkRunId"], f"{service_path}.checkRunId"
            ),
            "checkHeadSha": service_check_head,
            "checkConclusion": "success",
        },
        "consumedAt": _timestamp(value["consumedAt"], f"{path}.consumedAt"),
    }


def require_bootstrap_adoption_consumption_semantics(
    consumption: dict[str, Any], *, path: str = "bootstrap-adoption-consumption"
) -> None:
    artifact = consumption["validationArtifact"]
    service = consumption["validationService"]
    if not service["runUrl"].endswith(
        f"/actions/runs/{service['runId']}/attempts/{service['runAttempt']}"
    ):
        raise ContractError(f"{path}.validationService.runUrl: run binding mismatch")
    if not (
        (service["runStatus"] == "in_progress" and service["runConclusion"] is None)
        or (
            service["runStatus"] == "completed"
            and service["runConclusion"] == "success"
        )
    ):
        raise ContractError(f"{path}.validationService: invalid run state relation")
    if (
        service["repository"] != consumption["repository"]
        or service["workflowSha"] != consumption["baseCheckpoint"]
        or service["headSha"] != consumption["baseCheckpoint"]
        or service["checkContext"] != consumption["checkContext"]
        or service["checkHeadSha"] != consumption["headCheckpoint"]
        or artifact["runId"] != service["runId"]
        or artifact["runAttempt"] != service["runAttempt"]
        or artifact["runUrl"] != service["runUrl"]
        or artifact["name"]
        != f"authority-transition-validation-{consumption['headCheckpoint']}"
    ):
        raise ContractError(f"{path}: service, artifact, and transition bindings differ")
    if len(
        {
            consumption["baseCheckpoint"],
            consumption["headCheckpoint"],
            consumption["mergeCheckpoint"],
        }
    ) != 3:
        raise ContractError(f"{path}: base, head, and merge checkpoints must differ")


def _file_digest(path: Path, *, maximum: int = 1_000_000) -> str:
    content = _stable_file_bytes(path, maximum=maximum)
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _git_output(root: Path, arguments: list[str], *, label: str, maximum: int = 1_000_000) -> bytes:
    result = run_git(root, arguments, label=label, timeout_seconds=30, max_stdout_bytes=maximum)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"{label} failed" + (f": {detail}" if detail else ""))
    return result.stdout


def _checkout_repository(root: Path) -> str:
    raw = _git_output(
        root,
        ["remote", "get-url", "origin"],
        label="resolve authority-transition target repository",
        maximum=2_048,
    )
    try:
        remote = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError(
            "authority-transition target repository URL must be UTF-8"
        ) from error
    if not remote or "\x00" in remote:
        raise ContractError("authority-transition target repository URL is invalid")
    scp = re.fullmatch(r"[^@\s]+@[^:\s]+:(?P<path>[^\s]+)", remote)
    if scp is not None:
        path = scp.group("path")
    else:
        parsed = urlparse(remote)
        if parsed.scheme not in {"https", "ssh"} or not parsed.netloc:
            raise ContractError(
                "authority-transition target repository must use an authenticated remote URL"
            )
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if REPOSITORY_PATTERN.fullmatch(path) is None:
        raise ContractError(
            "authority-transition target repository URL has no valid repository identity"
        )
    return path


def _installed_package_root(process_root: Path) -> Path:
    process_root = process_root.resolve(strict=True)
    candidates = [
        process_root / "engineering_process",
        process_root / "Lib" / "site-packages" / "engineering_process",
    ]
    lib_root = process_root / "lib"
    if lib_root.is_dir() and not lib_root.is_symlink():
        python_roots = sorted(lib_root.glob("python*"))
        if len(python_roots) > 8:
            raise ContractError("target runtime has too many Python library roots")
        candidates.extend(
            root / "site-packages" / "engineering_process"
            for root in python_roots
        )
    matches = [path for path in candidates if path.is_dir() and not path.is_symlink()]
    if len(matches) != 1:
        raise ContractError("cannot resolve one exact installed target package root")
    return matches[0].resolve(strict=True)


def inspect_transition_candidate(
    candidate_root: Path,
    request: dict[str, Any],
    *,
    target_process_root: Path,
) -> dict[str, Any]:
    from .syncing import load_lock, synchronized_state

    candidate_root = candidate_root.resolve(strict=True)
    source = source_state(candidate_root)
    if source["dirty"] is not False or source["checkpoint"] is None or source["fingerprint"] is None:
        raise ContractError("authority-transition candidate must be a clean immutable checkpoint")
    checkpoint = source["checkpoint"]
    tree = _git_output(candidate_root, ["rev-parse", "--verify", f"{checkpoint}^{{tree}}"], label="resolve transition candidate tree", maximum=128).decode("ascii").strip()
    changed_output = _git_output(
        candidate_root,
        ["diff", "--name-only", "-z", request["candidate"]["baseCheckpoint"], checkpoint, "--"],
        label="inspect transition candidate paths",
        maximum=500_000,
    )
    changed_paths = sorted(os.fsdecode(item) for item in changed_output.split(b"\0") if item)
    if changed_paths != request["candidate"]["expectedChangedPaths"]:
        raise ContractError("authority-transition candidate changed paths do not match request")
    lock = load_lock(candidate_root)
    target = request["target"]
    if lock.version != target["version"] or lock.digest != target["processDigest"]:
        raise ContractError("authority-transition candidate lock does not match target")
    package_root = _installed_package_root(target_process_root)
    issues = synchronized_state(
        candidate_root,
        target_process_root.resolve(strict=True),
        lock,
        authority_version=target["version"],
        package_root=package_root,
    )
    if issues:
        raise ContractError("\n".join(issues))
    actual_distribution = distribution_digest(
        target_process_root.resolve(strict=True),
        lock.skills,
        package_root=package_root,
    )
    if actual_distribution != target["processDigest"]:
        raise ContractError("target distribution does not match transition process digest")
    return {
        "checkpoint": checkpoint,
        "tree": tree,
        "workspaceFingerprint": source["fingerprint"],
        "changedPaths": changed_paths,
        "lock": lock,
    }


def _target_authority_command(target_process_root: Path) -> list[str]:
    target_process_root = target_process_root.resolve(strict=True)
    source_entrypoint = target_process_root / "processctl.py"
    source_package = target_process_root / "engineering_process"
    if (
        source_entrypoint.is_file()
        and not source_entrypoint.is_symlink()
        and source_package.is_dir()
        and not source_package.is_symlink()
    ):
        return [sys.executable, str(source_entrypoint)]
    candidates = (
        target_process_root / "Scripts" / "python.exe",
        target_process_root / "bin" / "python",
    )
    available = [path for path in candidates if path.exists() and path.is_file()]
    if len(available) != 1:
        raise ContractError("cannot resolve one exact target authority interpreter")
    return [str(available[0]), "-I", "-m", "engineering_process"]


def _target_environment(temporary_root: Path) -> dict[str, str]:
    environment: dict[str, str] = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TEMP": str(temporary_root),
        "TMP": str(temporary_root),
        "TMPDIR": str(temporary_root),
    }
    for name in ("COMSPEC", "PATHEXT", "SystemRoot", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_target_adoption(
    target_process_root: Path,
    workspace: Path,
    *,
    action: str,
    deadline: float,
    rollback_probe: bool = False,
) -> dict[str, Any] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError("authority-transition materialization exceeded its time budget")
    command = [
        *_target_authority_command(target_process_root),
        "adoption",
        action,
        "--project-root",
        str(workspace),
        "--requirements-lock",
        str(workspace / "requirements" / "process.txt"),
        "--json",
    ]
    if rollback_probe:
        command.append("--rollback-probe")
    try:
        result = run_bounded_process(
            command,
            working_directory=workspace,
            environment=_target_environment(workspace.parent),
            timeout_seconds=remaining,
            max_stream_bytes=MATERIALIZATION_OUTPUT_BYTES,
            max_total_bytes=MATERIALIZATION_OUTPUT_BYTES,
        )
    except (OSError, ValueError) as error:
        raise ContractError(
            f"cannot execute target authority materialization: {error}"
        ) from error
    if (
        result.timed_out
        or result.output_exceeded
        or result.descendants_found
        or result.cleanup_error is not None
        or result.input_error
    ):
        raise ContractError(
            result.cleanup_error
            or "target authority materialization lost its bounded execution boundary"
        )
    if rollback_probe:
        try:
            failure_document = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractError(
                "target authority rollback probe returned invalid JSON"
            ) from error
        if (
            result.returncode != 2
            or result.stderr
            or not isinstance(failure_document, dict)
            or failure_document.get("status") != "failed"
            or failure_document.get("errors")
            != ["controlled authority-transition rollback probe"]
        ):
            raise ContractError(
                "target authority did not execute the controlled rollback probe"
            )
        return None
    if result.returncode != 0 or result.stderr:
        raise ContractError("target authority adoption command failed")
    try:
        document = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(
            "target authority adoption command returned invalid JSON"
        ) from error
    if not isinstance(document, dict) or document.get("status") != "passed":
        raise ContractError("target authority adoption command did not pass")
    return document


@contextmanager
def _materialization_worktree(candidate_root: Path, base_checkpoint: str):
    with tempfile.TemporaryDirectory(
        prefix="engineering-process-transition-materialization-"
    ) as directory:
        workspace = Path(directory) / "workspace"
        _git_output(
            candidate_root,
            ["worktree", "add", "--detach", str(workspace), base_checkpoint],
            label="create authority-transition materialization worktree",
            maximum=4_096,
        )
        try:
            yield workspace
        finally:
            _git_output(
                candidate_root,
                ["worktree", "remove", "--force", str(workspace)],
                label="remove authority-transition materialization worktree",
                maximum=4_096,
            )


def _write_materialization_input(
    workspace: Path, relative: str, content: bytes
) -> None:
    destination = workspace / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.resolve(strict=True).relative_to(
            workspace.resolve(strict=True)
        )
    except (OSError, ValueError) as error:
        raise ContractError(
            "authority-transition materialization input escapes its worktree"
        ) from error
    if os.path.lexists(destination) and destination.is_symlink():
        raise ContractError(
            "authority-transition materialization input must not replace a symlink"
        )
    destination.write_bytes(content)


def _prepare_materialization_inputs(
    candidate_root: Path, workspace: Path, request: dict[str, Any]
) -> None:
    relative_paths = {
        "requirements/process.in",
        "requirements/process.txt",
        *(item["path"] for item in request["candidate"]["actionPins"]),
    }
    migration_digest = request["candidate"]["projectMigrationSha256"]
    if migration_digest is not None:
        relative_paths.add(
            ".process/adoption-migrations/"
            f"{request['target']['version']}.json"
        )
    total = 0
    for relative in sorted(relative_paths):
        content = _stable_file_bytes(candidate_root / relative)
        total += len(content)
        if total > 16_000_000:
            raise ContractError(
                "authority-transition materialization inputs exceed 16000000 bytes"
            )
        _write_materialization_input(workspace, relative, content)


def _materialization_tree(workspace: Path) -> str:
    _git_output(
        workspace,
        ["add", "-A"],
        label="stage authority-transition materialization tree",
        maximum=4_096,
    )
    return _git_output(
        workspace,
        ["write-tree"],
        label="resolve authority-transition materialization tree",
        maximum=128,
    ).decode("ascii").strip()


def observe_candidate_materialization(
    candidate_root: Path,
    request: dict[str, Any],
    *,
    target_process_root: Path,
    expected_tree: str,
) -> dict[str, Any]:
    candidate_root = candidate_root.resolve(strict=True)
    target_process_root = target_process_root.resolve(strict=True)
    deadline = time.monotonic() + MATERIALIZATION_TIMEOUT_SECONDS
    with _materialization_worktree(
        candidate_root, request["candidate"]["baseCheckpoint"]
    ) as workspace:
        _prepare_materialization_inputs(candidate_root, workspace, request)
        _run_target_adoption(
            target_process_root, workspace, action="apply", deadline=deadline
        )
        _run_target_adoption(
            target_process_root, workspace, action="check", deadline=deadline
        )
        apply_tree = _materialization_tree(workspace)
        if apply_tree != expected_tree:
            raise ContractError(
                "target authority materialization does not reproduce the candidate tree"
            )
        _run_target_adoption(
            target_process_root, workspace, action="apply", deadline=deadline
        )
        _run_target_adoption(
            target_process_root, workspace, action="check", deadline=deadline
        )
        idempotent_tree = _materialization_tree(workspace)
        if idempotent_tree != apply_tree:
            raise ContractError(
                "target authority adoption apply/check is not idempotent"
            )
    with _materialization_worktree(
        candidate_root, request["candidate"]["baseCheckpoint"]
    ) as rollback_workspace:
        _prepare_materialization_inputs(
            candidate_root, rollback_workspace, request
        )
        rollback_before = _materialization_tree(rollback_workspace)
        _run_target_adoption(
            target_process_root,
            rollback_workspace,
            action="apply",
            deadline=deadline,
            rollback_probe=True,
        )
        rollback_after = _materialization_tree(rollback_workspace)
        if rollback_after != rollback_before:
            raise ContractError(
                "target authority adoption rollback did not restore the input tree"
            )
    return {
        "status": "passed",
        "applyTree": apply_tree,
        "idempotentTree": idempotent_tree,
        "checkRuns": 2,
        "rollback": {
            "status": "restored",
            "beforeTree": rollback_before,
            "afterTree": rollback_after,
            "probe": "after-authority-write",
        },
        "issues": [],
    }


def validate_registered_candidate(
    candidate_root: Path,
    request_document: dict[str, Any],
    evidence_document: dict[str, Any],
    *,
    target_process_root: Path,
) -> dict[str, Any]:
    request = validate_authority_transition_request(request_document)
    require_authority_transition_request_semantics(request)
    evidence = validate_authority_transition_evidence(evidence_document)
    require_authority_transition_evidence_semantics(evidence)
    if evidence["requestSha256"] != canonical_json_digest(request_document):
        raise ContractError("authority-transition evidence does not bind the request")
    for field in ("project", "changeId", "cycle"):
        if evidence[field] != request[field]:
            raise ContractError(f"authority-transition evidence {field} does not match request")
    if evidence["sourceAuthority"] != request["source"]["authority"]:
        raise ContractError("authority-transition source authority does not match")
    if evidence["targetAuthority"] != {
        "version": request["target"]["version"],
        "digest": request["target"]["processDigest"],
    }:
        raise ContractError("authority-transition target authority does not match")
    inspected = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
    )
    candidate = evidence["candidate"]
    expected = {
        "baseCheckpoint": request["candidate"]["baseCheckpoint"],
        "checkpoint": inspected["checkpoint"],
        "tree": inspected["tree"],
        "workspaceFingerprint": inspected["workspaceFingerprint"],
        "workingTreeDirty": False,
        "changedPaths": inspected["changedPaths"],
    }
    if candidate != expected:
        raise ContractError("authority-transition candidate evidence is stale")
    observed_materialization = observe_candidate_materialization(
        candidate_root,
        request,
        target_process_root=target_process_root,
        expected_tree=inspected["tree"],
    )
    if evidence["materialization"] != observed_materialization:
        raise ContractError(
            "authority-transition materialization evidence is stale or unobserved"
        )
    if evidence["bindings"]["processLockSha256"] != _file_digest(
        Path(candidate_root) / ".process" / "process.lock"
    ):
        raise ContractError("authority-transition process lock binding is stale")
    if evidence["bindings"]["requirementsLockSha256"] != _file_digest(
        Path(candidate_root) / "requirements" / "process.txt"
    ):
        raise ContractError("authority-transition requirements lock binding is stale")
    candidate_root = Path(candidate_root).resolve(strict=True)
    if evidence["bindings"]["requirementsInputSha256"] != _file_digest(
        candidate_root / "requirements" / "process.in"
    ):
        raise ContractError("authority-transition requirements input binding is stale")
    if evidence["bindings"]["requirementsInputSha256"] != request["candidate"]["requirementsInputSha256"]:
        raise ContractError("authority-transition requirements input is not pre-registered")
    if evidence["bindings"]["requirementsLockSha256"] != request["candidate"]["requirementsLockSha256"]:
        raise ContractError("authority-transition requirements lock is not pre-registered")
    if evidence["bindings"]["projectManifestSha256"] != _file_digest(
        candidate_root / ".process" / "project.json"
    ):
        raise ContractError("authority-transition project manifest binding is stale")
    if evidence["bindings"]["projectManifestSha256"] != request["candidate"]["projectManifestSha256"]:
        raise ContractError("authority-transition project manifest is not pre-registered")
    migration_path = (
        candidate_root
        / ".process"
        / "adoption-migrations"
        / f"{request['target']['version']}.json"
    )
    migration_digest = _optional_file_digest(migration_path)
    if (
        evidence["bindings"]["projectMigrationSha256"] != migration_digest
        or migration_digest != request["candidate"]["projectMigrationSha256"]
    ):
        raise ContractError("authority-transition project migration binding is stale")
    if evidence["bindings"]["managedDistributionSha256"] != request["target"]["processDigest"]:
        raise ContractError("authority-transition managed distribution binding is stale")
    action_pins_digest = _validate_action_pin_changes(
        candidate_root,
        base_checkpoint=request["candidate"]["baseCheckpoint"],
        head_checkpoint=inspected["checkpoint"],
        declarations=request["candidate"]["actionPins"],
    )
    if evidence["bindings"]["actionPinsSha256"] != action_pins_digest:
        raise ContractError("authority-transition action pin binding is stale")
    if evidence["bindings"]["actionPinsSha256"] != request["candidate"]["actionPinsSha256"]:
        raise ContractError("authority-transition action pins are not pre-registered")
    if list(inspected["lock"].skills) != request["candidate"]["selectedSkills"]:
        raise ContractError("authority-transition selected skills are incomplete")
    return {"request": request, "evidence": evidence, "candidate": inspected}


def _optional_file_digest(path: Path) -> str | None:
    return _file_digest(path) if path.is_file() and not path.is_symlink() else None


def _action_pins_digest(project_root: Path) -> str:
    workflow_root = project_root / ".github" / "workflows"
    entries: list[dict[str, str]] = []
    total = 0
    if workflow_root.is_dir() and not workflow_root.is_symlink():
        paths = sorted(
            path
            for path in workflow_root.iterdir()
            if path.suffix in {".yml", ".yaml"}
        )
        if len(paths) > 256:
            raise ContractError("authority-transition workflow count exceeds 256")
        for path in paths:
            try:
                content = path.read_bytes()
            except OSError as error:
                raise ContractError(f"cannot read workflow pin input {path}: {error}") from error
            total += len(content)
            if len(content) > 1_000_000 or total > 8_000_000:
                raise ContractError("authority-transition workflow inputs exceed bounds")
            entries.append(
                {
                    "path": path.relative_to(project_root).as_posix(),
                    "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
                }
            )
    return canonical_json_digest(entries)


def _workflow_snapshot(
    project_root: Path, checkpoint: str
) -> dict[str, tuple[str, str]]:
    output = _git_output(
        project_root,
        ["ls-tree", "-rz", checkpoint, "--", ".github/workflows"],
        label="inspect authority-transition workflow tree",
        maximum=2_000_000,
    )
    snapshot: dict[str, tuple[str, str]] = {}
    total = 0
    for raw in (item for item in output.split(b"\0") if item):
        try:
            metadata, encoded_path = raw.split(b"\t", 1)
            mode, kind, _oid = metadata.decode("ascii").split(" ")
            relative = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ContractError("authority-transition workflow tree is invalid") from error
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ContractError(f"workflow must be a regular blob: {relative}")
        if not relative.endswith((".yml", ".yaml")):
            continue
        content = _git_file(
            project_root,
            checkpoint,
            relative,
            label=f"read workflow {relative}",
        )
        total += len(content)
        if len(content) > 1_000_000 or total > 8_000_000:
            raise ContractError("authority-transition workflow tree exceeds bounds")
        try:
            snapshot[relative] = (mode, content.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ContractError(f"workflow must be UTF-8: {relative}") from error
    if len(snapshot) > 256:
        raise ContractError("authority-transition workflow count exceeds 256")
    return snapshot


USES_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:-[ \t]*)?uses[ \t]*:[ \t]*['\"]?(?P<value>[^'\"# \t\r\n]+)"
)


def _validate_pinned_uses(workflows: dict[str, tuple[str, str]]) -> None:
    for path, (_mode, content) in workflows.items():
        for match in USES_PATTERN.finditer(content):
            value = match.group("value")
            if value.startswith("./"):
                continue
            if value.startswith("docker://"):
                if re.fullmatch(r"docker://[^@\s]+@sha256:[0-9a-f]{64}", value) is None:
                    raise ContractError(f"workflow {path} contains an unpinned Docker action")
                continue
            if re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", value
            ) is None:
                raise ContractError(f"workflow {path} contains a non-full-SHA action use")


def _validate_action_pin_changes(
    project_root: Path,
    *,
    base_checkpoint: str,
    head_checkpoint: str,
    declarations: list[dict[str, str]],
) -> str:
    base = _workflow_snapshot(project_root, base_checkpoint)
    head = _workflow_snapshot(project_root, head_checkpoint)
    _validate_pinned_uses(base)
    _validate_pinned_uses(head)
    declared_paths = {item["path"] for item in declarations}
    changed_paths = {
        path for path in set(base).union(head) if base.get(path) != head.get(path)
    }
    if changed_paths != declared_paths:
        raise ContractError(
            "authority-transition workflow changes do not match declared action pins"
        )
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in declarations:
        grouped.setdefault(item["path"], []).append(item)
    declared_repositories = {item["repository"] for item in declarations}
    for repository in declared_repositories:
        declared_repository_paths = {
            item["path"] for item in declarations if item["repository"] == repository
        }
        observed_paths = {
            path
            for path, (_mode, content) in base.items()
            if re.search(
                rf"(?m)^[ \t]*(?:-[ \t]*)?uses[ \t]*:[ \t]*['\"]?{re.escape(repository)}@",
                content,
            )
        }
        if observed_paths != declared_repository_paths:
            raise ContractError(
                f"authority-transition action group for {repository} is incomplete"
            )
    for path, pins in grouped.items():
        if path not in base or path not in head or base[path][0] != head[path][0]:
            raise ContractError(f"authority-transition workflow mode changed: {path}")
        expected = base[path][1]
        for pin in pins:
            pattern = re.compile(
                rf"(?m)(uses[ \t]*:[ \t]*(?P<quote>['\"]?){re.escape(pin['repository'])}@)"
                rf"{pin['previousCommit']}(?P=quote)([ \t]+#[ \t]*)"
                rf"{re.escape(pin['previousReleaseTag'])}([ \t]*)(?P<line_ending>\r?)$"
            )
            expected, replacements = pattern.subn(
                lambda match: (
                    match.group(1)
                    + pin["targetCommit"]
                    + match.group("quote")
                    + match.group(3)
                    + pin["targetReleaseTag"]
                    + match.group(4)
                    + match.group("line_ending")
                ),
                expected,
            )
            if replacements < 1:
                raise ContractError(
                    f"authority-transition workflow lacks declared previous pin: {path}"
                )
        if expected != head[path][1]:
            raise ContractError(
                f"authority-transition workflow contains changes beyond declared pins: {path}"
            )
    return _action_pins_digest(project_root)


def create_authority_transition_evidence(
    candidate_root: Path,
    request_document: dict[str, Any],
    *,
    target_process_root: Path,
    actor_id: str,
    context_id: str,
    actor_kind: str,
) -> dict[str, Any]:
    request = validate_authority_transition_request(request_document)
    require_authority_transition_request_semantics(request)
    _require_not_expired(request["candidate"]["expiresAt"], label="authority-transition request")
    inspected = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
    )
    materialization = observe_candidate_materialization(
        candidate_root,
        request,
        target_process_root=target_process_root,
        expected_tree=inspected["tree"],
    )
    candidate_root = candidate_root.resolve(strict=True)
    migration_path = (
        candidate_root
        / ".process"
        / "adoption-migrations"
        / f"{request['target']['version']}.json"
    )
    migration_digest = _optional_file_digest(migration_path)
    if migration_digest != request["candidate"]["projectMigrationSha256"]:
        raise ContractError("authority-transition project migration does not match request")
    if list(inspected["lock"].skills) != request["candidate"]["selectedSkills"]:
        raise ContractError("authority-transition selected skills do not match request")
    evidence = {
        "schemaVersion": 1,
        "kind": EVIDENCE_KIND,
        "project": request["project"],
        "changeId": request["changeId"],
        "cycle": request["cycle"],
        "requestSha256": canonical_json_digest(request_document),
        "sourceAuthority": request["source"]["authority"],
        "targetAuthority": {
            "version": request["target"]["version"],
            "digest": request["target"]["processDigest"],
        },
        "candidate": {
            "baseCheckpoint": request["candidate"]["baseCheckpoint"],
            "checkpoint": inspected["checkpoint"],
            "tree": inspected["tree"],
            "workspaceFingerprint": inspected["workspaceFingerprint"],
            "workingTreeDirty": False,
            "changedPaths": inspected["changedPaths"],
        },
        "bindings": {
            "requirementsInputSha256": _file_digest(
                candidate_root / "requirements" / "process.in"
            ),
            "requirementsLockSha256": _file_digest(
                candidate_root / "requirements" / "process.txt"
            ),
            "processLockSha256": _file_digest(
                candidate_root / ".process" / "process.lock"
            ),
            "projectManifestSha256": _file_digest(
                candidate_root / ".process" / "project.json"
            ),
            "projectMigrationSha256": migration_digest,
            "managedDistributionSha256": request["target"]["processDigest"],
            "actionPinsSha256": _validate_action_pin_changes(
                candidate_root,
                base_checkpoint=request["candidate"]["baseCheckpoint"],
                head_checkpoint=inspected["checkpoint"],
                declarations=request["candidate"]["actionPins"],
            ),
        },
        "materialization": materialization,
        "generatedBy": _actor(
            {"actorId": actor_id, "contextId": context_id, "kind": actor_kind},
            "authority-transition evidence generatedBy",
        ),
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    for field in (
        "actionPinsSha256",
        "projectManifestSha256",
        "requirementsInputSha256",
        "requirementsLockSha256",
    ):
        if evidence["bindings"][field] != request["candidate"][field]:
            raise ContractError(
                f"authority-transition candidate {field} does not match request"
            )
    validated_evidence = validate_authority_transition_evidence(evidence)
    require_authority_transition_evidence_semantics(validated_evidence)
    return evidence


def _contained_relative(root: Path, path: Path, *, label: str) -> str:
    root = root.resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise ContractError(f"{label} must stay within the candidate repository") from error
    return _relative_path(relative, label)


def _git_file(root: Path, checkpoint: str, relative: str, *, label: str) -> bytes:
    return _git_output(
        root,
        ["show", f"{checkpoint}:{relative}"],
        label=label,
        maximum=1_000_000,
    )


def _stable_file_bytes(path: Path, *, maximum: int = 1_000_000) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"transition artifact must be a regular non-symlink file: {path}")
        if before.st_size > maximum:
            raise ContractError(f"transition artifact exceeds {maximum} bytes: {path}")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ContractError(f"transition artifact changed while opening: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(maximum + 1)
        after = path.lstat()
    except ContractError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ContractError(f"cannot read transition artifact {path}: {error}") from error
    if len(content) > maximum or len(content) != before.st_size:
        raise ContractError(f"transition artifact exceeds {maximum} bytes: {path}")
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"transition artifact changed while reading: {path}")
    return content


def _require_not_expired(value: str, *, label: str) -> None:
    expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expires <= datetime.now(UTC):
        raise ContractError(f"{label} has expired")


def require_authority_transition_current(request: dict[str, Any]) -> None:
    validated = validate_authority_transition_request(request)
    require_authority_transition_request_semantics(validated)
    _require_not_expired(
        validated["candidate"]["expiresAt"], label="authority-transition request"
    )


def validate_transition_target_provenance(
    request_document: dict[str, Any],
    *,
    target_checkout: Path,
    artifact_root: Path,
    release_receipt_path: Path,
    artifact_attestation_path: Path,
) -> dict[str, Any]:
    from .artifact_attestation import validate_distribution_attestation
    from .release import validate_release_checkpoint

    request = validate_authority_transition_request(request_document)
    require_authority_transition_current(request)
    target_checkout = target_checkout.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    target = request["target"]
    if _checkout_repository(target_checkout).casefold() != target[
        "repository"
    ].casefold():
        raise ContractError("authority-transition target repository mismatch")
    release_result = validate_release_checkpoint(
        target_checkout,
        tag=target["tag"],
        release_name=target["tag"],
        commit=target["commit"],
        main_ref=target["commit"],
        receipt_path=release_receipt_path,
    )
    if (
        release_result["version"] != target["version"]
        or release_result["provenanceMode"]
        not in {"governed", "authority-transition-bootstrap"}
        or _file_digest(target_checkout / "release.json")
        != target["releaseContractSha256"]
        or _file_digest(release_receipt_path, maximum=8_000_000)
        != target["lifecycleReceiptSha256"]
    ):
        raise ContractError("authority-transition target release provenance mismatch")
    receipt = release_result["lifecycleReceipt"]
    if not isinstance(receipt, dict) or {
        "version": receipt["processVersion"],
        "digest": receipt["processDigest"],
    } != request["source"]["authority"]:
        raise ContractError(
            "authority-transition target receipt is not governed by the source authority"
        )
    attestation = validate_distribution_attestation(
        target_checkout,
        artifact_root,
        artifact_attestation_path,
        receipt_path=release_receipt_path,
        checkpoint=target["commit"],
    )
    if (
        _file_digest(artifact_attestation_path)
        != target["distributionAttestationSha256"]
    ):
        raise ContractError("authority-transition target attestation digest mismatch")
    actual_artifacts = [
        {
            "name": item["name"],
            "sha256": item["sha256"],
            "sizeBytes": item["sizeBytes"],
        }
        for item in attestation["artifacts"]
    ]
    if actual_artifacts != target["artifacts"]:
        raise ContractError("authority-transition target artifact bytes mismatch")
    return {
        "version": target["version"],
        "tag": target["tag"],
        "commit": target["commit"],
        "processDigest": target["processDigest"],
        "releaseContractSha256": target["releaseContractSha256"],
        "lifecycleReceiptSha256": target["lifecycleReceiptSha256"],
        "distributionAttestationSha256": target[
            "distributionAttestationSha256"
        ],
        "artifacts": target["artifacts"],
        "sourceAuthority": request["source"]["authority"],
    }


def validate_bootstrap_transition_candidate(
    controller_root: Path,
    candidate_root: Path,
    *,
    policy_path: Path,
    intent_path: Path,
    authorization_path: Path,
    target_checkout: Path,
    target_process_root: Path,
    artifact_root: Path,
    release_receipt_path: Path,
    artifact_attestation_path: Path,
    protected_base_ref: str,
    validation_service_path: Path,
) -> dict[str, Any]:
    from .artifact_attestation import validate_distribution_attestation
    from .evidence import validate_bootstrap_authorization
    from .release import validate_release_checkpoint

    controller_root = controller_root.resolve(strict=True)
    candidate_root = candidate_root.resolve(strict=True)
    target_checkout = target_checkout.resolve(strict=True)
    policy = validate_protected_transition_policy(
        read_json(policy_path), str(policy_path)
    )
    intent = validate_bootstrap_adoption_intent(
        read_json(intent_path), str(intent_path)
    )
    require_bootstrap_adoption_intent_semantics(intent)
    require_protected_transition_policy_semantics(policy)
    authorization = validate_bootstrap_authorization(authorization_path)
    authorization_document = read_json(authorization_path)
    _require_not_expired(policy["expiresAt"], label="protected transition policy")
    _require_not_expired(intent["expiresAt"], label="bootstrap adoption intent")
    if (
        policy["project"] != intent["project"]
        or policy["repository"] != intent["repository"]
        or policy["sourceAuthority"] != intent["sourceAuthority"]
        or policy["target"]
        != {
            "version": intent["targetRelease"]["version"],
            "tag": intent["targetRelease"]["tag"],
            "commit": intent["targetRelease"]["commit"],
            "processDigest": intent["targetRelease"]["processDigest"],
        }
    ):
        raise ContractError("bootstrap intent and protected policy identity mismatch")
    if intent["policySha256"] != _file_digest(policy_path):
        raise ContractError("bootstrap intent does not bind protected policy bytes")
    if (
        authorization["project"] != intent["project"]
        or authorization["changeId"] != intent["changeId"]
        or authorization["processVersion"] != intent["sourceAuthority"]["version"]
        or authorization["processDigest"] != intent["sourceAuthority"]["digest"]
    ):
        raise ContractError("public source authorization does not bind bootstrap intent")
    plan_decision = authorization_document.get("artifacts", {}).get("planDecision")
    resolution = (
        plan_decision.get("resolution") if isinstance(plan_decision, dict) else None
    )
    if (
        not isinstance(resolution, dict)
        or resolution.get("sourceDigest") != intent["ownerResolutionSha256"]
    ):
        raise ContractError(
            "bootstrap authorization does not bind the declared owner resolution"
        )
    controller_head = _git_output(
        controller_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        label="resolve protected transition verifier",
        maximum=128,
    ).decode("ascii").strip()
    if controller_head != policy["verifier"]["commit"]:
        raise ContractError("protected transition verifier checkout is not policy-fixed")
    if (
        _contained_relative(candidate_root, intent_path, label="bootstrap intent path")
        != policy["intentPath"]
    ):
        raise ContractError("bootstrap intent path does not match protected policy")
    policy_relative = _contained_relative(
        candidate_root, policy_path, label="protected transition policy path"
    )
    if _git_file(
        candidate_root,
        authorization["checkpoint"],
        policy["intentPath"],
        label="read authorized bootstrap intent",
    ) != _stable_file_bytes(intent_path):
        raise ContractError("bootstrap authorization checkpoint does not bind intent bytes")
    if _git_file(
        candidate_root,
        authorization["checkpoint"],
        policy_relative,
        label="read authorized protected transition policy",
    ) != _stable_file_bytes(policy_path):
        raise ContractError("bootstrap authorization checkpoint does not bind policy bytes")
    protected_base = _git_output(
        candidate_root,
        ["rev-parse", "--verify", "--end-of-options", f"{protected_base_ref}^{{commit}}"],
        label="resolve protected transition base",
        maximum=128,
    ).decode("ascii").strip()
    authorization_tree = _git_output(
        candidate_root,
        ["rev-parse", "--verify", f"{authorization['checkpoint']}^{{tree}}"],
        label="resolve authorized bootstrap tree",
        maximum=128,
    ).decode("ascii").strip()
    protected_base_tree = _git_output(
        candidate_root,
        ["rev-parse", "--verify", f"{protected_base}^{{tree}}"],
        label="resolve protected bootstrap base tree",
        maximum=128,
    ).decode("ascii").strip()
    if authorization_tree != protected_base_tree:
        raise ContractError(
            "protected base tree does not match the N-1 authorized intent/policy tree"
        )
    _git_output(
        candidate_root,
        ["merge-base", "--is-ancestor", policy["verifier"]["commit"], protected_base],
        label="verify source-owned transition controller ancestry",
        maximum=128,
    )
    base_lock_content = _git_file(
        candidate_root,
        protected_base,
        ".process/process.lock",
        label="read protected-base process lock",
    )
    try:
        base_lock_document = json.loads(base_lock_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("protected-base process lock is invalid") from error
    base_lock = validate_process_lock(base_lock_document, "protected-base process.lock")
    if policy["sourceAuthority"] != {
        "version": base_lock.version,
        "digest": base_lock.digest,
    }:
        raise ContractError("protected-base process lock does not match source authority")
    validation_service = _validate_transition_validation_service(
        read_json(validation_service_path),
        policy=policy,
        protected_base=protected_base,
    )

    target = intent["targetRelease"]
    release_result = validate_release_checkpoint(
        target_checkout,
        tag=target["tag"],
        release_name=target["tag"],
        commit=target["commit"],
        main_ref=target["commit"],
        receipt_path=release_receipt_path,
    )
    if (
        release_result["version"] != target["version"]
        or release_result["provenanceMode"] != "authority-transition-bootstrap"
        or _file_digest(target_checkout / "release.json")
        != target["releaseContractSha256"]
    ):
        raise ContractError("bootstrap target release identity mismatch")
    attestation = validate_distribution_attestation(
        target_checkout,
        artifact_root,
        artifact_attestation_path,
        receipt_path=release_receipt_path,
        checkpoint=target["commit"],
    )
    if _file_digest(artifact_attestation_path) != target["distributionAttestationSha256"]:
        raise ContractError("bootstrap target attestation digest mismatch")
    attested_artifacts = {
        item["name"]: (item["sha256"], item["sizeBytes"])
        for item in attestation["artifacts"]
    }
    if attested_artifacts != {
        item["name"]: (item["sha256"], item["sizeBytes"])
        for item in target["artifacts"]
    }:
        raise ContractError("bootstrap target artifact set mismatch")

    request = {
        "candidate": {
            "baseCheckpoint": protected_base,
            "expectedChangedPaths": intent["candidate"]["expectedChangedPaths"],
            "actionPins": intent["candidate"]["actionPins"],
            "projectMigrationSha256": intent["candidate"]["projectMigrationSha256"],
        },
        "target": {
            "version": target["version"],
            "processDigest": target["processDigest"],
        },
    }
    inspected = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
    )
    materialization = observe_candidate_materialization(
        candidate_root,
        request,
        target_process_root=target_process_root,
        expected_tree=inspected["tree"],
    )
    if list(inspected["lock"].skills) != intent["candidate"]["selectedSkills"]:
        raise ContractError("bootstrap candidate selected skills mismatch")
    if _file_digest(candidate_root / "requirements" / "process.in") != intent[
        "candidate"
    ]["requirementsInputSha256"]:
        raise ContractError("bootstrap candidate requirements input mismatch")
    if _file_digest(candidate_root / "requirements" / "process.txt") != intent[
        "candidate"
    ]["requirementsLockSha256"]:
        raise ContractError("bootstrap candidate requirements lock mismatch")
    if _file_digest(candidate_root / ".process" / "project.json") != intent[
        "candidate"
    ]["projectManifestSha256"]:
        raise ContractError("bootstrap candidate project manifest mismatch")
    if _validate_action_pin_changes(
        candidate_root,
        base_checkpoint=protected_base,
        head_checkpoint=inspected["checkpoint"],
        declarations=intent["candidate"]["actionPins"],
    ) != intent["candidate"]["actionPinsSha256"]:
        raise ContractError("bootstrap candidate action pin set mismatch")
    migration_digest = intent["candidate"]["projectMigrationSha256"]
    migration_path = (
        candidate_root
        / ".process"
        / "adoption-migrations"
        / f"{target['version']}.json"
    )
    if migration_digest is None:
        if migration_path.exists():
            raise ContractError("bootstrap candidate contains an undeclared project migration")
    elif _file_digest(migration_path) != migration_digest:
        raise ContractError("bootstrap candidate project migration mismatch")
    parents = _git_output(
        candidate_root,
        ["rev-list", "--parents", "-n", "1", inspected["checkpoint"]],
        label="inspect bootstrap candidate parent",
        maximum=256,
    ).decode("ascii").split()
    if len(parents) != 2 or parents[1] != protected_base:
        raise ContractError("bootstrap candidate must be one exact commit on protected base")
    return {
        "status": "passed",
        "project": policy["project"],
        "repository": policy["repository"],
        "policySha256": _file_digest(policy_path),
        "intentSha256": _file_digest(intent_path),
        "authorizationSha256": _file_digest(
            authorization_path, maximum=8_000_000
        ),
        "baseCheckpoint": protected_base,
        "headCheckpoint": inspected["checkpoint"],
        "headTree": inspected["tree"],
        "checkContext": policy["workflow"]["checkContext"],
        "validationService": validation_service,
        "targetVersion": target["version"],
        "targetCommit": target["commit"],
        "verifierCommit": policy["verifier"]["commit"],
        "materialization": materialization,
        "grantsMerge": True,
        "postMergeMutation": False,
    }


def create_bootstrap_adoption_consumption(
    candidate_root: Path,
    validation: dict[str, Any],
    *,
    merge_checkpoint: str,
    validation_artifact: dict[str, Any],
    validation_service: dict[str, Any],
    consumed_at: str,
) -> dict[str, Any]:
    candidate_root = candidate_root.resolve(strict=True)
    merge_checkpoint = _git_oid(merge_checkpoint, "merge checkpoint")
    merge_tree = _git_output(
        candidate_root,
        ["rev-parse", "--verify", f"{merge_checkpoint}^{{tree}}"],
        label="resolve bootstrap merge tree",
        maximum=128,
    ).decode("ascii").strip()
    if merge_tree != validation.get("headTree"):
        raise ContractError("protected merge tree does not match validated candidate")
    merge_parents = _git_output(
        candidate_root,
        ["rev-list", "--parents", "-n", "1", merge_checkpoint],
        label="inspect protected transition merge parents",
        maximum=256,
    ).decode("ascii").split()
    if len(merge_parents) != 2 or merge_parents[1] != validation.get(
        "baseCheckpoint"
    ):
        raise ContractError("protected merge is not rooted at the validated base")
    observed_service = _validate_transition_consumption_service(
        validation_service,
        expected=validation.get("validationService"),
        validation=validation,
        artifact=validation_artifact,
    )
    consumption = validate_bootstrap_adoption_consumption(
        {
            "schemaVersion": 1,
            "kind": BOOTSTRAP_CONSUMPTION_KIND,
            "project": validation["project"],
            "repository": validation["repository"],
            "policySha256": validation["policySha256"],
            "intentSha256": validation["intentSha256"],
            "authorizationSha256": validation["authorizationSha256"],
            "baseCheckpoint": validation["baseCheckpoint"],
            "headCheckpoint": validation["headCheckpoint"],
            "headTree": validation["headTree"],
            "mergeCheckpoint": merge_checkpoint,
            "checkContext": validation["checkContext"],
            "validationArtifact": validation_artifact,
            "validationService": observed_service,
            "consumedAt": consumed_at,
        }
    )
    require_bootstrap_adoption_consumption_semantics(consumption)
    return consumption
