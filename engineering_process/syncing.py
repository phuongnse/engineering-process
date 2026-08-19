from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sysconfig
import tempfile
import time
from pathlib import Path, PurePosixPath

from . import VERSION
from .bundles import load_bundles
from .contracts import ContractError, ProcessLock, read_json, validate_process_lock
from .distribution import asset_root, distribution_digest, skills_root
from .git_attributes import (
    canonical_attributes_block,
    has_managed_attributes_marker,
    managed_attributes_issues,
    read_managed_attributes,
)
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
from .git import portable_git_path


MAX_SKILL_ENTRIES = 500
MAX_SKILL_FILE_BYTES = 1_000_000
MAX_SKILL_TOTAL_BYTES = 8_000_000
SKILL_COMPARISON_TIMEOUT_SECONDS = 10.0
MAX_ADOPTION_RUNNER_BYTES = 128_000
ADOPTION_RUNNER_MARKER = "# Managed by engineering-process; do not edit."


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


def _files(path: Path, *, ignore_marker: bool) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    if not os.path.lexists(path):
        return result
    try:
        root_stat = path.lstat()
    except OSError as error:
        raise ContractError(f"{path}: cannot inspect managed skill: {error}") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ContractError(f"{path}: managed skill comparison root must be a directory")
    deadline = time.monotonic() + SKILL_COMPARISON_TIMEOUT_SECONDS
    entries = 0
    total_bytes = 0
    stack: list[tuple[Path, PurePosixPath]] = [(path, PurePosixPath())]
    while stack:
        if time.monotonic() >= deadline:
            raise ContractError(
                f"{path}: managed skill comparison exceeded "
                f"{SKILL_COMPARISON_TIMEOUT_SECONDS:g} seconds"
            )
        directory, relative_directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = []
                for child in iterator:
                    if time.monotonic() >= deadline:
                        raise ContractError(
                            f"{path}: managed skill comparison exceeded "
                            f"{SKILL_COMPARISON_TIMEOUT_SECONDS:g} seconds"
                        )
                    entries += 1
                    if entries > MAX_SKILL_ENTRIES:
                        raise ContractError(
                            f"{path}: managed skill entry count exceeds "
                            f"{MAX_SKILL_ENTRIES}"
                        )
                    children.append(child)
                children.sort(key=lambda item: item.name)
        except OSError as error:
            raise ContractError(
                f"{directory}: cannot enumerate managed skill: {error}"
            ) from error
        directories: list[tuple[Path, PurePosixPath]] = []
        for child in children:
            relative = relative_directory / child.name
            try:
                encoded = relative.as_posix().encode("utf-8")
            except UnicodeEncodeError as error:
                raise ContractError(
                    f"{path}: managed skill paths must use UTF-8"
                ) from error
            portable = portable_git_path(
                encoded, label=f"{path}: managed skill comparison"
            )
            try:
                before = child.stat(follow_symlinks=False)
            except OSError as error:
                raise ContractError(
                    f"{child.path}: cannot inspect managed skill entry: {error}"
                ) from error
            if stat.S_ISLNK(before.st_mode):
                raise ContractError(
                    f"{child.path}: managed skill comparison rejects symlinks"
                )
            if stat.S_ISDIR(before.st_mode):
                directories.append((Path(child.path), relative))
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ContractError(
                    f"{child.path}: managed skill comparison requires regular files"
                )
            if ignore_marker and child.name == MARKER_NAME:
                continue
            if before.st_size > MAX_SKILL_FILE_BYTES:
                raise ContractError(
                    f"{child.path}: managed skill file exceeds "
                    f"{MAX_SKILL_FILE_BYTES} bytes"
                )
            total_bytes += before.st_size
            if total_bytes > MAX_SKILL_TOTAL_BYTES:
                raise ContractError(
                    f"{path}: managed skill content exceeds "
                    f"{MAX_SKILL_TOTAL_BYTES} bytes"
                )
            digest = hashlib.sha256()
            read_bytes = 0
            try:
                with Path(child.path).open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_dev != before.st_dev
                        or opened.st_ino != before.st_ino
                    ):
                        raise ContractError(
                            f"{child.path}: managed skill file changed while opening"
                        )
                    while chunk := stream.read(64 * 1024):
                        if time.monotonic() >= deadline:
                            raise ContractError(
                                f"{path}: managed skill comparison exceeded "
                                f"{SKILL_COMPARISON_TIMEOUT_SECONDS:g} seconds"
                            )
                        read_bytes += len(chunk)
                        if read_bytes > before.st_size:
                            raise ContractError(
                                f"{child.path}: managed skill file changed while reading"
                            )
                        digest.update(chunk)
                after = Path(child.path).lstat()
            except OSError as error:
                raise ContractError(
                    f"{child.path}: cannot read managed skill file: {error}"
                ) from error
            if (
                read_bytes != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_mode != before.st_mode
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                raise ContractError(
                    f"{child.path}: managed skill file changed while reading"
                )
            result[portable] = (read_bytes, digest.hexdigest())
        stack.extend(reversed(directories))
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
    target = project_root / ".agents" / ".gitattributes"
    try:
        current = read_managed_attributes(target)
    except ContractError as error:
        return [f"{target}: {error}"]
    if (
        current is not None
        and managed_attributes_issues(current)
        and not has_managed_attributes_marker(current)
    ):
        return [f"{target}: refusing to overwrite unmanaged Git attributes"]
    return []


def _pull_request_template_source(process_root: Path) -> tuple[Path, str]:
    path = asset_root(process_root) / "templates" / "PULL_REQUEST_TEMPLATE.md"
    if not path.is_file():
        raise ContractError(f"{path}: missing pull-request template")
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"{path}: cannot read pull-request template: {error}") from error


def _adoption_runner_source(process_root: Path) -> tuple[Path, bytes]:
    path = asset_root(process_root) / "templates" / "adopt-process.py"
    if not path.is_file() or path.is_symlink():
        raise ContractError(f"{path}: missing regular adoption runner template")
    try:
        if path.stat().st_size > MAX_ADOPTION_RUNNER_BYTES:
            raise ContractError(
                f"{path}: adoption runner exceeds {MAX_ADOPTION_RUNNER_BYTES} bytes"
            )
        content = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read adoption runner: {error}") from error
    if len(content) > MAX_ADOPTION_RUNNER_BYTES:
        raise ContractError(
            f"{path}: adoption runner exceeds {MAX_ADOPTION_RUNNER_BYTES} bytes"
        )
    if not content.startswith((ADOPTION_RUNNER_MARKER + "\n").encode("utf-8")):
        raise ContractError(f"{path}: adoption runner is missing its managed marker")
    return path, content


def _read_adoption_runner_target(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ContractError(f"{path}: managed adoption runner must not be a symlink")
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        raise ContractError(f"{path}: managed adoption runner must be a regular file")
    try:
        size = path.stat().st_size
        if size > MAX_ADOPTION_RUNNER_BYTES:
            raise ContractError(
                f"{path}: adoption runner exceeds {MAX_ADOPTION_RUNNER_BYTES} bytes"
            )
        content = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read adoption runner: {error}") from error
    if len(content) > MAX_ADOPTION_RUNNER_BYTES:
        raise ContractError(
            f"{path}: adoption runner exceeds {MAX_ADOPTION_RUNNER_BYTES} bytes"
        )
    return content


def adoption_runner_target_issues(
    project_root: Path, process_root: Path
) -> list[str]:
    target = project_root / ".process" / "adopt-process.py"
    try:
        current = _read_adoption_runner_target(target)
        _, source = _adoption_runner_source(process_root)
    except ContractError as error:
        return [str(error)]
    if (
        current is not None
        and current != source
        and not current.startswith((ADOPTION_RUNNER_MARKER + "\n").encode("utf-8"))
    ):
        return [f"{target}: refusing to overwrite unmanaged adoption runner"]
    return []


def _adoption_runner_issues(project_root: Path, process_root: Path) -> list[str]:
    target = project_root / ".process" / "adopt-process.py"
    try:
        current = _read_adoption_runner_target(target)
        _, source = _adoption_runner_source(process_root)
    except ContractError as error:
        return [str(error)]
    if current is None:
        return [f"{target}: missing managed adoption runner"]
    if current != source:
        return [f"{target}: managed adoption runner differs from the pinned distribution"]
    return []


def _sync_adoption_runner(project_root: Path, process_root: Path) -> None:
    target = project_root / ".process" / "adopt-process.py"
    current = _read_adoption_runner_target(target)
    _, source = _adoption_runner_source(process_root)
    if (
        current is not None
        and current != source
        and not current.startswith((ADOPTION_RUNNER_MARKER + "\n").encode("utf-8"))
    ):
        raise ContractError(f"{target}: refusing to overwrite unmanaged adoption runner")
    if current != source:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)


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
    target = project_root / ".agents" / ".gitattributes"
    try:
        current = read_managed_attributes(target)
    except ContractError as error:
        return [f"{target}: {error}"]
    if current is None:
        return [f"{target}: missing managed engineering-process Git attributes"]
    return [f"{target}: {issue}" for issue in managed_attributes_issues(current)]


def _sync_git_attributes(project_root: Path) -> None:
    target = project_root / ".agents" / ".gitattributes"
    try:
        current = read_managed_attributes(target)
    except ContractError as error:
        raise ContractError(f"{target}: {error}") from error
    if (
        current is not None
        and managed_attributes_issues(current)
        and not has_managed_attributes_marker(current)
    ):
        raise ContractError(f"{target}: refusing to overwrite unmanaged Git attributes")
    if current is None or managed_attributes_issues(current):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical_attributes_block(), encoding="utf-8")


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
    issues.extend(_adoption_runner_issues(project_root, process_root))
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
        *adoption_runner_target_issues(project_root, process_root),
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
    _sync_adoption_runner(project_root, process_root)

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
