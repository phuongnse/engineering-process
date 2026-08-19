from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .contracts import ContractError, read_json, validate_release


VERSION_TAG_PATTERN = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def _git(project_root: Path, arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=project_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"git {' '.join(arguments)} failed: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"git {' '.join(arguments)} failed"
            + (f": {detail}" if detail else "")
        )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ContractError("git output must be UTF-8") from error


def _project_version(project_root: Path) -> str:
    path = project_root / "pyproject.toml"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if len(data) > 1_000_000:
        raise ContractError(f"{path}: exceeds the 1 MB limit")
    try:
        document = tomllib.loads(data.decode("utf-8"))
        version = document["project"]["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise ContractError(f"{path}: cannot read project.version: {error}") from error
    if not isinstance(version, str):
        raise ContractError(f"{path}: project.version must be a string")
    return version


def validate_release_checkpoint(
    project_root: Path,
    *,
    tag: str,
    commit: str,
    main_ref: str,
) -> dict[str, Any]:
    release_path = project_root / "release.json"
    release = validate_release(read_json(release_path), str(release_path))
    package_version = _project_version(project_root)
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

    checkpoint = _git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
    )
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
        "checkpoint": checkpoint,
        "mainCheckpoint": main_checkpoint,
        "previousTag": previous_tag,
        "previousCheckpoint": previous_checkpoint,
        "version": release.version,
        "classification": release.classification,
        "compatibility": release.compatibility,
        "schemaImpact": release.schema_impact,
        "migration": release.migration,
    }
