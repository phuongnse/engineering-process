from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

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
    names: list[str] = []
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
        names.append(name)
        result.append(
            {
                "name": name,
                "sha256": _digest(item["sha256"], f"{item_path}.sha256"),
                "sizeBytes": size,
            }
        )
    if names != sorted(set(names)):
        raise ContractError(f"{path}: must be sorted by name and unique")
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
    if target["tag"] != f"v{target_version}":
        raise ContractError(f"{target_path}.tag: must be v{target_version}")
    if target_version == source_authority["version"]:
        raise ContractError(f"{target_path}.version: must differ from source authority")

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
    if not selected_skills or selected_skills != sorted(set(selected_skills)):
        raise ContractError(
            f"{candidate_path}.selectedSkills: must be non-empty, sorted, and unique"
        )
    changed_paths = [
        _relative_path(item, f"{candidate_path}.expectedChangedPaths[{index}]")
        for index, item in enumerate(candidate["expectedChangedPaths"])
    ] if isinstance(candidate["expectedChangedPaths"], list) else []
    if (
        not changed_paths
        or len(changed_paths) > MAX_TRANSITION_PATHS
        or changed_paths != sorted(set(changed_paths))
    ):
        raise ContractError(
            f"{candidate_path}.expectedChangedPaths: must be non-empty, sorted, unique, and bounded"
        )
    migration_digest = candidate["projectMigrationSha256"]
    if migration_digest is not None:
        _digest(migration_digest, f"{candidate_path}.projectMigrationSha256")

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
            "expectedChangedPaths": changed_paths,
            "expiresAt": _timestamp(candidate["expiresAt"], f"{candidate_path}.expiresAt"),
        },
        "registeredBy": _actor(value["registeredBy"], f"{path}.registeredBy"),
        "registeredAt": _timestamp(value["registeredAt"], f"{path}.registeredAt"),
    }


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
    if changed_paths != sorted(set(changed_paths)) or len(changed_paths) > MAX_TRANSITION_PATHS:
        raise ContractError(f"{candidate_path}.changedPaths: must be sorted, unique, and bounded")

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
        required={"status", "complete", "idempotent", "rollbackStatus", "issues"},
        path=materialization_path,
    )
    issues = materialization["issues"]
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise ContractError(f"{materialization_path}.issues: must be an array of strings")
    if (
        materialization["status"] != "passed"
        or materialization["complete"] is not True
        or materialization["idempotent"] is not True
        or materialization["rollbackStatus"] != "clean"
        or issues
    ):
        raise ContractError(f"{materialization_path}: candidate must be complete, idempotent, and clean")

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
            "tree": _git_oid(candidate["tree"], f"{candidate_path}.tree"),
            "workspaceFingerprint": _digest(candidate["workspaceFingerprint"], f"{candidate_path}.workspaceFingerprint"),
            "workingTreeDirty": False,
            "changedPaths": changed_paths,
        },
        "bindings": validated_bindings,
        "materialization": {
            "status": "passed",
            "complete": True,
            "idempotent": True,
            "rollbackStatus": "clean",
            "issues": [],
        },
        "generatedBy": _actor(value["generatedBy"], f"{path}.generatedBy"),
        "generatedAt": _timestamp(value["generatedAt"], f"{path}.generatedAt"),
    }


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
    if target["tag"] != f"v{target_version}":
        raise ContractError(f"{target_path}.tag: must be v{target_version}")
    candidate_path = f"{path}.candidate"
    candidate = _object(value["candidate"], candidate_path)
    _exact_keys(
        candidate,
        required={"requirementsInputSha256", "requirementsLockSha256", "projectManifestSha256", "projectMigrationSha256", "actionPinsSha256", "selectedSkills", "expectedChangedPaths"},
        path=candidate_path,
    )
    selected = _string_list(candidate["selectedSkills"], f"{candidate_path}.selectedSkills", pattern=PROFILE_PATTERN, maximum=256)
    if not selected or selected != sorted(set(selected)):
        raise ContractError(f"{candidate_path}.selectedSkills: must be non-empty, sorted, and unique")
    changed = [_relative_path(item, f"{candidate_path}.expectedChangedPaths[{index}]") for index, item in enumerate(candidate["expectedChangedPaths"])] if isinstance(candidate["expectedChangedPaths"], list) else []
    if not changed or changed != sorted(set(changed)) or len(changed) > MAX_TRANSITION_PATHS:
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
            "selectedSkills": selected,
            "expectedChangedPaths": changed,
        },
        "policySha256": _digest(value["policySha256"], f"{path}.policySha256"),
        "ownerResolutionSha256": _digest(value["ownerResolutionSha256"], f"{path}.ownerResolutionSha256"),
        "expiresAt": _timestamp(value["expiresAt"], f"{path}.expiresAt"),
    }


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
    if target["tag"] != f"v{target_version}":
        raise ContractError(f"{target_path}.tag: must be v{target_version}")
    verifier_path = f"{path}.verifier"
    verifier = _object(value["verifier"], verifier_path)
    _exact_keys(verifier, required={"repository", "commit", "entrypoint"}, path=verifier_path)
    workflow_path = f"{path}.workflow"
    workflow = _object(value["workflow"], workflow_path)
    _exact_keys(workflow, required={"repository", "path", "checkContext"}, path=workflow_path)
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
    if verifier_commit == target["commit"]:
        raise ContractError(f"{verifier_path}.commit: target release cannot be its own verifier")
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
        },
        "merge": merge,
        "authorizationTransport": transport,
        "singleUse": True,
        "postMergeMutation": False,
        "expiresAt": _timestamp(value["expiresAt"], f"{path}.expiresAt"),
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
            "headTree", "mergeCheckpoint", "checkContext", "consumedAt"
        },
        path=path,
    )
    common = _bootstrap_common(value, path, kind=BOOTSTRAP_CONSUMPTION_KIND)
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
        "consumedAt": _timestamp(value["consumedAt"], f"{path}.consumedAt"),
    }


def _file_digest(path: Path, *, maximum: int = 1_000_000) -> str:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read transition input: {error}") from error
    if len(content) > maximum:
        raise ContractError(f"{path}: transition input exceeds {maximum} bytes")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _git_output(root: Path, arguments: list[str], *, label: str, maximum: int = 1_000_000) -> bytes:
    result = run_git(root, arguments, label=label, timeout_seconds=30, max_stdout_bytes=maximum)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"{label} failed" + (f": {detail}" if detail else ""))
    return result.stdout


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


def validate_registered_candidate(
    candidate_root: Path,
    request_document: dict[str, Any],
    evidence_document: dict[str, Any],
    *,
    target_process_root: Path,
) -> dict[str, Any]:
    request = validate_authority_transition_request(request_document)
    evidence = validate_authority_transition_evidence(evidence_document)
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
    if evidence["bindings"]["actionPinsSha256"] != _action_pins_digest(candidate_root):
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
    _require_not_expired(request["candidate"]["expiresAt"], label="authority-transition request")
    inspected = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
    )
    repeated = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
    )
    if any(
        inspected[name] != repeated[name]
        for name in ("checkpoint", "tree", "workspaceFingerprint", "changedPaths")
    ):
        raise ContractError("authority-transition candidate check is not idempotent")
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
            "actionPinsSha256": _action_pins_digest(candidate_root),
        },
        "materialization": {
            "status": "passed",
            "complete": True,
            "idempotent": True,
            "rollbackStatus": "clean",
            "issues": [],
        },
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
    validate_authority_transition_evidence(evidence)
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
    try:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise ContractError(f"cannot read transition artifact {path}: {error}") from error
    if len(content) > maximum:
        raise ContractError(f"transition artifact exceeds {maximum} bytes: {path}")
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"transition artifact changed while reading: {path}")
    return content


def _require_not_expired(value: str, *, label: str) -> None:
    expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expires <= datetime.now(UTC):
        raise ContractError(f"{label} has expired")


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
        },
        "target": {
            "version": target["version"],
            "processDigest": target["processDigest"],
        },
    }
    inspected = inspect_transition_candidate(
        candidate_root, request, target_process_root=target_process_root
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
    if _action_pins_digest(candidate_root) != intent["candidate"]["actionPinsSha256"]:
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
        "targetVersion": target["version"],
        "targetCommit": target["commit"],
        "verifierCommit": policy["verifier"]["commit"],
        "grantsMerge": True,
        "postMergeMutation": False,
    }


def create_bootstrap_adoption_consumption(
    candidate_root: Path,
    validation: dict[str, Any],
    *,
    merge_checkpoint: str,
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
    return validate_bootstrap_adoption_consumption(
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
            "consumedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )
