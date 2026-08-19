from __future__ import annotations

import json
import os
import shutil
import sysconfig
import tempfile
from pathlib import Path

from . import VERSION
from .bundles import load_bundles
from .contracts import ContractError, ProcessLock, read_json, validate_process_lock
from .distribution import asset_root, distribution_digest, skills_root
from .git_attributes import managed_attributes_issues, merge_managed_attributes
from .managed import (
    managed_agents_block,
    managed_agents_visibility_issues,
    merge_managed_agents,
)
from .publication import (
    managed_pull_request_block,
    managed_pull_request_visibility_issues,
    merge_managed_pull_request_template,
    validate_project_extensions,
)
from .skills import MARKER_NAME, validate_skills


def default_process_root() -> Path:
    source_root = Path(__file__).resolve().parent.parent
    candidates = (source_root, Path(sysconfig.get_path("data")).resolve())
    for candidate in candidates:
        try:
            asset_root(candidate)
        except ContractError:
            continue
        return candidate
    return candidates[-1]


def process_skills_root(process_root: Path) -> Path:
    return skills_root(process_root)


def load_lock(project_root: Path) -> ProcessLock:
    path = project_root / ".process" / "process.lock"
    return validate_process_lock(read_json(path), str(path))


def _marker(lock: ProcessLock, skill: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "distribution": "engineering-process",
        "version": lock.version,
        "digest": lock.digest,
        "skill": skill,
    }


def _read_marker(path: Path) -> dict[str, object] | None:
    marker_path = path / MARKER_NAME
    if not marker_path.is_file():
        return None
    value = read_json(marker_path)
    return value if isinstance(value, dict) else None


def _files(path: Path, *, ignore_marker: bool) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    if not path.is_dir():
        return result
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        if ignore_marker and item.name == MARKER_NAME:
            continue
        result[item.relative_to(path).as_posix()] = item.read_bytes()
    return result


def managed_parent_issues(project_root: Path) -> list[str]:
    issues: list[str] = []
    for path in (
        project_root / ".process",
        project_root / ".agents",
        project_root / ".agents" / "skills",
        project_root / ".github",
    ):
        if path.is_symlink():
            issues.append(f"{path}: managed parent must not be a symlink")
    return issues


def skill_target_ownership_issues(project_root: Path) -> list[str]:
    target_root = project_root / ".agents" / "skills"
    if target_root.is_symlink():
        return [f"{target_root}: managed skills root must not be a symlink"]
    if not os.path.lexists(target_root):
        return []
    if not target_root.is_dir():
        return [f"{target_root}: managed skills root must be a directory"]

    issues: list[str] = []
    for target in target_root.iterdir():
        if target.is_symlink():
            issues.append(f"{target}: unmanaged symlink in managed skills root")
            continue
        if target.is_file():
            issues.append(
                f"{target}: unmanaged project skill asset; process capabilities must come "
                "from the pinned engineering-process distribution"
            )
            continue
        if not target.is_dir():
            issues.append(f"{target}: unsupported entry in managed skills root")
            continue
        if not (target / "SKILL.md").is_file():
            continue
        marker = _read_marker(target)
        if not marker or marker.get("distribution") != "engineering-process":
            issues.append(
                f"{target}: unmanaged project skill; process capabilities must come "
                "from the pinned engineering-process distribution"
            )
    return issues


def selected_skill_target_issues(
    project_root: Path, skills: tuple[str, ...]
) -> list[str]:
    target_root = project_root / ".agents" / "skills"
    issues: list[str] = []
    for skill in skills:
        target = target_root / skill
        if target.is_symlink():
            issues.append(f"{target}: selected managed skill target must not be a symlink")
            continue
        if not os.path.lexists(target):
            continue
        if not target.is_dir():
            issues.append(f"{target}: selected managed skill target must be a directory")
            continue
        marker = _read_marker(target)
        if not marker or marker.get("distribution") != "engineering-process":
            issues.append(f"{target}: refusing to overwrite an unmanaged skill target")
    return issues


def git_attributes_target_issues(project_root: Path) -> list[str]:
    target = project_root / ".gitattributes"
    if target.is_symlink():
        return [f"{target}: managed Git attributes must not be a symlink"]
    if os.path.lexists(target) and not target.is_file():
        return [f"{target}: managed Git attributes must be a regular file"]
    if target.is_file():
        try:
            merge_managed_attributes(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ContractError) as error:
            return [f"{target}: invalid managed Git attributes: {error}"]
    return []


def _pull_request_template_source(process_root: Path) -> tuple[Path, str]:
    path = asset_root(process_root) / "templates" / "PULL_REQUEST_TEMPLATE.md"
    if not path.is_file():
        raise ContractError(f"{path}: missing pull-request template")
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{path}: cannot read pull-request template: {error}") from error


def _agents_source(process_root: Path) -> tuple[Path, str]:
    path = asset_root(process_root) / "templates" / "AGENTS.process.md"
    if not path.is_file():
        raise ContractError(f"{path}: missing agent contract template")
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{path}: cannot read agent contract template: {error}") from error


def _agents_issues(project_root: Path, process_root: Path) -> list[str]:
    _, source = _agents_source(process_root)
    target = project_root / "AGENTS.md"
    if target.is_symlink():
        return [f"{target}: managed agent contract must not be a symlink"]
    if not target.is_file():
        return [f"{target}: missing managed engineering-process agent contract"]
    try:
        current = target.read_text(encoding="utf-8")
        source_block = managed_agents_block(source).strip()
        target_block = managed_agents_block(current).strip()
    except (OSError, UnicodeError, ContractError) as error:
        return [f"{target}: invalid managed agent contract: {error}"]
    if source_block != target_block:
        issues = [
            f"{target}: managed agent contract differs from the pinned distribution"
        ]
    else:
        issues = []
    issues.extend(
        f"{target}: {issue}" for issue in managed_agents_visibility_issues(current)
    )
    return issues


def _sync_agents(project_root: Path, process_root: Path) -> None:
    _, source = _agents_source(process_root)
    target = project_root / "AGENTS.md"
    if target.is_symlink():
        raise ContractError(f"{target}: managed agent contract must not be a symlink")
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{target}: cannot read agent contract: {error}") from error
    updated = merge_managed_agents(current, source)
    if current != updated:
        target.write_text(updated, encoding="utf-8")


def _pull_request_template_issues(
    project_root: Path, process_root: Path
) -> list[str]:
    _, source = _pull_request_template_source(process_root)
    target = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    if target.is_symlink():
        return [f"{target}: managed pull-request template must not be a symlink"]
    if not target.is_file():
        return [f"{target}: missing managed pull-request template"]
    try:
        current = target.read_text(encoding="utf-8")
        source_block = managed_pull_request_block(source).strip()
        target_block = managed_pull_request_block(current).strip()
    except (OSError, UnicodeError, ContractError) as error:
        return [f"{target}: invalid managed pull-request template: {error}"]
    issues = [
        f"{target}: managed pull-request template differs from the pinned distribution"
    ] if source_block != target_block else []
    issues.extend(
        f"{target}: {issue}"
        for issue in managed_pull_request_visibility_issues(current)
    )
    issues.extend(
        f"{target}: {issue}"
        for issue in validate_project_extensions(current, allow_pending=True)
    )
    return issues


def _sync_pull_request_template(project_root: Path, process_root: Path) -> None:
    _, source = _pull_request_template_source(process_root)
    target = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    if target.is_symlink():
        raise ContractError(f"{target}: managed pull-request template must not be a symlink")
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{target}: cannot read pull-request template: {error}") from error
    updated = merge_managed_pull_request_template(current, source)
    if current != updated:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")


def _git_attributes_issues(project_root: Path) -> list[str]:
    target = project_root / ".gitattributes"
    if target.is_symlink():
        return [f"{target}: managed Git attributes must not be a symlink"]
    if not target.is_file():
        return [f"{target}: missing managed engineering-process Git attributes"]
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"{target}: invalid managed Git attributes: {error}"]
    return [f"{target}: {issue}" for issue in managed_attributes_issues(current)]


def _sync_git_attributes(project_root: Path) -> None:
    target = project_root / ".gitattributes"
    if target.is_symlink():
        raise ContractError(f"{target}: managed Git attributes must not be a symlink")
    if os.path.lexists(target) and not target.is_file():
        raise ContractError(f"{target}: managed Git attributes must be a regular file")
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{target}: cannot read Git attributes: {error}") from error
    updated = merge_managed_attributes(current)
    if current != updated:
        target.write_text(updated, encoding="utf-8")


def synchronized_state(
    project_root: Path,
    process_root: Path,
    lock: ProcessLock,
) -> list[str]:
    source_root = process_skills_root(process_root)
    target_root = project_root / ".agents" / "skills"
    issues = validate_skills(source_root, lock.skills)
    bundles = load_bundles(process_root, source_root)
    missing_core = sorted(set(bundles["core"]) - set(lock.skills))
    if missing_core:
        issues.append(
            "process.lock omits mandatory core skills: " + ", ".join(missing_core)
        )
    if issues:
        return issues
    if lock.version != VERSION:
        issues.append(
            f"process.lock pins {lock.version}, but processctl is {VERSION}"
        )
    actual_digest = distribution_digest(process_root, lock.skills)
    if actual_digest != lock.digest:
        issues.append(
            f"process.lock digest {lock.digest} does not match source {actual_digest}"
        )
    for skill in lock.skills:
        source = source_root / skill
        target = target_root / skill
        if _read_marker(target) != _marker(lock, skill):
            issues.append(f"{target}: missing or stale managed-skill marker")
            continue
        if _files(source, ignore_marker=True) != _files(target, ignore_marker=True):
            issues.append(f"{target}: content differs from the pinned skill")
    issues.extend(skill_target_ownership_issues(project_root))
    issues.extend(managed_parent_issues(project_root))
    if target_root.is_dir():
        for target in target_root.iterdir():
            if not target.is_dir() or not (target / "SKILL.md").is_file():
                continue
            marker = _read_marker(target)
            if (
                marker
                and marker.get("distribution") == "engineering-process"
                and target.name not in lock.skills
            ):
                issues.append(f"{target}: stale managed skill is not present in process.lock")
    issues.extend(_agents_issues(project_root, process_root))
    issues.extend(_pull_request_template_issues(project_root, process_root))
    issues.extend(_git_attributes_issues(project_root))
    return issues


def sync_skills(project_root: Path, process_root: Path, *, check: bool) -> list[str]:
    project_root = project_root.resolve()
    process_root = process_root.resolve()
    lock = load_lock(project_root)
    ownership_issues = [
        *managed_parent_issues(project_root),
        *skill_target_ownership_issues(project_root),
        *selected_skill_target_issues(project_root, lock.skills),
        *git_attributes_target_issues(project_root),
    ]
    if ownership_issues and not check:
        raise ContractError("\n".join(ownership_issues))
    issues = synchronized_state(project_root, process_root, lock)
    if check or not issues:
        return issues

    source_root = process_skills_root(process_root)
    source_issues = validate_skills(source_root, lock.skills)
    bundles = load_bundles(process_root, source_root)
    missing_core = sorted(set(bundles["core"]) - set(lock.skills))
    if source_issues or missing_core:
        details = list(source_issues)
        if missing_core:
            details.append(
                "process.lock omits mandatory core skills: "
                + ", ".join(missing_core)
            )
        raise ContractError("\n".join(details))
    if lock.version != VERSION:
        raise ContractError(
            f"process.lock pins {lock.version}, but processctl is {VERSION}"
        )
    actual_digest = distribution_digest(process_root, lock.skills)
    if actual_digest != lock.digest:
        raise ContractError(
            f"process.lock digest {lock.digest} does not match source {actual_digest}"
        )

    target_root = project_root / ".agents" / "skills"
    existing_targets = [target_root / skill for skill in lock.skills]
    stale_targets: list[Path] = []
    if target_root.is_dir():
        for target in target_root.iterdir():
            if not target.is_dir():
                continue
            marker = _read_marker(target)
            if marker and marker.get("distribution") == "engineering-process":
                if target.name not in lock.skills:
                    stale_targets.append(target)

    _sync_agents(project_root, process_root)
    _sync_pull_request_template(project_root, process_root)
    _sync_git_attributes(project_root)

    target_root.mkdir(parents=True, exist_ok=True)

    stage_parent = target_root.parent
    stage = Path(tempfile.mkdtemp(prefix=".engineering-process-stage-", dir=stage_parent))
    staged_skills = stage / "skills"
    backup = stage / "backup"
    staged_skills.mkdir()
    backup.mkdir()
    try:
        for skill in lock.skills:
            target = staged_skills / skill
            shutil.copytree(source_root / skill, target)
            (target / MARKER_NAME).write_text(
                json.dumps(
                    _marker(lock, skill),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        moved: list[Path] = []
        installed: list[Path] = []
        try:
            for target in [*existing_targets, *stale_targets]:
                if target.exists():
                    target.rename(backup / target.name)
                    moved.append(target)
            for skill in lock.skills:
                target = target_root / skill
                (staged_skills / skill).rename(target)
                installed.append(target)
        except Exception:
            for target in installed:
                if target.exists():
                    shutil.rmtree(target)
            for original in moved:
                saved = backup / original.name
                if saved.exists():
                    saved.rename(original)
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return synchronized_state(project_root, process_root, lock)
