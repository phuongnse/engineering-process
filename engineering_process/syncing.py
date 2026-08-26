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
MAX_SYNC_SKILL_ENTRIES = 4_096
MAX_SYNC_SKILL_BYTES = 32_000_000
SYNC_SKILL_TIMEOUT_SECONDS = 20.0


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare stable ids when Windows path stat omits the volume serial."""
    if os.name == "nt":
        return (
            left.st_ino != 0
            and left.st_ino == right.st_ino
            and (
                left.st_dev == 0
                or right.st_dev == 0
                or left.st_dev == right.st_dev
            )
        )
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


MAX_ADOPTION_RUNNER_BYTES = 128_000
ADOPTION_RUNNER_MARKER = "# Managed by engineering-process; do not edit."
ADOPTION_SCRIPT_NAMES = (
    "adopt-process.py",
    "adopt-process-windows-job.py",
)


def _write_utf8_lf(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def _has_adoption_runner_marker(content: bytes) -> bool:
    marker = ADOPTION_RUNNER_MARKER.encode("utf-8")
    return content.startswith(marker + b"\n") or content.startswith(
        marker + b"\r\n"
    )


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


def _read_marker(
    path: Path,
    *,
    shared_budget: dict[str, float | int] | None = None,
) -> dict[str, object] | None:
    marker_path = path / MARKER_NAME
    if not os.path.lexists(marker_path):
        return None
    if shared_budget is None:
        value = read_json(marker_path)
        return value if isinstance(value, dict) else None
    if time.monotonic() >= shared_budget["deadline"]:
        raise ContractError(
            "managed synchronization exceeded "
            f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
        )
    try:
        before = marker_path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(
                f"{marker_path}: managed marker must be a regular file"
            )
        if before.st_size > MAX_SKILL_FILE_BYTES:
            raise ContractError(
                f"{marker_path}: managed marker exceeds {MAX_SKILL_FILE_BYTES} bytes"
            )
        shared_budget["entries"] += 1
        shared_budget["bytes"] += before.st_size
        if shared_budget["entries"] > MAX_SYNC_SKILL_ENTRIES:
            raise ContractError(
                "managed synchronization entry count exceeds "
                f"{MAX_SYNC_SKILL_ENTRIES}"
            )
        if shared_budget["bytes"] > MAX_SYNC_SKILL_BYTES:
            raise ContractError(
                "managed synchronization bytes exceed "
                f"{MAX_SYNC_SKILL_BYTES}"
            )
        with marker_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_file_identity(opened, before)
            ):
                raise ContractError(f"{marker_path}: changed while opening")
            content = stream.read(MAX_SKILL_FILE_BYTES + 1)
        after = marker_path.lstat()
    except OSError as error:
        raise ContractError(f"{marker_path}: cannot read marker: {error}") from error
    if (
        len(content) != before.st_size
        or len(content) > MAX_SKILL_FILE_BYTES
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or not _same_file_identity(after, before)
    ):
        raise ContractError(f"{marker_path}: changed while reading")
    if time.monotonic() >= shared_budget["deadline"]:
        raise ContractError(
            "managed synchronization exceeded "
            f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
        )
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{marker_path}: invalid marker JSON: {error}") from error
    return value if isinstance(value, dict) else None


def _files(
    path: Path,
    *,
    ignore_marker: bool,
    shared_budget: dict[str, float | int] | None = None,
) -> dict[str, tuple[int, str]]:
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
        if (
            shared_budget is not None
            and time.monotonic() >= shared_budget["deadline"]
        ):
            raise ContractError(
                "managed synchronization exceeded "
                f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
            )
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
                    if shared_budget is not None:
                        shared_budget["entries"] += 1
                        if shared_budget["entries"] > MAX_SYNC_SKILL_ENTRIES:
                            raise ContractError(
                                "managed synchronization entry count exceeds "
                                f"{MAX_SYNC_SKILL_ENTRIES}"
                            )
                        if time.monotonic() >= shared_budget["deadline"]:
                            raise ContractError(
                                "managed synchronization exceeded "
                                f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
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
                # DirEntry.stat() may reuse incomplete directory-enumeration
                # metadata on Windows.  A path lstat obtains the same stable
                # file identity surface used by the opened handle below.
                before = Path(child.path).lstat()
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
            if shared_budget is not None:
                shared_budget["bytes"] += before.st_size
                if shared_budget["bytes"] > MAX_SYNC_SKILL_BYTES:
                    raise ContractError(
                        "managed synchronization bytes exceed "
                        f"{MAX_SYNC_SKILL_BYTES}"
                    )
            digest = hashlib.sha256()
            read_bytes = 0
            try:
                with Path(child.path).open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or not _same_file_identity(opened, before)
                    ):
                        raise ContractError(
                            f"{child.path}: managed skill file changed while opening"
                        )
                    while chunk := stream.read(64 * 1024):
                        if (
                            shared_budget is not None
                            and time.monotonic() >= shared_budget["deadline"]
                        ):
                            raise ContractError(
                                "managed synchronization exceeded "
                                f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
                            )
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
                or not _same_file_identity(after, before)
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


def skill_target_ownership_issues(
    project_root: Path,
    *,
    targets: list[Path] | None = None,
    shared_budget: dict[str, float | int] | None = None,
) -> list[str]:
    target_root = project_root / ".agents" / "skills"
    if target_root.is_symlink():
        return [f"{target_root}: managed skills root must not be a symlink"]
    if not os.path.lexists(target_root):
        return []
    if not target_root.is_dir():
        return [f"{target_root}: managed skills root must be a directory"]

    issues: list[str] = []
    for target in target_root.iterdir() if targets is None else targets:
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
        marker = _read_marker(target, shared_budget=shared_budget)
        if not marker or marker.get("distribution") != "engineering-process":
            issues.append(
                f"{target}: unmanaged project skill; process capabilities must come "
                "from the pinned engineering-process distribution"
            )
    return issues


def _bounded_skill_targets(
    project_root: Path, shared_budget: dict[str, float | int]
) -> list[Path]:
    target_root = project_root / ".agents" / "skills"
    if not target_root.is_dir() or target_root.is_symlink():
        return []
    targets: list[Path] = []
    try:
        with os.scandir(target_root) as iterator:
            for item in iterator:
                shared_budget["entries"] += 1
                if shared_budget["entries"] > MAX_SYNC_SKILL_ENTRIES:
                    raise ContractError(
                        "managed synchronization entry count exceeds "
                        f"{MAX_SYNC_SKILL_ENTRIES}"
                    )
                if time.monotonic() >= shared_budget["deadline"]:
                    raise ContractError(
                        "managed synchronization exceeded "
                        f"{SYNC_SKILL_TIMEOUT_SECONDS:g} seconds"
                    )
                targets.append(Path(item.path))
    except OSError as error:
        raise ContractError(
            f"cannot enumerate managed skill targets {target_root}: {error}"
        ) from error
    return sorted(targets)


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


def _adoption_runner_source(
    process_root: Path, name: str = "adopt-process.py"
) -> tuple[Path, bytes]:
    if name not in ADOPTION_SCRIPT_NAMES:
        raise ContractError(f"unsupported adoption script: {name}")
    path = asset_root(process_root) / "templates" / name
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
    issues: list[str] = []
    for name in ADOPTION_SCRIPT_NAMES:
        target = project_root / ".process" / name
        try:
            current = _read_adoption_runner_target(target)
            _, source = _adoption_runner_source(process_root, name)
        except ContractError as error:
            issues.append(str(error))
            continue
        if (
            current is not None
            and current != source
            and not _has_adoption_runner_marker(current)
        ):
            issues.append(
                f"{target}: refusing to overwrite unmanaged adoption runner"
            )
    return issues


def _adoption_runner_issues(project_root: Path, process_root: Path) -> list[str]:
    issues: list[str] = []
    for name in ADOPTION_SCRIPT_NAMES:
        target = project_root / ".process" / name
        try:
            current = _read_adoption_runner_target(target)
            _, source = _adoption_runner_source(process_root, name)
        except ContractError as error:
            issues.append(str(error))
            continue
        if current is None:
            issues.append(f"{target}: missing managed adoption runner")
        elif current != source:
            issues.append(
                f"{target}: managed adoption runner differs from the pinned distribution"
            )
    return issues


def _sync_adoption_runner(project_root: Path, process_root: Path) -> None:
    for name in ADOPTION_SCRIPT_NAMES:
        target = project_root / ".process" / name
        current = _read_adoption_runner_target(target)
        _, source = _adoption_runner_source(process_root, name)
        if (
            current is not None
            and current != source
            and not _has_adoption_runner_marker(current)
        ):
            raise ContractError(
                f"{target}: refusing to overwrite unmanaged adoption runner"
            )
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
        _write_utf8_lf(target, updated)


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
        _write_utf8_lf(target, updated)


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
        _write_utf8_lf(target, canonical_attributes_block())


def synchronized_state(
    project_root: Path,
    process_root: Path,
    lock: ProcessLock,
    *,
    authority_version: str | None = None,
    package_root: Path | None = None,
) -> list[str]:
    source_root = process_skills_root(process_root)
    target_root = project_root / ".agents" / "skills"
    shared_budget: dict[str, float | int] = {
        "deadline": time.monotonic() + SYNC_SKILL_TIMEOUT_SECONDS,
        "entries": 0,
        "bytes": 0,
    }
    try:
        target_entries = _bounded_skill_targets(project_root, shared_budget)
    except ContractError as error:
        return [str(error)]
    issues = validate_skills(source_root, lock.skills)
    bundles = load_bundles(
        process_root,
        source_root,
        selected_skills=lock.skills,
    )
    missing_core = sorted(set(bundles["core"]) - set(lock.skills))
    if missing_core:
        issues.append(
            "process.lock omits mandatory core skills: " + ", ".join(missing_core)
        )
    if issues:
        return issues
    expected_authority_version = authority_version or VERSION
    if lock.version != expected_authority_version:
        issues.append(
            f"process.lock pins {lock.version}, but processctl is "
            f"{expected_authority_version}"
        )
    actual_digest = distribution_digest(
        process_root,
        lock.skills,
        package_root=package_root,
    )
    if actual_digest != lock.digest:
        issues.append(
            f"process.lock digest {lock.digest} does not match source {actual_digest}"
        )
    for skill in lock.skills:
        source = source_root / skill
        target = target_root / skill
        if _read_marker(target, shared_budget=shared_budget) != _marker(lock, skill):
            issues.append(f"{target}: missing or stale managed-skill marker")
            continue
        if _files(
            source,
            ignore_marker=True,
            shared_budget=shared_budget,
        ) != _files(
            target,
            ignore_marker=True,
            shared_budget=shared_budget,
        ):
            issues.append(f"{target}: content differs from the pinned skill")
    issues.extend(
        skill_target_ownership_issues(
            project_root,
            targets=target_entries,
            shared_budget=shared_budget,
        )
    )
    issues.extend(managed_parent_issues(project_root))
    if target_root.is_dir():
        for target in target_entries:
            if not target.is_dir() or not (target / "SKILL.md").is_file():
                continue
            marker = _read_marker(target, shared_budget=shared_budget)
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
            _write_utf8_lf(
                target / MARKER_NAME,
                json.dumps(
                    _marker(lock, skill),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
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
