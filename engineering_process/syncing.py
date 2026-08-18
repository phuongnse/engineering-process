from __future__ import annotations

import json
import shutil
import sysconfig
import tempfile
from pathlib import Path

from . import VERSION
from .contracts import ContractError, ProcessLock, read_json, validate_process_lock
from .distribution import distribution_digest, skills_root
from .skills import MARKER_NAME, validate_skills


def default_process_root() -> Path:
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / ".agents" / "skills").is_dir():
        return source_root
    return Path(sysconfig.get_path("data")).resolve()


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


def skill_target_ownership_issues(project_root: Path) -> list[str]:
    target_root = project_root / ".agents" / "skills"
    if not target_root.exists():
        return []
    if not target_root.is_dir():
        return [f"{target_root}: managed skills root must be a directory"]

    issues: list[str] = []
    for target in target_root.iterdir():
        if target.is_file():
            issues.append(
                f"{target}: unmanaged project skill asset; process capabilities must come "
                "from the pinned engineering-process distribution"
            )
            continue
        if not target.is_dir() or not (target / "SKILL.md").is_file():
            continue
        marker = _read_marker(target)
        if not marker or marker.get("distribution") != "engineering-process":
            issues.append(
                f"{target}: unmanaged project skill; process capabilities must come "
                "from the pinned engineering-process distribution"
            )
    return issues


def synchronized_state(
    project_root: Path,
    process_root: Path,
    lock: ProcessLock,
) -> list[str]:
    source_root = process_skills_root(process_root)
    target_root = project_root / ".agents" / "skills"
    issues = validate_skills(source_root, lock.skills)
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
    return issues


def sync_skills(project_root: Path, process_root: Path, *, check: bool) -> list[str]:
    project_root = project_root.resolve()
    process_root = process_root.resolve()
    lock = load_lock(project_root)
    ownership_issues = skill_target_ownership_issues(project_root)
    if ownership_issues and not check:
        raise ContractError("\n".join(ownership_issues))
    issues = synchronized_state(project_root, process_root, lock)
    if check or not issues:
        return issues

    source_root = process_skills_root(process_root)
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
    target_root.mkdir(parents=True, exist_ok=True)
    existing_targets = [target_root / skill for skill in lock.skills]
    stale_targets: list[Path] = []
    for target in target_root.iterdir():
        if not target.is_dir():
            continue
        marker = _read_marker(target)
        if marker and marker.get("distribution") == "engineering-process":
            if target.name not in lock.skills:
                stale_targets.append(target)
    for target in existing_targets:
        if target.exists():
            marker = _read_marker(target)
            if not marker or marker.get("distribution") != "engineering-process":
                raise ContractError(
                    f"{target}: refusing to overwrite an unmanaged skill"
                )

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
