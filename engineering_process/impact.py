from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import Check, ContractError, Project, ProjectImpact
from .git import remaining_seconds, run_git


MAX_CHANGED_PATHS = 5_000
MAX_CHANGED_PATH_BYTES = 100_000
MAX_CHANGED_PATH_LENGTH = 1_024
MAX_IMPACT_EVIDENCE_BYTES = 350_000
IMPACT_PLANNING_TIMEOUT_SECONDS = 15.0
IMPACT_FILE_ENV = "ENGINEERING_PROCESS_IMPACT_FILE"


@dataclass(frozen=True)
class ImpactPlan:
    checks: tuple[Check, ...]
    evidence: dict[str, Any]


def _git(
    root: Path,
    arguments: list[str],
    *,
    label: str,
    deadline: float,
    max_stdout_bytes: int,
) -> bytes:
    result = run_git(
        root,
        arguments,
        label=f"impact scope {label}",
        timeout_seconds=remaining_seconds(
            deadline, label=f"impact scope {label}"
        ),
        max_stdout_bytes=max_stdout_bytes,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"impact scope {label}: git exited {result.returncode}: "
            f"{detail or 'no diagnostic'}"
        )
    return result.stdout


def _commit(root: Path, ref: str, *, deadline: float) -> str | None:
    result = run_git(
        root,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        label=f"impact scope resolve {ref}",
        timeout_seconds=remaining_seconds(
            deadline, label=f"impact scope resolve {ref}"
        ),
        max_stdout_bytes=128,
    )
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
    segments = path.split("/")
    windows_reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if (
        not path
        or len(path) > MAX_CHANGED_PATH_LENGTH
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or any(character in '<>:"|?*' for character in path)
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != path
        or any(not segment or segment.endswith((" ", ".")) for segment in segments)
        or any(
            segment.split(".", 1)[0].upper() in windows_reserved
            for segment in segments
        )
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


def _changed_paths(
    root: Path, merge_base: str, head: str, *, deadline: float
) -> list[str]:
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
        output = _git(
            root,
            arguments,
            label=label,
            deadline=deadline,
            max_stdout_bytes=MAX_CHANGED_PATH_BYTES,
        )
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
    deadline: float | None = None,
) -> dict[str, Any]:
    deadline = deadline or (time.monotonic() + IMPACT_PLANNING_TIMEOUT_SECONDS)
    head = _commit(root, "HEAD", deadline=deadline)
    if head is None:
        raise ContractError("impact scope: project root has no Git HEAD commit")

    candidates = (base_ref,) if base_ref is not None else impact.base_refs
    selected_ref: str | None = None
    base_commit: str | None = None
    for candidate in candidates:
        resolved = _commit(root, candidate, deadline=deadline)
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
        deadline=deadline,
        max_stdout_bytes=128,
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
        "changedPaths": _changed_paths(
            root, merge_base, head, deadline=deadline
        ),
    }


def _segment_matches(pattern: str, value: str) -> bool:
    pattern_index = 0
    value_index = 0
    star_index = -1
    star_value_index = -1
    while value_index < len(value):
        if (
            pattern_index < len(pattern)
            and pattern[pattern_index] in {"?", value[value_index]}
        ):
            pattern_index += 1
            value_index += 1
        elif pattern_index < len(pattern) and pattern[pattern_index] == "*":
            star_index = pattern_index
            pattern_index += 1
            star_value_index = value_index
        elif star_index >= 0:
            pattern_index = star_index + 1
            star_value_index += 1
            value_index = star_value_index
        else:
            return False
    return all(character == "*" for character in pattern[pattern_index:])


def _glob_matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    previous = [False] * (len(path) + 1)
    previous[0] = True
    for segment in pattern:
        current = [False] * (len(path) + 1)
        if segment == "**":
            current[0] = previous[0]
            for index in range(1, len(path) + 1):
                current[index] = previous[index] or current[index - 1]
        else:
            for index in range(1, len(path) + 1):
                current[index] = previous[index - 1] and _segment_matches(
                    segment, path[index - 1]
                )
        previous = current
    return previous[-1]


def _affected_components(
    impact: ProjectImpact, direct: set[str]
) -> set[str]:
    affected = set(direct)
    pending = deque(sorted(direct))
    while pending:
        identifier = pending.popleft()
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
    deadline = time.monotonic() + IMPACT_PLANNING_TIMEOUT_SECONDS
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

    scope = discover_scope(
        root, project.impact, base_ref=base_ref, deadline=deadline
    )
    compiled_patterns = {
        component.identifier: tuple(
            tuple(pattern.split("/")) for pattern in component.paths
        )
        for component in project.impact.components.values()
    }
    direct: set[str] = set()
    matched_paths: set[str] = set()
    for path in scope["changedPaths"]:
        remaining_seconds(deadline, label="impact component matching")
        path_segments = tuple(path.split("/"))
        attempts = 0
        for component in project.impact.components.values():
            component_matched = False
            for pattern in compiled_patterns[component.identifier]:
                attempts += 1
                if attempts % 64 == 0:
                    remaining_seconds(deadline, label="impact component matching")
                if _glob_matches(pattern, path_segments):
                    component_matched = True
                    break
            if component_matched:
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
    evidence_size = len(
        json.dumps(
            evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if evidence_size > MAX_IMPACT_EVIDENCE_BYTES:
        raise ContractError(
            "impact evidence exceeds the bounded serialized limit: "
            f"{evidence_size} > {MAX_IMPACT_EVIDENCE_BYTES} bytes"
        )
    return ImpactPlan(checks=tuple(selected), evidence=evidence)
