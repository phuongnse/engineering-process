from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    MAX_CONTRACT_ITEMS,
    MAX_JSON_BYTES,
    ReleaseChange,
    derive_release_version,
    read_json,
    validate_process_lock,
    validate_improvement_catalog,
    validate_release,
    validate_release_change,
)
from .release import _git, _project_metadata, _runtime_version


MAX_RELEASE_CHANGE_BYTES = 8_000_000
SCHEMA_IMPACT_ORDER = {"unchanged": 0, "additive": 1, "breaking": 2}


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _canonical_json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _bounded_file(path: Path, *, label: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label}: must be a regular non-symlink file")
        if before.st_size > MAX_JSON_BYTES:
            raise ContractError(f"{label}: exceeds {MAX_JSON_BYTES} bytes")
        data = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"{label}: cannot read {path}: {error}") from error
    if (
        len(data) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"{label}: changed while reading")
    return data


def load_release_changes(changes_dir: Path) -> list[tuple[Path, ReleaseChange]]:
    if not changes_dir.is_dir() or changes_dir.is_symlink():
        raise ContractError("release changes must be a non-symlink directory")
    entries: list[tuple[Path, ReleaseChange]] = []
    total_bytes = 0
    try:
        candidates = sorted(os.scandir(changes_dir), key=lambda item: item.name)
    except OSError as error:
        raise ContractError(f"cannot enumerate release changes: {error}") from error
    for candidate in candidates:
        try:
            candidate_stat = candidate.stat(follow_symlinks=False)
        except OSError as error:
            raise ContractError(
                f"cannot inspect release change {candidate.name}: {error}"
            ) from error
        if stat.S_ISDIR(candidate_stat.st_mode):
            raise ContractError(
                f"release changes must not contain directories: {candidate.name}"
            )
        if candidate.name == "README.md" and stat.S_ISREG(candidate_stat.st_mode):
            continue
        if not candidate.name.endswith(".json"):
            raise ContractError(
                f"unexpected release change entry: {candidate.name}"
            )
        if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISREG(
            candidate_stat.st_mode
        ):
            raise ContractError(
                f"release change must be a regular non-symlink file: {candidate.name}"
            )
        if len(entries) >= MAX_CONTRACT_ITEMS:
            raise ContractError(
                f"release changes exceed {MAX_CONTRACT_ITEMS} items"
            )
        total_bytes += candidate_stat.st_size
        if total_bytes > MAX_RELEASE_CHANGE_BYTES:
            raise ContractError(
                f"release changes exceed {MAX_RELEASE_CHANGE_BYTES} bytes"
            )
        path = Path(candidate.path)
        change = validate_release_change(read_json(path), str(path))
        if candidate.name != f"{change.identifier}.json":
            raise ContractError(
                f"release change filename must be {change.identifier}.json"
            )
        entries.append((path, change))
    if not entries:
        raise ContractError("release preparation requires at least one change fragment")
    identifiers = [change.identifier for _path, change in entries]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("release change ids must be unique")
    if identifiers != sorted(identifiers):
        raise ContractError("release changes must be sorted by id")
    return entries


def _replace_project_version(data: bytes, previous: str, version: str) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("pyproject.toml must be UTF-8") from error
    lines = text.splitlines(keepends=True)
    in_project = False
    matches: list[int] = []
    version_pattern = re.compile(r'^version\s*=\s*"([^"\r\n]+)"\s*$')
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project and version_pattern.fullmatch(line.rstrip("\r\n")):
            matches.append(index)
    if len(matches) != 1:
        raise ContractError(
            "pyproject.toml must declare exactly one project.version string"
        )
    index = matches[0]
    match = version_pattern.fullmatch(lines[index].rstrip("\r\n"))
    assert match is not None
    if match.group(1) != previous:
        raise ContractError(
            "pyproject.toml project.version does not match the current release"
        )
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines[index] = f'version = "{version}"{newline}'
    return "".join(lines).encode("utf-8")


def _replace_runtime_version(
    data: bytes, *, variable: str, previous: str, version: str
) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("runtime version source must be UTF-8") from error
    pattern = re.compile(
        rf'(?m)^(?P<prefix>{re.escape(variable)}[ \t]*=[ \t]*)"{re.escape(previous)}"(?P<suffix>[ \t]*)(?P<carriage_return>\r?)$'
    )
    updated, count = pattern.subn(
        lambda match: (
            f'{match.group("prefix")}"{version}"{match.group("suffix")}'
            f'{match.group("carriage_return")}'
        ),
        text,
    )
    if count != 1:
        raise ContractError(
            f"runtime version source must assign {variable} to {previous} exactly once"
        )
    return updated.encode("utf-8")


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ContractError(f"refusing to write through symlink directory: {path.parent}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ContractError(f"cannot update release candidate file {path}: {error}") from error


def _remove_runtime_bytecode(runtime_path: Path) -> None:
    cache = runtime_path.parent / "__pycache__"
    if not cache.exists():
        return
    if not cache.is_dir() or cache.is_symlink():
        raise ContractError("runtime bytecode cache must be a non-symlink directory")
    prefix = f"{runtime_path.stem}."
    inspected = 0
    try:
        with os.scandir(cache) as entries:
            for entry in entries:
                inspected += 1
                if inspected > 256:
                    raise ContractError("runtime bytecode cache exceeds 256 entries")
                if not (
                    entry.name.startswith(prefix)
                    and entry.name.endswith((".pyc", ".pyo"))
                ):
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(
                    entry_stat.st_mode
                ):
                    raise ContractError(
                        f"runtime bytecode cache entry is not a regular file: {entry.name}"
                    )
                Path(entry.path).unlink()
    except OSError as error:
        raise ContractError(f"cannot invalidate runtime bytecode cache: {error}") from error


def _resolved_improvement_catalog(
    project_root: Path,
    *,
    change_ids: set[str],
    version: str,
) -> tuple[Path, bytes] | None:
    path = project_root / "improvement-catalog.json"
    if not path.is_file():
        return None
    document = read_json(path)
    validate_improvement_catalog(document, str(path))
    changed = False
    for entry in document["entries"]:
        active_change_id = entry["activeChangeId"]
        if entry["status"] != "active" or active_change_id not in change_ids:
            continue
        entry["status"] = "resolved"
        entry["lastResolution"] = {
            "changeId": active_change_id,
            "version": version,
        }
        entry["activeChangeId"] = None
        changed = True
    if not changed:
        return None
    validate_improvement_catalog(document, "generated improvement catalog")
    return path, _canonical_json_bytes(document)


def _release_lifecycle_documents(
    *,
    project: str,
    version: str,
    comparison_base: str,
) -> tuple[bytes, bytes]:
    change_id = f"release-{version.replace('.', '-')}"
    contract = {
        "schemaVersion": 3,
        "id": change_id,
        "summary": f"Authorize and publish {project} {version}",
        "source": "Automatically aggregated reviewed release-change fragments",
        "comparisonBase": comparison_base,
        "specification": {
            "kind": "change-contract",
            "reference": "release.json and VERSIONING.md",
            "rationale": "The release contract owns the exact public change set and identity surfaces.",
        },
        "risk": "high",
        "affectedProjects": [project],
        "acceptanceCriteria": [
            {
                "id": "ac-identity",
                "outcome": "Every release identity surface matches the derived version and reviewed source checkpoint.",
            },
            {
                "id": "ac-publication",
                "outcome": "The immutable tag, release assets, attestation, and PyPI distributions contain the verified bytes.",
            },
            {
                "id": "ac-review",
                "outcome": "Required profiles pass and an independent reviewer approves the exact candidate with no open finding.",
            },
        ],
        "requiredProfiles": ["development", "review"],
        "quality": {
            "standard": "production-v1",
            "assessments": [
                {
                    "dimension": "compatibility",
                    "status": "applicable",
                    "rationale": "The release must preserve its declared compatibility and exact identity surfaces.",
                    "criteria": ["ac-identity"],
                },
                {
                    "dimension": "correctness",
                    "status": "applicable",
                    "rationale": "The derived identity and published bytes must match the reviewed release contract.",
                    "criteria": ["ac-identity", "ac-publication"],
                },
                {
                    "dimension": "maintainability",
                    "status": "applicable",
                    "rationale": "Independent review verifies the generated contract and deterministic release owner.",
                    "criteria": ["ac-review"],
                },
                {
                    "dimension": "observability",
                    "status": "applicable",
                    "rationale": "Release evidence and attestations expose the exact publication outcome.",
                    "criteria": ["ac-publication"],
                },
                {
                    "dimension": "operability",
                    "status": "applicable",
                    "rationale": "The reviewed release must publish and recover through the declared automated workflow.",
                    "criteria": ["ac-publication"],
                },
                {
                    "dimension": "performance",
                    "status": "not-applicable",
                    "rationale": "Release authorization adds no product runtime or scaling behavior.",
                    "criteria": [],
                },
                {
                    "dimension": "privacy",
                    "status": "not-applicable",
                    "rationale": "Release metadata contains no personal or sensitive data processing.",
                    "criteria": [],
                },
                {
                    "dimension": "reliability",
                    "status": "applicable",
                    "rationale": "Immutable assets and exact external-state checks must fail closed and recover deterministically.",
                    "criteria": ["ac-publication"],
                },
                {
                    "dimension": "security",
                    "status": "applicable",
                    "rationale": "Independent review and merge remain the only publication authorization boundary.",
                    "criteria": ["ac-review"],
                },
                {
                    "dimension": "supply-chain",
                    "status": "applicable",
                    "rationale": "The release binds source, version, artifacts, attestations, and registry bytes.",
                    "criteria": ["ac-identity", "ac-publication"],
                },
            ],
        },
        "signOff": {
            "required": False,
            "status": "not-required",
            "evidence": None,
        },
    }
    contract_bytes = _canonical_json_bytes(contract)
    contract_digest = f"sha256:{hashlib.sha256(contract_bytes).hexdigest()}"
    plan = {
        "schemaVersion": 2,
        "changeId": change_id,
        "contractDigest": contract_digest,
        "approach": "Validate the generated release contract and every declared surface, build from the exact reviewed tree, and permit publication only after independent approval and merge.",
        "workItems": [
            {
                "id": "work-release",
                "outcome": "Verify and publish the exact generated release candidate",
                "affectedPaths": ["release.json", "pyproject.toml", "engineering_process/__init__.py"],
                "verificationProfiles": ["development", "review"],
            }
        ],
        "acceptancePlan": [
            {
                "criterionId": criterion,
                "workItems": ["work-release"],
                "verificationProfiles": ["development", "review"],
            }
            for criterion in ("ac-identity", "ac-publication", "ac-review")
        ],
        "risks": [
            {
                "risk": "Publication could use source or artifacts other than the reviewed candidate.",
                "mitigation": "Bind review evidence, merge-tree equality, tag, release assets, attestation, and PyPI upload to one immutable identity.",
            }
        ],
        "openDecisions": [],
    }
    return contract_bytes, _canonical_json_bytes(plan)


def prepare_release_candidate(
    project_root: Path,
    *,
    changes_dir: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    requested_changes_dir = changes_dir or project_root / "release-changes"
    if requested_changes_dir.is_symlink():
        raise ContractError("release changes directory must not be a symlink")
    try:
        changes_dir = requested_changes_dir.resolve(strict=True)
    except OSError as error:
        raise ContractError(
            f"cannot resolve release changes directory: {error}"
        ) from error
    try:
        changes_dir.relative_to(project_root)
    except ValueError as error:
        raise ContractError("release changes directory must stay within the project") from error
    entries = load_release_changes(changes_dir)
    release_path = project_root / "release.json"
    previous_release = validate_release(
        read_json(release_path), str(release_path)
    )
    if (
        previous_release.package_name is None
        or previous_release.distribution_name is None
        or previous_release.runtime_version_file is None
        or previous_release.runtime_version_variable is None
    ):
        raise ContractError("release candidate requires an identity-bearing prior release")
    package_name, package_version = _project_metadata(project_root)
    if package_name != previous_release.package_name or package_version != previous_release.version:
        raise ContractError("current package metadata does not match the prior release contract")
    runtime_version = _runtime_version(
        project_root,
        previous_release.runtime_version_file,
        previous_release.runtime_version_variable,
    )
    if runtime_version != previous_release.version:
        raise ContractError("current runtime version does not match the prior release contract")
    lock_path = project_root / ".process" / "process.lock"
    lock = validate_process_lock(read_json(lock_path), str(lock_path))
    if lock.version != previous_release.version:
        raise ContractError(
            "self-adoption must pin the latest public release before preparing another release"
        )
    if previous_release.provenance_mode == "bootstrap-history":
        provenance_mode = "bootstrap-authority"
    elif previous_release.provenance_mode in {"bootstrap-authority", "governed"}:
        provenance_mode = "governed"
    else:
        raise ContractError("legacy release history cannot prepare an automated release")

    plan = derive_release_version(
        previous_release.version,
        [change.change_type for _path, change in entries],
    )
    version = plan.version
    tag = f"v{version}"
    distribution = previous_release.distribution_name
    artifacts = sorted(
        [
            f"{distribution}-{version}-py3-none-any.whl",
            f"{distribution}-{version}.tar.gz",
        ]
    )
    schema_impact = max(
        (change.schema_impact for _path, change in entries),
        key=SCHEMA_IMPACT_ORDER.__getitem__,
    )
    migrations = [
        f"{change.identifier}: {change.migration}"
        for _path, change in entries
        if change.migration is not None
    ]
    migration = "; ".join(migrations) if migrations else None
    if migration is not None and len(migration) > 1000:
        raise ContractError("combined release migration guidance exceeds 1000 characters")
    change_id = f"release-{version.replace('.', '-')}"
    receipt_asset = (
        f"{package_name}-{tag}-evidence.json"
        if provenance_mode == "governed"
        else None
    )
    authorization_asset = (
        f"{package_name}-{tag}-bootstrap-authorization.json"
        if provenance_mode == "bootstrap-authority"
        else None
    )
    release_document: dict[str, Any] = {
        "schemaVersion": 3,
        "previousVersion": previous_release.version,
        "version": version,
        "classification": plan.classification,
        "compatibility": plan.compatibility,
        "schemaImpact": schema_impact,
        "migration": migration,
        "identity": {
            "package": package_name,
            "distribution": distribution,
            "tag": tag,
            "releaseName": tag,
            "runtimeVersion": {
                "path": previous_release.runtime_version_file,
                "variable": previous_release.runtime_version_variable,
            },
            "artifacts": artifacts,
            "receiptAsset": receipt_asset,
            "authorizationAsset": authorization_asset,
        },
        "provenance": {
            "mode": provenance_mode,
            "statement": (
                "A reviewed bootstrap-authority Release PR authorizes the first public lifecycle authority."
                if provenance_mode == "bootstrap-authority"
                else "The public N-1 lifecycle receipt and reviewed Release PR authorize this release."
            ),
            "lifecycleReceipt": (
                {
                    "asset": receipt_asset,
                    "project": package_name,
                    "changeId": change_id,
                    "cycle": 1,
                }
                if provenance_mode == "governed"
                else None
            ),
        },
        "changes": [
            {
                "id": change.identifier,
                "type": change.change_type,
                "surfaces": list(change.surfaces),
                "rationale": change.rationale,
            }
            for _path, change in entries
        ],
    }
    validate_release(release_document, "generated release.json")
    previous_checkpoint = _git(
        project_root,
        ["rev-list", "-n", "1", f"v{previous_release.version}", "--"],
    )
    lifecycle_contract, lifecycle_plan = _release_lifecycle_documents(
        project=package_name,
        version=version,
        comparison_base=previous_checkpoint,
    )

    pyproject_path = project_root / "pyproject.toml"
    runtime_path = project_root / previous_release.runtime_version_file
    pyproject_bytes = _replace_project_version(
        _bounded_file(pyproject_path, label="pyproject.toml"),
        previous_release.version,
        version,
    )
    runtime_bytes = _replace_runtime_version(
        _bounded_file(runtime_path, label="runtime version source"),
        variable=previous_release.runtime_version_variable,
        previous=previous_release.version,
        version=version,
    )
    outputs = {
        release_path: _json_bytes(release_document),
        pyproject_path: pyproject_bytes,
        runtime_path: runtime_bytes,
        project_root / ".release" / "change.json": lifecycle_contract,
        project_root / ".release" / "plan.json": lifecycle_plan,
    }
    catalog_output = _resolved_improvement_catalog(
        project_root,
        change_ids={change.identifier for _path, change in entries},
        version=version,
    )
    if catalog_output is not None:
        catalog_path, catalog_bytes = catalog_output
        outputs[catalog_path] = catalog_bytes
    _remove_runtime_bytecode(runtime_path)
    for path, data in outputs.items():
        _atomic_replace(path, data)
    for path, _change in entries:
        try:
            path.unlink()
        except OSError as error:
            raise ContractError(f"cannot consume release change {path}: {error}") from error
    return {
        "previousVersion": previous_release.version,
        "version": version,
        "tag": tag,
        "classification": plan.classification,
        "compatibility": plan.compatibility,
        "schemaImpact": schema_impact,
        "provenanceMode": provenance_mode,
        "changeId": change_id,
        "changes": [change.identifier for _path, change in entries],
        "artifacts": artifacts,
        "receiptAsset": receipt_asset,
        "authorizationAsset": authorization_asset,
    }


def render_release_pull_request(project_root: Path, *, approved: bool) -> str:
    release_path = project_root.resolve(strict=True) / "release.json"
    release = validate_release(read_json(release_path), str(release_path))
    if release.provenance_mode not in {"bootstrap-authority", "governed"}:
        raise ContractError("Release PR body requires a publishable release candidate")
    state = "satisfied" if approved else "pending"
    checkbox = "x" if approved else " "
    review_text = (
        "The exact candidate has completed independent review with no open required finding."
        if approved
        else "The exact candidate awaits independent review; release-authorization remains pending."
    )
    return f"""<!-- engineering-process:pr-description:start -->
## Summary

Publish `{release.package_name}` `{release.version}` from one generated, reviewed Release PR.

## Contract and scope

`release.json` owns the exact ordered public change set, derived SemVer, immutable identities, and `{release.provenance_mode}` evidence mode.

## Impact and risk

Merging this PR is the sole release authorization. Post-merge automation may create tag `{release.tag}`, immutable release assets, and the PyPI publication; it may not choose a different version or artifact identity.

## Verification

Required `development` and `review` profiles, release identity validation, exact reviewed-tree evidence, and the complete repository matrix gate this candidate.

## Independent review

{review_text}

## Requirements and rules followed

- [{checkbox}] **Scope and contract** — accepted scope is implemented without unapproved expansion. [status: {state}]
- [{checkbox}] **Verification evidence** — required current profiles pass on the published checkpoint. [status: {state}]
- [{checkbox}] **Independent review** — a separate reviewer approved the published checkpoint with no open required finding. [status: {state}]
<!-- engineering-process:pr-description:end -->
"""
