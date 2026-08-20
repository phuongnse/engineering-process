from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import VERSION
from .bundles import load_bundles
from .contracts import (
    ContractError,
    ProcessLock,
    read_json,
    validate_adoption_migration,
    validate_project,
)
from .distribution import distribution_digest
from .syncing import (
    _files as _managed_files,
    adoption_runner_target_issues,
    default_process_root,
    git_attributes_target_issues,
    load_lock,
    managed_parent_issues,
    process_skills_root,
    selected_skill_target_issues,
    skill_target_ownership_issues,
    sync_skills,
    synchronized_state,
)
from .skills import MARKER_NAME


MAX_REQUIREMENTS_BYTES = 1_000_000
MAX_REQUIREMENTS = 256
MAX_MANAGED_ADOPTION_TARGETS = 256
MAX_MANAGED_ADOPTION_FILE_BYTES = 1_000_000
MAX_MANAGED_ADOPTION_TOTAL_BYTES = 16_000_000
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PACKAGE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
HASH_PATTERN = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
PathIdentity = tuple[int, int, int, int]


@dataclass(frozen=True)
class RequirementPin:
    name: str
    version: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class RequirementsLock:
    path: Path
    digest: str
    pins: tuple[RequirementPin, ...]


@dataclass(frozen=True)
class ProjectMigration:
    path: Path
    content: bytes
    digest: str
    source_digest: str
    target_content: bytes
    target_digest: str
    status: str


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0)
        & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _path_identity(value: os.stat_result) -> PathIdentity:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_mtime_ns,
    )


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


def _path_identity_chain(
    root: Path, path: Path, *, label: str = "requirements path"
) -> tuple[PathIdentity, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ContractError(f"{path}: {label} escaped {root}") from error
    if not relative.parts:
        raise ContractError(f"{path}: {label} must name a file")
    try:
        root_value = root.lstat()
    except OSError as error:
        raise ContractError(
            f"{root}: cannot inspect {label} root: {error}"
        ) from error
    if _is_link_or_reparse(root_value) or not stat.S_ISDIR(root_value.st_mode):
        raise ContractError(
            f"{root}: {label} root must be a regular directory"
        )
    chain: list[PathIdentity] = [_path_identity(root_value)]
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            value = current.lstat()
        except OSError as error:
            raise ContractError(
                f"{current}: cannot inspect {label}: {error}"
            ) from error
        if _is_link_or_reparse(value):
            raise ContractError(
                f"{current}: {label} must not traverse a link or reparse point"
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(value.st_mode):
            raise ContractError(
                f"{current}: {label} ancestor must be a directory"
            )
        chain.append(_path_identity(value))
    return tuple(chain)


def _read_bounded_regular_file(
    path: Path,
    *,
    containment_root: Path | None = None,
    label: str = "requirements lock",
    max_bytes: int = MAX_REQUIREMENTS_BYTES,
) -> bytes:
    before_chain = (
        _path_identity_chain(containment_root, path, label=label)
        if containment_root is not None
        else None
    )
    try:
        before = path.lstat()
    except OSError as error:
        raise ContractError(
            f"{path}: cannot inspect {label}: {error}"
        ) from error
    if not stat.S_ISREG(before.st_mode):
        raise ContractError(f"{path}: {label} must be a regular file")
    if before_chain is not None and _path_identity(before) != before_chain[-1]:
        raise ContractError(
            f"{path}: {label} changed before opening"
        )
    if before.st_size > max_bytes:
        raise ContractError(
            f"{path}: {label} exceeds {max_bytes} bytes"
        )
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not _same_file_identity(opened, before)
                ):
                    raise ContractError(
                        f"{path}: {label} changed while opening"
                    )
                content = stream.read(max_bytes + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        after = path.lstat()
    except OSError as error:
        raise ContractError(
            f"{path}: cannot read {label}: {error}"
        ) from error
    if len(content) > max_bytes:
        raise ContractError(
            f"{path}: {label} exceeds {max_bytes} bytes"
        )
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or not _same_file_identity(after, before)
    ):
        raise ContractError(f"{path}: {label} changed while reading")
    if containment_root is not None:
        after_chain = _path_identity_chain(
            containment_root, path, label=label
        )
        if (
            after_chain != before_chain
            or _path_identity(after) != after_chain[-1]
        ):
            raise ContractError(
                f"{path}: {label} changed while reading"
            )
    return content


def _logical_lines(content: str, path: Path) -> tuple[str, ...]:
    result: list[str] = []
    continuation: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if continuation:
                raise ContractError(f"{path}: interrupted requirement continuation")
            continue
        if line.endswith("\\"):
            continuation.append(line[:-1].rstrip())
            continue
        logical = " ".join([*continuation, line])
        continuation.clear()
        result.append(logical)
    if continuation:
        raise ContractError(f"{path}: unterminated requirement continuation")
    return tuple(result)


def validate_requirements_lock(
    path: Path, *, containment_root: Path | None = None
) -> RequirementsLock:
    resolved = Path(os.path.abspath(os.fspath(path)))
    content = _read_bounded_regular_file(
        resolved, containment_root=containment_root
    )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{resolved}: requirements lock must use UTF-8") from error

    pins: list[RequirementPin] = []
    only_binary = False
    for logical in _logical_lines(text, resolved):
        try:
            tokens = shlex.split(logical, posix=True)
        except ValueError as error:
            raise ContractError(f"{resolved}: invalid requirements syntax: {error}") from error
        if tokens == ["--only-binary", ":all:"]:
            if only_binary:
                raise ContractError(f"{resolved}: duplicate --only-binary directive")
            only_binary = True
            continue
        if not tokens or "==" not in tokens[0]:
            raise ContractError(
                f"{resolved}: every dependency must be an exact name==version pin"
            )
        raw_name, version = tokens[0].split("==", 1)
        if (
            PACKAGE_NAME_PATTERN.fullmatch(raw_name) is None
            or PACKAGE_VERSION_PATTERN.fullmatch(version) is None
        ):
            raise ContractError(f"{resolved}: invalid exact dependency pin {tokens[0]}")
        hashes = tuple(token for token in tokens[1:] if HASH_PATTERN.fullmatch(token))
        if len(hashes) != len(tokens) - 1 or not hashes:
            raise ContractError(
                f"{resolved}: {raw_name} must contain only lowercase sha256 hashes"
            )
        pins.append(
            RequirementPin(
                name=_canonical_package_name(raw_name),
                version=version,
                hashes=tuple(sorted(set(hashes))),
            )
        )
        if len(pins) > MAX_REQUIREMENTS:
            raise ContractError(
                f"{resolved}: dependency count exceeds {MAX_REQUIREMENTS}"
            )

    if not only_binary:
        raise ContractError(f"{resolved}: requires --only-binary :all:")
    names = [pin.name for pin in pins]
    if not pins or names != sorted(names) or len(names) != len(set(names)):
        raise ContractError(
            f"{resolved}: dependency pins must be non-empty, sorted, and unique"
        )
    authority = next(
        (pin for pin in pins if pin.name == "engineering-process"), None
    )
    if authority is None:
        raise ContractError(f"{resolved}: missing engineering-process pin")
    if authority.version != VERSION:
        raise ContractError(
            f"{resolved}: pins engineering-process {authority.version}, "
            f"but processctl is {VERSION}"
        )
    return RequirementsLock(
        path=resolved,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        pins=tuple(pins),
    )


def _require_external_authority(project_root: Path, process_root: Path) -> None:
    installed_root = default_process_root().resolve()
    if process_root != installed_root:
        raise ContractError(
            "adoption authority must be the active installed process root"
        )
    try:
        process_root.relative_to(project_root.resolve(strict=True))
    except ValueError:
        return
    raise ContractError(
        "adoption authority must be installed outside the consumer checkout"
    )


def _checkout_requirements_binding(
    project_root: Path, path: Path
) -> tuple[Path, Path]:
    supplied = path if path.is_absolute() else project_root / path
    candidate = Path(os.path.abspath(os.fspath(supplied)))
    try:
        canonical_root = project_root.resolve(strict=True)
        canonical_candidate = candidate.resolve(strict=True)
        relative = canonical_candidate.relative_to(canonical_root)
    except (OSError, ValueError) as error:
        raise ContractError(
            "requirements source must be a regular file inside the consumer checkout"
        ) from error
    if not relative.parts:
        raise ContractError("requirements source must name a file inside the checkout")
    if len(relative.parts) > len(candidate.parents):
        raise ContractError(
            "requirements source path does not preserve its rooted component depth"
        )
    anchor = candidate.parents[len(relative.parts) - 1]
    try:
        if anchor.resolve(strict=True) != canonical_root:
            raise ContractError(
                "requirements source path does not preserve its rooted component depth"
            )
    except OSError as error:
        raise ContractError(
            "requirements source must be a regular file inside the consumer checkout"
        ) from error
    before_chain = _path_identity_chain(anchor, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(canonical_root)
    except (OSError, ValueError) as error:
        raise ContractError(
            "requirements source must be a regular file inside the consumer checkout"
        ) from error
    if resolved != canonical_candidate:
        raise ContractError(f"{candidate}: requirements path changed while validating")
    if _path_identity_chain(anchor, candidate) != before_chain:
        raise ContractError(
            f"{candidate}: requirements path changed while validating"
        )
    return candidate, anchor


def _checkout_requirements_path(project_root: Path, path: Path) -> Path:
    candidate, _anchor = _checkout_requirements_binding(project_root, path)
    return candidate


def _require_matching_requirements_source(
    source: Path,
    expected_digest: str,
    *,
    containment_root: Path | None = None,
) -> None:
    current = validate_requirements_lock(
        source, containment_root=containment_root
    )
    if current.digest != expected_digest:
        raise ContractError(
            f"{source}: requirements source changed from {expected_digest} "
            f"to {current.digest}"
        )


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_from_bytes(content: bytes, path: Path, *, label: str) -> Any:
    if content.startswith(b"\xef\xbb\xbf"):
        raise ContractError(f"{path}: {label} must not use a UTF-8 BOM")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{path}: {label} must use UTF-8") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(
            f"{path}:{error.lineno}:{error.colno}: invalid {label} JSON: "
            f"{error.msg}"
        ) from error


def _project_document_bytes(document: Any) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _project_migration_path(project_root: Path) -> Path:
    return project_root / ".process" / "adoption-migrations" / f"{VERSION}.json"


def _inspect_project_migration(
    project_root: Path, current_process_version: str
) -> tuple[bytes, ProjectMigration | None]:
    project_path = project_root / ".process" / "project.json"
    project_content = _read_bounded_regular_file(
        project_path,
        containment_root=project_root,
        label="project manifest",
        max_bytes=MAX_MANAGED_ADOPTION_FILE_BYTES,
    )
    project_document = _json_from_bytes(
        project_content, project_path, label="project manifest"
    )
    migration_path = _project_migration_path(project_root)
    if not os.path.lexists(migration_path):
        validate_project(project_document, str(project_path))
        return project_content, None

    migration_content = _read_bounded_regular_file(
        migration_path,
        containment_root=project_root,
        label="project adoption migration",
        max_bytes=MAX_MANAGED_ADOPTION_FILE_BYTES,
    )
    migration_document = _json_from_bytes(
        migration_content,
        migration_path,
        label="project adoption migration",
    )
    validate_adoption_migration(migration_document, str(migration_path))
    if migration_document["toProcessVersion"] != VERSION:
        raise ContractError(
            f"{migration_path}: targets process "
            f"{migration_document['toProcessVersion']}, not {VERSION}"
        )
    if current_process_version not in {
        migration_document["fromProcessVersion"],
        migration_document["toProcessVersion"],
    }:
        raise ContractError(
            f"{migration_path}: cannot migrate process {current_process_version} "
            f"from {migration_document['fromProcessVersion']} to "
            f"{migration_document['toProcessVersion']}"
        )

    target_project = validate_project(
        migration_document["project"], f"{migration_path}.project"
    )
    if (
        not isinstance(project_document, dict)
        or project_document.get("project") != target_project.identifier
    ):
        raise ContractError(
            f"{migration_path}: project identity does not match the active manifest"
        )
    target_content = _project_document_bytes(migration_document["project"])
    target_digest = _sha256(target_content)
    if target_digest != migration_document["targetProjectDigest"]:
        raise ContractError(
            f"{migration_path}: targetProjectDigest does not match project content"
        )
    project_digest = _sha256(project_content)
    source_digest = migration_document["sourceProjectDigest"]
    if project_digest == source_digest:
        status = "pending"
    elif project_digest == target_digest:
        status = "applied"
    else:
        raise ContractError(
            f"{migration_path}: active project digest {project_digest} matches "
            "neither the migration source nor target"
        )
    return project_content, ProjectMigration(
        path=migration_path,
        content=migration_content,
        digest=_sha256(migration_content),
        source_digest=source_digest,
        target_content=target_content,
        target_digest=target_digest,
        status=status,
    )


def _require_unchanged_file(
    path: Path,
    expected: bytes,
    *,
    containment_root: Path,
    label: str,
) -> None:
    current = _read_bounded_regular_file(
        path,
        containment_root=containment_root,
        label=label,
        max_bytes=MAX_MANAGED_ADOPTION_FILE_BYTES,
    )
    if current != expected:
        raise ContractError(
            f"{path}: {label} changed from {_sha256(expected)} to {_sha256(current)}"
        )


def _project_migration_result(
    project_root: Path,
    migration: ProjectMigration | None,
    *,
    applied: bool = False,
) -> dict[str, object] | None:
    if migration is None:
        return None
    return {
        "path": migration.path.relative_to(project_root).as_posix(),
        "digest": migration.digest,
        "sourceProjectDigest": migration.source_digest,
        "targetProjectDigest": migration.target_digest,
        "status": "applied" if applied else migration.status,
    }


def _lock_document(process_root: Path, previous: ProcessLock) -> dict[str, object]:
    bundles = load_bundles(process_root, process_skills_root(process_root))
    skills = tuple(sorted(set(previous.skills) | set(bundles["core"])))
    return {
        "schemaVersion": 1,
        "process": {
            "version": VERSION,
            "digest": distribution_digest(process_root, skills),
        },
        "skills": list(skills),
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _managed_skill_targets(project_root: Path, selected: tuple[str, ...]) -> tuple[Path, ...]:
    root = project_root / ".agents" / "skills"
    targets = {root / skill for skill in selected}
    if not root.is_dir():
        return tuple(sorted(targets))
    entries = 0
    for candidate in root.iterdir():
        entries += 1
        if entries > MAX_MANAGED_ADOPTION_TARGETS:
            raise ContractError(
                f"{root}: managed skill target count exceeds "
                f"{MAX_MANAGED_ADOPTION_TARGETS}"
            )
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        marker = candidate / MARKER_NAME
        if not marker.is_file() or marker.is_symlink():
            continue
        value = read_json(marker)
        if isinstance(value, dict) and value.get("distribution") == "engineering-process":
            targets.add(candidate)
    return tuple(sorted(targets))


def _snapshot_targets(
    project_root: Path,
    selected: tuple[str, ...],
    backup_root: Path,
    *,
    extra_targets: tuple[Path, ...] = (),
) -> list[tuple[Path, Path, bool]]:
    targets = [
        project_root / ".process" / "process.lock",
        project_root / ".process" / "adopt-process.py",
        project_root / ".process" / "adopt-process-windows-job.py",
        project_root / "AGENTS.md",
        project_root / ".github" / "PULL_REQUEST_TEMPLATE.md",
        project_root / ".agents" / ".gitattributes",
        *_managed_skill_targets(project_root, selected),
        *extra_targets,
    ]
    if len(targets) > MAX_MANAGED_ADOPTION_TARGETS:
        raise ContractError(
            "managed adoption target count exceeds "
            f"{MAX_MANAGED_ADOPTION_TARGETS}"
        )
    if len(set(targets)) != len(targets):
        raise ContractError("managed adoption targets must be unique")
    snapshots: list[tuple[Path, Path, bool]] = []
    total_bytes = 0
    for index, target in enumerate(targets):
        backup = backup_root / str(index)
        exists = os.path.lexists(target)
        if exists:
            if target.is_symlink():
                raise ContractError(f"{target}: managed adoption target must not be a symlink")
            if target.is_dir():
                before = _managed_files(target, ignore_marker=False)
                total_bytes += sum(size for size, _ in before.values())
                if total_bytes > MAX_MANAGED_ADOPTION_TOTAL_BYTES:
                    raise ContractError(
                        "managed adoption snapshot exceeds "
                        f"{MAX_MANAGED_ADOPTION_TOTAL_BYTES} bytes"
                    )
                shutil.copytree(target, backup)
                if (
                    _managed_files(target, ignore_marker=False) != before
                    or _managed_files(backup, ignore_marker=False) != before
                ):
                    raise ContractError(
                        f"{target}: managed adoption target changed while snapshotting"
                    )
            elif target.is_file():
                before = target.stat()
                if before.st_size > MAX_MANAGED_ADOPTION_FILE_BYTES:
                    raise ContractError(
                        f"{target}: managed adoption file exceeds "
                        f"{MAX_MANAGED_ADOPTION_FILE_BYTES} bytes"
                    )
                total_bytes += before.st_size
                if total_bytes > MAX_MANAGED_ADOPTION_TOTAL_BYTES:
                    raise ContractError(
                        "managed adoption snapshot exceeds "
                        f"{MAX_MANAGED_ADOPTION_TOTAL_BYTES} bytes"
                    )
                backup.parent.mkdir(parents=True, exist_ok=True)
                with target.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (
                        not _same_file_identity(opened, before)
                        or not stat.S_ISREG(opened.st_mode)
                    ):
                        raise ContractError(
                            f"{target}: managed adoption target changed while opening"
                        )
                    content = stream.read(MAX_MANAGED_ADOPTION_FILE_BYTES + 1)
                if len(content) > MAX_MANAGED_ADOPTION_FILE_BYTES:
                    raise ContractError(
                        f"{target}: managed adoption file exceeds "
                        f"{MAX_MANAGED_ADOPTION_FILE_BYTES} bytes"
                    )
                after = target.stat()
                if (
                    len(content) != before.st_size
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                    or not _same_file_identity(after, before)
                ):
                    raise ContractError(
                        f"{target}: managed adoption target changed while snapshotting"
                    )
                backup.write_bytes(content)
                os.chmod(backup, stat.S_IMODE(before.st_mode))
            else:
                raise ContractError(
                    f"{target}: managed adoption target must be a regular file or directory"
                )
        snapshots.append((target, backup, exists))
    return snapshots


def _restore_targets(snapshots: list[tuple[Path, Path, bool]]) -> list[str]:
    issues: list[str] = []
    for target, backup, existed in reversed(snapshots):
        try:
            if os.path.lexists(target):
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                else:
                    raise OSError("unsupported managed target type")
            if existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, target)
                else:
                    shutil.copy2(backup, target)
        except OSError as error:
            issues.append(f"{target}: adoption rollback failed: {error}")
    return issues


def check_adoption(
    project_root: Path,
    process_root: Path,
    requirements_lock: Path,
) -> dict[str, object]:
    project_root = Path(os.path.abspath(os.fspath(project_root)))
    process_root = process_root.resolve()
    _require_external_authority(project_root, process_root)
    requirement_path, requirement_root = _checkout_requirements_binding(
        project_root, requirements_lock
    )
    requirement = validate_requirements_lock(
        requirement_path, containment_root=requirement_root
    )
    lock = load_lock(project_root)
    _, migration = _inspect_project_migration(project_root, lock.version)
    issues = synchronized_state(project_root, process_root, lock)
    if migration is not None and migration.status == "pending":
        issues.append(
            f"{migration.path}: project adoption migration is pending"
        )
    return {
        "version": VERSION,
        "digest": lock.digest,
        "requirementsDigest": requirement.digest,
        "skills": list(lock.skills),
        "projectMigration": _project_migration_result(
            project_root, migration
        ),
        "issues": issues,
    }


def apply_adoption(
    project_root: Path,
    process_root: Path,
    requirements_lock: Path,
    *,
    requirements_source: Path | None = None,
    expected_requirements_digest: str | None = None,
) -> dict[str, object]:
    project_root = Path(os.path.abspath(os.fspath(project_root)))
    process_root = process_root.resolve()
    _require_external_authority(project_root, process_root)
    if requirements_source is None:
        requirement_path, requirement_root = _checkout_requirements_binding(
            project_root, requirements_lock
        )
        source_path = requirement_path
        source_root = requirement_root
    else:
        source_path, source_root = _checkout_requirements_binding(
            project_root, requirements_source
        )
        requirement_path = Path(
            os.path.abspath(os.fspath(requirements_lock))
        )
        if requirement_path.is_symlink():
            raise ContractError(
                f"{requirement_path}: requirements snapshot must not be a symlink"
            )
        try:
            resolved_snapshot = requirement_path.resolve(strict=True)
        except OSError as error:
            raise ContractError(
                f"{requirement_path}: cannot inspect requirements snapshot: {error}"
            ) from error
        try:
            resolved_snapshot.relative_to(project_root.resolve(strict=True))
        except ValueError:
            pass
        else:
            raise ContractError(
                "requirements snapshot must be outside the consumer checkout"
            )
        requirement_root = None
    requirement = validate_requirements_lock(
        requirement_path,
        containment_root=requirement_root,
    )
    if (
        expected_requirements_digest is not None
        and requirement.digest != expected_requirements_digest
    ):
        raise ContractError(
            "requirements snapshot digest does not match the runner expectation"
        )
    _require_matching_requirements_source(
        source_path,
        requirement.digest,
        containment_root=source_root,
    )

    lock_path = project_root / ".process" / "process.lock"
    if lock_path.is_symlink():
        raise ContractError(f"{lock_path}: process lock must not be a symlink")
    previous = load_lock(project_root)
    project_content, migration = _inspect_project_migration(
        project_root, previous.version
    )
    document = _lock_document(process_root, previous)
    selected = tuple(document["skills"])
    ownership_issues = [
        *managed_parent_issues(project_root),
        *skill_target_ownership_issues(project_root),
        *selected_skill_target_issues(project_root, selected),
        *git_attributes_target_issues(project_root),
        *adoption_runner_target_issues(project_root, process_root),
    ]
    if ownership_issues:
        raise ContractError("\n".join(ownership_issues))
    updated = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )

    with tempfile.TemporaryDirectory(prefix="engineering-process-adoption-backup-") as directory:
        backup_root = Path(directory).resolve()
        try:
            backup_root.relative_to(project_root.resolve(strict=True))
        except ValueError:
            pass
        else:
            raise ContractError(
                "temporary adoption backup must be outside the consumer checkout"
            )
        snapshots = _snapshot_targets(
            project_root,
            selected,
            backup_root,
            extra_targets=(
                (project_root / ".process" / "project.json",)
                if migration is not None and migration.status == "pending"
                else ()
            ),
        )
        try:
            if migration is not None:
                _require_unchanged_file(
                    migration.path,
                    migration.content,
                    containment_root=project_root,
                    label="project adoption migration",
                )
            _require_unchanged_file(
                project_root / ".process" / "project.json",
                project_content,
                containment_root=project_root,
                label="project manifest",
            )
            _atomic_write(lock_path, updated)
            if migration is not None and migration.status == "pending":
                _atomic_write(
                    project_root / ".process" / "project.json",
                    migration.target_content,
                )
            issues = sync_skills(project_root, process_root, check=False)
            if issues:
                raise ContractError("\n".join(issues))
            _require_matching_requirements_source(
                requirement_path, requirement.digest
            )
            _require_matching_requirements_source(
                source_path,
                requirement.digest,
                containment_root=source_root,
            )
            if migration is not None:
                _require_unchanged_file(
                    migration.path,
                    migration.content,
                    containment_root=project_root,
                    label="project adoption migration",
                )
                _require_unchanged_file(
                    project_root / ".process" / "project.json",
                    migration.target_content,
                    containment_root=project_root,
                    label="project manifest",
                )
            else:
                _require_unchanged_file(
                    project_root / ".process" / "project.json",
                    project_content,
                    containment_root=project_root,
                    label="project manifest",
                )
        except BaseException as error:
            rollback_issues = _restore_targets(snapshots)
            if rollback_issues:
                raise ContractError("\n".join(rollback_issues)) from error
            raise

    return {
        "previousVersion": previous.version,
        "version": VERSION,
        "digest": document["process"]["digest"],
        "requirementsDigest": requirement.digest,
        "skills": document["skills"],
        "projectMigration": _project_migration_result(
            project_root,
            migration,
            applied=migration is not None and migration.status == "pending",
        ),
    }
