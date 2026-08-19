from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import VERSION
from .bundles import load_bundles, select_bundles
from .contracts import ContractError, read_json, validate_project, validate_process_lock
from .distribution import asset_root, distribution_digest, skills_root
from .managed import AGENTS_END, AGENTS_START, merge_managed_agents
from .publication import (
    PR_DESCRIPTION_END,
    PR_DESCRIPTION_START,
    merge_managed_pull_request_template,
)
from .syncing import skill_target_ownership_issues, sync_skills


RUNS_IGNORE = "/.process/runs/"


def _serialized(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    if not path.is_file():
        raise ContractError(f"{path}: expected a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{path}: cannot read existing file: {error}") from error


def _preflight_file(path: Path, content: str, *, replace: bool) -> None:
    if path.exists():
        current = _read_optional_text(path)
        if current == content:
            return
        if not replace:
            raise ContractError(f"{path}: already exists with different content; use --replace")


def _preflight_parents(project_root: Path, *paths: Path) -> None:
    for path in paths:
        parent = path.parent
        while parent != project_root.parent:
            if parent.exists() and not parent.is_dir():
                raise ContractError(
                    f"{parent}: target parent must be a directory before bootstrap"
                )
            if parent == project_root:
                break
            parent = parent.parent


def _write_text(path: Path, content: str) -> None:
    if _read_optional_text(path) == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _agents_update(project_root: Path, process_root: Path) -> tuple[Path, str]:
    template = asset_root(process_root) / "templates" / "AGENTS.process.md"
    if not template.is_file():
        raise ContractError(f"{template}: missing process agent contract template")
    try:
        block = template.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{template}: cannot read template: {error}") from error
    path = project_root / "AGENTS.md"
    current = _read_optional_text(path)
    return path, merge_managed_agents(current, block)


def _pull_request_template_update(
    project_root: Path, process_root: Path
) -> tuple[Path, str]:
    source = asset_root(process_root) / "templates" / "PULL_REQUEST_TEMPLATE.md"
    if not source.is_file():
        raise ContractError(f"{source}: missing pull-request template")
    try:
        block = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{source}: cannot read template: {error}") from error
    path = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    current = _read_optional_text(path)
    return path, merge_managed_pull_request_template(current, block)


def _ignore_update(project_root: Path) -> tuple[Path, str]:
    path = project_root / ".gitignore"
    current = _read_optional_text(path)
    lines = [RUNS_IGNORE if line == ".process/runs/" else line for line in current.splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line == RUNS_IGNORE and RUNS_IGNORE in normalized:
            continue
        normalized.append(line)
    if RUNS_IGNORE not in normalized:
        normalized.append(RUNS_IGNORE)
    updated = "\n".join(normalized) + "\n"
    return path, updated


def initialize_project(
    project_root: Path,
    process_root: Path,
    *,
    manifest_path: Path | None,
    requested_bundles: list[str],
    replace: bool,
) -> dict[str, object]:
    project_root = project_root.resolve()
    process_root = process_root.resolve()
    target_manifest = project_root / ".process" / "project.json"
    source_manifest = target_manifest if manifest_path is None else manifest_path.resolve()
    document = read_json(source_manifest)
    project = validate_project(document, str(source_manifest))

    available = load_bundles(process_root, skills_root(process_root))
    selected_bundles = select_bundles(available, requested_bundles)
    skills = tuple(
        sorted(
            {
                skill
                for bundle in selected_bundles
                for skill in available[bundle]
            }
        )
    )
    lock_document = {
        "schemaVersion": 1,
        "process": {
            "version": VERSION,
            "digest": distribution_digest(process_root, skills),
        },
        "skills": list(skills),
    }
    validate_process_lock(lock_document, str(project_root / ".process" / "process.lock"))
    ownership_issues = skill_target_ownership_issues(project_root)
    if ownership_issues:
        raise ContractError("\n".join(ownership_issues))

    manifest_content = _serialized(document)
    lock_path = project_root / ".process" / "process.lock"
    lock_content = _serialized(lock_document)
    agents_target = project_root / "AGENTS.md"
    pr_target = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    ignore_target = project_root / ".gitignore"
    _preflight_parents(
        project_root,
        target_manifest,
        lock_path,
        agents_target,
        pr_target,
        ignore_target,
        project_root / ".agents" / "skills" / "__process_probe__",
    )
    _preflight_file(target_manifest, manifest_content, replace=replace)
    _preflight_file(lock_path, lock_content, replace=replace)
    agents_path, agents_content = _agents_update(project_root, process_root)
    pr_path, pr_content = _pull_request_template_update(project_root, process_root)
    ignore_path, ignore_content = _ignore_update(project_root)

    _write_text(target_manifest, manifest_content)
    _write_text(lock_path, lock_content)
    _write_text(agents_path, agents_content)
    _write_text(pr_path, pr_content)
    _write_text(ignore_path, ignore_content)
    issues = sync_skills(project_root, process_root, check=False)
    if issues:
        raise ContractError("\n".join(issues))

    return {
        "project": project.identifier,
        "version": VERSION,
        "digest": lock_document["process"]["digest"],
        "bundles": list(selected_bundles),
        "skills": list(skills),
    }
