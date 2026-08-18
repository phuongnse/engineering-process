from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import VERSION
from .bundles import load_bundles, select_bundles
from .contracts import ContractError, read_json, validate_project, validate_process_lock
from .distribution import asset_root, distribution_digest, skills_root
from .syncing import skill_target_ownership_issues, sync_skills


AGENTS_START = "<!-- engineering-process:start -->"
AGENTS_END = "<!-- engineering-process:end -->"
PR_DESCRIPTION_START = "<!-- engineering-process:pr-description:start -->"
PR_DESCRIPTION_END = "<!-- engineering-process:pr-description:end -->"
RUNS_IGNORE = "/.process/runs/"


def _serialized(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _install_file(path: Path, content: str, *, replace: bool) -> None:
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContractError(f"{path}: cannot read existing file: {error}") from error
        if current == content:
            return
        if not replace:
            raise ContractError(f"{path}: already exists with different content; use --replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _managed_agents_text(current: str, block: str) -> str:
    start_count = current.count(AGENTS_START)
    end_count = current.count(AGENTS_END)
    if start_count != end_count or start_count > 1:
        raise ContractError("AGENTS.md: invalid engineering-process managed block")
    if start_count == 1:
        start = current.index(AGENTS_START)
        end = current.index(AGENTS_END, start) + len(AGENTS_END)
        prefix = current[:start].rstrip()
        suffix = current[end:].strip()
        parts = [part for part in (prefix, block.strip(), suffix) if part]
        return "\n\n".join(parts) + "\n"
    if not current.strip():
        return block.strip() + "\n"
    return current.rstrip() + "\n\n" + block.strip() + "\n"


def _install_agents(project_root: Path, process_root: Path) -> None:
    template = asset_root(process_root) / "templates" / "AGENTS.process.md"
    if not template.is_file():
        raise ContractError(f"{template}: missing process agent contract template")
    try:
        block = template.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{template}: cannot read template: {error}") from error
    path = project_root / "AGENTS.md"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = _managed_agents_text(current, block)
    if current != updated:
        path.write_text(updated, encoding="utf-8")


def _managed_pull_request_text(current: str, block: str) -> str:
    start_count = current.count(PR_DESCRIPTION_START)
    end_count = current.count(PR_DESCRIPTION_END)
    if start_count != end_count or start_count > 1:
        raise ContractError(
            ".github/PULL_REQUEST_TEMPLATE.md: invalid engineering-process managed block"
        )
    if start_count == 0:
        if current.strip():
            raise ContractError(
                ".github/PULL_REQUEST_TEMPLATE.md: existing unmanaged template must "
                "be migrated around the engineering-process managed block"
            )
        return block.strip() + "\n"
    start = current.index(PR_DESCRIPTION_START)
    end = current.index(PR_DESCRIPTION_END, start) + len(PR_DESCRIPTION_END)
    prefix = current[:start].rstrip()
    suffix = current[end:].strip()
    parts = [part for part in (prefix, block.strip(), suffix) if part]
    return "\n\n".join(parts) + "\n"


def _install_pull_request_template(project_root: Path, process_root: Path) -> None:
    source = asset_root(process_root) / "templates" / "PULL_REQUEST_TEMPLATE.md"
    if not source.is_file():
        raise ContractError(f"{source}: missing pull-request template")
    try:
        block = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{source}: cannot read template: {error}") from error
    path = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = _managed_pull_request_text(current, block)
    if current != updated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")


def _install_ignore(project_root: Path) -> None:
    path = project_root / ".gitignore"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = [RUNS_IGNORE if line == ".process/runs/" else line for line in current.splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line == RUNS_IGNORE and RUNS_IGNORE in normalized:
            continue
        normalized.append(line)
    if RUNS_IGNORE not in normalized:
        normalized.append(RUNS_IGNORE)
    updated = "\n".join(normalized) + "\n"
    if current != updated:
        path.write_text(updated, encoding="utf-8")


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

    _install_file(target_manifest, _serialized(document), replace=replace)
    _install_file(
        project_root / ".process" / "process.lock",
        _serialized(lock_document),
        replace=replace,
    )
    _install_agents(project_root, process_root)
    _install_pull_request_template(project_root, process_root)
    _install_ignore(project_root)
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
