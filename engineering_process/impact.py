from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import Check, ContractError, Project, ProjectImpact


MAX_CHANGED_PATHS = 10_000
MAX_CHANGED_PATH_BYTES = 2_000_000
IMPACT_FILE_ENV = "ENGINEERING_PROCESS_IMPACT_FILE"


@dataclass(frozen=True)
class ImpactPlan:
    checks: tuple[Check, ...]
    evidence: dict[str, Any]


def _git(root: Path, arguments: list[str], *, label: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"impact scope {label}: git failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"impact scope {label}: git exited {result.returncode}: "
            f"{detail or 'no diagnostic'}"
        )
    return result.stdout


def _commit(root: Path, ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            cwd=root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContractError(f"impact scope resolve {ref}: git failed: {error}") from error
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ContractError(f"impact scope resolve {ref}: invalid Git object id") from error


def _portable_path(encoded: bytes, *, label: str) -> str:
    try:
        path = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(
            f"impact scope {label}: changed paths must use UTF-8"
        ) from error
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != path
    ):
        raise ContractError(
            f"impact scope {label}: Git returned a non-portable path: {path!r}"
        )
    return path


def _paths(output: bytes, *, label: str) -> list[str]:
    if len(output) > MAX_CHANGED_PATH_BYTES:
        raise ContractError(
            f"impact scope {label}: changed-path output exceeds "
            f"{MAX_CHANGED_PATH_BYTES} bytes"
        )
    encoded_paths = [item for item in output.split(b"\0") if item]
    if len(encoded_paths) > MAX_CHANGED_PATHS:
        raise ContractError(
            f"impact scope {label}: changed-path count exceeds {MAX_CHANGED_PATHS}"
        )
    return [_portable_path(item, label=label) for item in encoded_paths]


def _changed_paths(root: Path, merge_base: str, head: str) -> list[str]:
    commands = (
        (
            ["diff", "--name-only", "--no-renames", "-z", f"{merge_base}..{head}", "--"],
            "committed diff",
        ),
        (
            ["diff", "--name-only", "--no-renames", "-z", "--cached", "--"],
            "staged diff",
        ),
        (
            ["diff", "--name-only", "--no-renames", "-z", "--"],
            "working-tree diff",
        ),
        (
            ["ls-files", "--others", "--exclude-standard", "-z", "--"],
            "untracked files",
        ),
    )
    paths: set[str] = set()
    total_bytes = 0
    for arguments, label in commands:
        output = _git(root, arguments, label=label)
        total_bytes += len(output)
        if total_bytes > MAX_CHANGED_PATH_BYTES:
            raise ContractError(
                "impact scope: combined changed-path output exceeds "
                f"{MAX_CHANGED_PATH_BYTES} bytes"
            )
        paths.update(_paths(output, label=label))
        if len(paths) > MAX_CHANGED_PATHS:
            raise ContractError(
                f"impact scope: changed-path count exceeds {MAX_CHANGED_PATHS}"
            )
    return sorted(paths)


def discover_scope(
    root: Path,
    impact: ProjectImpact,
    *,
    base_ref: str | None = None,
) -> dict[str, Any]:
    head = _commit(root, "HEAD")
    if head is None:
        raise ContractError("impact scope: project root has no Git HEAD commit")

    candidates = (base_ref,) if base_ref is not None else impact.base_refs
    selected_ref: str | None = None
    base_commit: str | None = None
    for candidate in candidates:
        resolved = _commit(root, candidate)
        if resolved is not None:
            selected_ref = candidate
            base_commit = resolved
            break
    if selected_ref is None or base_commit is None:
        attempted = ", ".join(candidates)
        qualifier = "explicit " if base_ref is not None else ""
        raise ContractError(
            f"impact scope: {qualifier}base ref is unavailable; tried {attempted}"
        )

    merge_base = _git(
        root,
        ["merge-base", base_commit, head],
        label=f"merge base for {selected_ref}",
    ).decode("ascii").strip()
    if not merge_base:
        raise ContractError(
            f"impact scope: no merge base for {selected_ref} and HEAD"
        )
    return {
        "baseRef": selected_ref,
        "baseCommit": base_commit,
        "headCommit": head,
        "mergeBase": merge_base,
        "changedPaths": _changed_paths(root, merge_base, head),
    }


def _glob_regex(pattern: str) -> re.Pattern[str]:
    expression = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression += "(?:[^/]+/)*"
                    index += 1
                else:
                    expression += ".*"
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        else:
            expression += re.escape(character)
        index += 1
    return re.compile(f"^{expression}$")


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob_regex(pattern).fullmatch(path) for pattern in patterns)


def _affected_components(
    impact: ProjectImpact, direct: set[str]
) -> set[str]:
    affected = set(direct)
    pending = list(sorted(direct))
    while pending:
        identifier = pending.pop(0)
        for downstream in impact.components[identifier].affects:
            if downstream not in affected:
                affected.add(downstream)
                pending.append(downstream)
    return affected


def plan_profile(
    root: Path,
    project: Project,
    profile: str,
    *,
    base_ref: str | None = None,
) -> ImpactPlan:
    checks = project.profiles.get(profile)
    if checks is None:
        available = ", ".join(sorted(project.profiles))
        raise ContractError(
            f"unknown profile {profile}; available profiles: {available}"
        )

    if project.impact is None:
        return ImpactPlan(
            checks=checks,
            evidence={
                "schemaVersion": 1,
                "mode": "full-profile",
                "profile": profile,
                "selectedCheckIds": [check.identifier for check in checks],
                "skippedCheckIds": [],
                "checkSelection": [
                    {
                        "id": check.identifier,
                        "selected": True,
                        "reason": "profile-has-no-impact-contract",
                        "components": [],
                        "matchedComponents": [],
                    }
                    for check in checks
                ],
            },
        )

    scope = discover_scope(root, project.impact, base_ref=base_ref)
    direct: set[str] = set()
    matched_paths: set[str] = set()
    for path in scope["changedPaths"]:
        for component in project.impact.components.values():
            if _matches(path, component.paths):
                direct.add(component.identifier)
                matched_paths.add(path)
    affected = _affected_components(project.impact, direct)
    unmatched = sorted(set(scope["changedPaths"]) - matched_paths)

    selected: list[Check] = []
    skipped: list[str] = []
    selection: list[dict[str, Any]] = []
    for check in checks:
        declared = set(check.components or ())
        matched = sorted(declared & affected)
        if check.components is None:
            is_selected = True
            reason = "unscoped-always-run"
        elif unmatched:
            is_selected = True
            reason = "unmatched-path-fallback"
        elif matched:
            is_selected = True
            reason = "affected-component"
        else:
            is_selected = False
            reason = "no-affected-component"
        if is_selected:
            selected.append(check)
        else:
            skipped.append(check.identifier)
        selection.append(
            {
                "id": check.identifier,
                "selected": is_selected,
                "reason": reason,
                "components": list(check.components or ()),
                "matchedComponents": matched,
            }
        )

    evidence = {
        "schemaVersion": 1,
        "mode": "affected-checks",
        "profile": profile,
        **scope,
        "directlyChangedComponents": sorted(direct),
        "affectedComponents": sorted(affected),
        "unmatchedPaths": unmatched,
        "selectedCheckIds": [check.identifier for check in selected],
        "skippedCheckIds": skipped,
        "checkSelection": selection,
    }
    return ImpactPlan(checks=tuple(selected), evidence=evidence)
