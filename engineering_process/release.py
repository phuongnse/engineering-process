from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    read_json,
    validate_process_lock,
    validate_release,
)
from .evidence import validate_receipt
from .git import run_git


VERSION_TAG_PATTERN = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def _git(project_root: Path, arguments: list[str]) -> str:
    label = f"git {' '.join(arguments)}"
    completed = run_git(
        project_root,
        arguments,
        label=label,
        timeout_seconds=30,
        max_stdout_bytes=256_000,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"{label} failed"
            + (f": {detail}" if detail else "")
        )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError("git output must be UTF-8") from error


def _project_metadata(project_root: Path) -> tuple[str, str]:
    path = project_root / "pyproject.toml"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if len(data) > 1_000_000:
        raise ContractError(f"{path}: exceeds the 1 MB limit")
    try:
        document = tomllib.loads(data.decode("utf-8"))
        name = document["project"]["name"]
        version = document["project"]["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise ContractError(f"{path}: cannot read project.version: {error}") from error
    if not isinstance(name, str) or not isinstance(version, str):
        raise ContractError(f"{path}: project.name and project.version must be strings")
    return name, version


def _runtime_version(project_root: Path, relative_path: str, variable: str) -> str:
    path = project_root / relative_path
    try:
        resolved_root = project_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        data = path.read_bytes()
    except (OSError, ValueError) as error:
        raise ContractError(f"{path}: cannot read runtime version source: {error}") from error
    if len(data) > 1_000_000:
        raise ContractError(f"{path}: exceeds the 1 MB limit")
    try:
        document = ast.parse(data.decode("utf-8"), filename=str(path))
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ContractError(f"{path}: cannot parse runtime version source: {error}") from error
    matches: list[str] = []
    for statement in document.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == variable
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            matches.append(statement.value.value)
    if len(matches) != 1:
        raise ContractError(
            f"{path}: must assign {variable} to exactly one string literal"
        )
    return matches[0]


def validate_release_checkpoint(
    project_root: Path,
    *,
    tag: str,
    release_name: str,
    commit: str,
    main_ref: str,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    release_path = project_root / "release.json"
    release = validate_release(read_json(release_path), str(release_path))
    if release.provenance_mode != "governed":
        raise ContractError(
            "publication requires a governed release contract; bootstrap history "
            "and legacy contracts are read-only"
        )
    package_name, package_version = _project_metadata(project_root)
    if package_version != release.version:
        raise ContractError(
            f"{release_path}: version {release.version} does not match "
            f"pyproject.toml project.version {package_version}"
        )

    expected_tag = f"v{release.version}"
    if tag != expected_tag:
        raise ContractError(
            f"release tag {tag!r} does not match contract version {expected_tag!r}"
        )
    if release.tag is not None and tag != release.tag:
        raise ContractError(
            f"release tag {tag!r} does not match release identity {release.tag!r}"
        )
    if release.release_name is not None and release_name != release.release_name:
        raise ContractError(
            f"release name {release_name!r} does not match release identity "
            f"{release.release_name!r}"
        )
    if release.package_name is not None and package_name != release.package_name:
        raise ContractError(
            f"pyproject.toml project.name {package_name!r} does not match release "
            f"identity {release.package_name!r}"
        )
    if release.runtime_version_file is not None:
        assert release.runtime_version_variable is not None
        runtime_version = _runtime_version(
            project_root,
            release.runtime_version_file,
            release.runtime_version_variable,
        )
        if runtime_version != release.version:
            raise ContractError(
                f"runtime version {runtime_version!r} does not match release "
                f"version {release.version!r}"
            )

    receipt: dict[str, Any] | None = None
    if release.provenance_mode == "governed":
        if receipt_path is None:
            raise ContractError("governed release requires a lifecycle receipt")
        if release.receipt_asset != receipt_path.name:
            raise ContractError(
                f"receipt filename {receipt_path.name!r} does not match release identity "
                f"{release.receipt_asset!r}"
            )
        receipt = validate_receipt(receipt_path)
        if (
            receipt["project"] != release.receipt_project
            or receipt["changeId"] != release.receipt_change_id
            or receipt["cycle"] != release.receipt_cycle
            or receipt["checkpoint"] != commit
        ):
            raise ContractError(
                "lifecycle receipt change, cycle, or checkpoint does not match release"
            )
        lock_path = project_root / ".process" / "process.lock"
        lock = validate_process_lock(read_json(lock_path), str(lock_path))
        if (
            receipt["processVersion"] != lock.version
            or receipt["processDigest"] != lock.digest
        ):
            raise ContractError(
                "lifecycle receipt authority does not match the pinned process lock"
            )
    elif receipt_path is not None:
        raise ContractError("bootstrap or legacy releases must not claim a lifecycle receipt")

    checkpoint = _git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
    )
    head_checkpoint = _git(
        project_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
    )
    if head_checkpoint != checkpoint:
        raise ContractError(
            f"release checkout HEAD {head_checkpoint} does not match {checkpoint}"
        )
    worktree = run_git(
        project_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        label="inspect release checkout state",
        timeout_seconds=30,
        max_stdout_bytes=500_000,
    )
    if worktree.returncode != 0 or worktree.stdout:
        raise ContractError("release checkout must be clean at the declared checkpoint")
    main_checkpoint = _git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{main_ref}^{{commit}}"],
    )
    tag_checkpoint = _git(
        project_root,
        ["rev-list", "-n", "1", expected_tag, "--"],
    )
    if tag_checkpoint != checkpoint:
        raise ContractError(
            f"release tag {expected_tag} points to {tag_checkpoint}, not {checkpoint}"
        )
    _git(project_root, ["merge-base", "--is-ancestor", checkpoint, main_checkpoint])

    previous_tag = f"v{release.previous_version}"
    previous_checkpoint = _git(
        project_root,
        ["rev-list", "-n", "1", previous_tag, "--"],
    )
    _git(
        project_root,
        ["merge-base", "--is-ancestor", previous_checkpoint, checkpoint],
    )

    merged_tags = _git(project_root, ["tag", "--merged", checkpoint, "--list", "v*"])
    prior_versions: list[tuple[tuple[int, int, int], str]] = []
    for candidate in merged_tags.splitlines():
        match = VERSION_TAG_PATTERN.fullmatch(candidate)
        if match is None or candidate == expected_tag:
            continue
        prior_versions.append((tuple(int(part) for part in match.groups()), candidate))
    if not prior_versions:
        raise ContractError("release checkpoint has no prior final SemVer tag")
    latest_prior_tag = max(prior_versions)[1]
    if latest_prior_tag != previous_tag:
        raise ContractError(
            f"{release_path}: previousVersion must name latest reachable release "
            f"{latest_prior_tag[1:]}, not {release.previous_version}"
        )

    return {
        "tag": expected_tag,
        "releaseName": release_name,
        "checkpoint": checkpoint,
        "mainCheckpoint": main_checkpoint,
        "previousTag": previous_tag,
        "previousCheckpoint": previous_checkpoint,
        "version": release.version,
        "classification": release.classification,
        "compatibility": release.compatibility,
        "schemaImpact": release.schema_impact,
        "migration": release.migration,
        "artifacts": list(release.artifacts),
        "receiptAsset": release.receipt_asset,
        "provenanceMode": release.provenance_mode,
        "lifecycleReceipt": receipt,
    }
