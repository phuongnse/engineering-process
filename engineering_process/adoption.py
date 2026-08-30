"""Idempotent materialization of one published process into a consumer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Callable

from . import VERSION
from .contracts import ProcessError, read_json, validate_document
from .distribution import (
    distribution_digest,
    schemas_root,
    skill_digest,
    skill_names,
    skills_root,
)
from .project import normalize_project, project_path


MAX_REQUIREMENTS_BYTES = 2_000_000
MAX_MANAGED_BYTES = 10_000_000
PIN = re.compile(r"^engineering-process==([^\s\\]+)", re.MULTILINE)
START_MARKER = "<!-- engineering-process:start -->"
END_MARKER = "<!-- engineering-process:end -->"
PR_START_MARKER = "<!-- engineering-process:pr-description:start -->"
PR_END_MARKER = "<!-- engineering-process:pr-description:end -->"
LEGACY_SKILL_FILES = (
    Path("SKILL.md"),
    Path(".engineering-process.json"),
    Path("references/execution.md"),
)


def _managed_inventory_path(raw: str, skills: set[str]) -> Path:
    relative = PurePosixPath(raw)
    if relative.as_posix() != raw:
        raise ProcessError(f"process lock contains a non-canonical managed path: {raw}")
    if raw in {
        ".process/adopt-process.py",
        ".process/adopt-process-windows-job.py",
    }:
        return Path(*relative.parts)
    parts = relative.parts
    if (
        len(parts) < 4
        or parts[:2] != (".agents", "skills")
        or parts[2] not in skills
    ):
        raise ProcessError(
            f"process lock managed path is outside its owned namespaces: {raw}"
        )
    return Path(*parts)


def _requirements(
    requirements_lock: Path,
    *,
    requirements_source: Path | None = None,
    expected_digest: str | None = None,
) -> tuple[bytes, str]:
    try:
        content = requirements_lock.read_bytes()
    except OSError as error:
        raise ProcessError(f"cannot read requirements lock: {error}") from error
    if len(content) > MAX_REQUIREMENTS_BYTES:
        raise ProcessError("requirements lock exceeds the adoption input limit")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise ProcessError("requirements lock digest does not match the runner snapshot")
    if requirements_source is not None:
        try:
            source_content = requirements_source.read_bytes()
        except OSError as error:
            raise ProcessError(f"cannot read checkout requirements lock: {error}") from error
        if source_content != content:
            raise ProcessError("private and checkout requirements locks differ")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise ProcessError("requirements lock must be UTF-8") from error
    matches = list(PIN.finditer(text))
    if len(matches) != 1:
        raise ProcessError("requirements lock must contain one engineering-process pin")
    match = matches[0]
    if match.group(1) != VERSION:
        raise ProcessError(
            f"requirements lock pins {match.group(1)}, but installed process is {VERSION}"
        )
    block = text[match.start() :]
    next_requirement = re.search(r"\n[a-zA-Z0-9][^\n]*==", block[1:])
    if next_requirement is not None:
        block = block[: next_requirement.start() + 1]
    if "--hash=sha256:" not in block:
        raise ProcessError("engineering-process pin is not hash locked")
    return content, digest


def _managed_block(
    existing: str,
    managed: str,
    *,
    start_marker: str = START_MARKER,
    end_marker: str = END_MARKER,
) -> str:
    if managed.count(start_marker) != 1 or managed.count(end_marker) != 1:
        raise ProcessError("managed template has invalid markers")
    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if start == -1 and end == -1:
        prefix = existing.rstrip()
        return (prefix + "\n\n" if prefix else "") + managed.rstrip() + "\n"
    if start == -1 or end == -1 or end < start:
        raise ProcessError("managed target has an incomplete engineering-process block")
    end += len(end_marker)
    if existing.find(start_marker, start + 1) != -1 or existing.find(end_marker, end) != -1:
        raise ProcessError("managed target has multiple engineering-process blocks")
    return existing[:start] + managed.rstrip() + existing[end:]


def _read_lock(project_root: Path, process_root: Path) -> dict[str, Any] | None:
    path = project_root / ".process" / "process.lock"
    if not path.exists():
        return None
    value = read_json(path)
    validate_document(
        value,
        "process-lock",
        schema_root=schemas_root(process_root),
        source=str(path),
    )
    return value


def _expected_files(
    project_root: Path,
    process_root: Path,
    requirements_digest: str,
) -> tuple[dict[Path, bytes], set[Path], dict[str, Any]]:
    old_lock = _read_lock(project_root, process_root)
    old_skills = set(old_lock.get("skills", [])) if old_lock else set()
    names = skill_names(process_root)
    new_skills = set(names)
    writes: dict[Path, bytes] = {}
    deletions: set[Path] = set()
    source_skills = skills_root(process_root)

    for name in names:
        source = source_skills / name
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ProcessError(f"{path}: managed skill sources cannot be symlinks")
            if path.is_file():
                relative = path.relative_to(source)
                writes[Path(".agents/skills") / name / relative] = path.read_bytes()
        metadata = {
            "schemaVersion": 1,
            "managedBy": "engineering-process",
            "version": VERSION,
            "digest": skill_digest(source),
        }
        writes[Path(".agents/skills") / name / ".engineering-process.json"] = (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    if old_lock and old_lock["schemaVersion"] == 2:
        old_managed = {
            _managed_inventory_path(path, old_skills)
            for path in old_lock["managedFiles"]
        }
    else:
        old_managed = {
            Path(".agents/skills") / name / relative
            for name in old_skills
            for relative in LEGACY_SKILL_FILES
            if (project_root / ".agents" / "skills" / name / relative).is_file()
        }
        if (project_root / ".process" / "adopt-process.py").is_file():
            old_managed.add(Path(".process/adopt-process.py"))
        if (project_root / ".process" / "adopt-process-windows-job.py").is_file():
            old_managed.add(Path(".process/adopt-process-windows-job.py"))

    for name in sorted(old_skills | new_skills):
        target = project_root / ".agents" / "skills" / name
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_dir():
            raise ProcessError(f"{target}: managed skill target is not a real directory")
        for path in target.rglob("*"):
            if path.is_symlink():
                raise ProcessError(f"{path}: managed skill target contains a symlink")
    new_managed = {
        relative
        for relative in writes
        if relative in {
            Path(".process/adopt-process.py"),
            Path(".process/adopt-process-windows-job.py"),
        }
        or relative.parts[:2] == (".agents", "skills")
    }
    for relative in new_managed:
        expected = writes[relative]
        path = project_root / relative
        if path.exists() and relative not in old_managed:
            if not path.is_file() or path.read_bytes() != expected:
                raise ProcessError(
                    f"{relative}: consumer-owned path collides with a managed file"
                )
    for relative in old_managed:
        if relative not in writes and (project_root / relative).is_file():
            deletions.add(relative)

    adopter = process_root / "templates" / "adopt-process.py"
    writes[Path(".process/adopt-process.py")] = adopter.read_bytes()
    windows_adopter = process_root / "templates" / "adopt-process-windows-job.py"
    writes[Path(".process/adopt-process-windows-job.py")] = windows_adopter.read_bytes()
    for relative in (
        Path(".process/adopt-process.py"),
        Path(".process/adopt-process-windows-job.py"),
    ):
        path = project_root / relative
        if (
            path.exists()
            and relative not in old_managed
            and (not path.is_file() or path.read_bytes() != writes[relative])
        ):
            raise ProcessError(
                f"{relative}: consumer-owned path collides with a managed file"
            )
        new_managed.add(relative)
    for legacy_file in (
        Path(".process/automation.json"),
    ):
        if (project_root / legacy_file).exists():
            deletions.add(legacy_file)
    migration_root = project_root / ".process" / "adoption-migrations"
    if migration_root.is_dir() and not migration_root.is_symlink():
        for path in migration_root.glob("*.json"):
            if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.json", path.name) and path.is_file() and not path.is_symlink():
                deletions.add(path.relative_to(project_root))

    agents_template = (process_root / "templates" / "AGENTS.process.md").read_text(
        encoding="utf-8"
    )
    agents_path = project_root / "AGENTS.md"
    existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    writes[Path("AGENTS.md")] = _managed_block(existing_agents, agents_template).encode(
        "utf-8"
    )

    pull_request_template = (
        process_root / "templates" / "PULL_REQUEST_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    pull_request_path = project_root / ".github" / "PULL_REQUEST_TEMPLATE.md"
    existing_pull_request = (
        pull_request_path.read_text(encoding="utf-8")
        if pull_request_path.exists()
        else ""
    )
    writes[Path(".github/PULL_REQUEST_TEMPLATE.md")] = _managed_block(
        existing_pull_request,
        pull_request_template,
        start_marker=PR_START_MARKER,
        end_marker=PR_END_MARKER,
    ).encode("utf-8")

    manifest_path = project_path(project_root)
    if manifest_path.exists():
        normalized = normalize_project(read_json(manifest_path), process_root)
        writes[manifest_path.relative_to(project_root)] = (
            json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    deletions.difference_update(writes)

    lock = {
        "schemaVersion": 2,
        "process": {
            "package": "engineering-process",
            "version": VERSION,
            "digest": distribution_digest(process_root),
        },
        "requirementsDigest": requirements_digest,
        "skills": list(names),
        "managedFiles": sorted(
            relative.as_posix()
            for relative in new_managed
        ),
    }
    validate_document(
        lock,
        "process-lock",
        schema_root=schemas_root(process_root),
        source="generated process lock",
    )
    writes[Path(".process/process.lock")] = (
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    total = sum(len(value) for value in writes.values())
    if total > MAX_MANAGED_BYTES:
        raise ProcessError("managed adoption output exceeds its aggregate limit")
    return writes, deletions, lock


def _target(project_root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ProcessError(f"unsafe managed path: {relative}")
    current = project_root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ProcessError(f"managed path traverses symlink: {relative}")
    target = project_root / relative
    if target.is_symlink():
        raise ProcessError(f"managed target is a symlink: {relative}")
    return target


def _drift(
    project_root: Path,
    writes: dict[Path, bytes],
    deletions: set[Path],
) -> tuple[list[Path], list[Path]]:
    changed: list[Path] = []
    present: list[Path] = []
    for relative, expected in sorted(writes.items(), key=lambda item: item[0].as_posix()):
        path = _target(project_root, relative)
        try:
            actual = path.read_bytes()
        except FileNotFoundError:
            changed.append(relative)
        except OSError as error:
            raise ProcessError(f"cannot inspect managed file {relative}: {error}") from error
        else:
            if actual != expected:
                changed.append(relative)
    for relative in sorted(deletions, key=lambda item: item.as_posix()):
        path = _target(project_root, relative)
        if path.exists():
            present.append(relative)
    return changed, present


def _apply_transaction(
    project_root: Path,
    writes: dict[Path, bytes],
    deletions: set[Path],
    *,
    precondition: Callable[[], None],
    postcondition: Callable[[], None],
) -> None:
    targets = sorted(set(writes) | deletions, key=lambda item: item.as_posix())
    backups: dict[Path, tuple[bytes | None, int | None]] = {}
    for relative in targets:
        path = _target(project_root, relative)
        if path.exists():
            if not path.is_file():
                raise ProcessError(f"managed target is not a file: {relative}")
            backups[relative] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        else:
            backups[relative] = (None, None)

    staged: dict[Path, Path] = {}
    try:
        for relative, data in sorted(
            writes.items(), key=lambda item: item[0].as_posix()
        ):
            path = _target(project_root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.adoption-",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if relative in {
                    Path(".process/adopt-process.py"),
                    Path(".process/adopt-process-windows-job.py"),
                } and os.name != "nt":
                    temporary.chmod(0o755)
                elif os.name != "nt":
                    temporary.chmod(0o644)
                if temporary.parent.resolve(strict=True) != path.parent.resolve(strict=True):
                    raise ProcessError(f"managed staging path escaped: {relative}")
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            staged[relative] = temporary
    except BaseException:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        raise

    def restore() -> list[str]:
        errors: list[str] = []
        for relative, (data, mode) in reversed(list(backups.items())):
            path = project_root / relative
            try:
                if path.is_symlink():
                    path.unlink()
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{path.name}.rollback-",
                        dir=path.parent,
                    )
                    temporary = Path(temporary_name)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if mode is not None:
                        temporary.chmod(mode)
                    os.replace(temporary, path)
            except BaseException as error:
                errors.append(f"{relative}: {error}")
        for relative, (data, mode) in backups.items():
            path = project_root / relative
            try:
                if data is None:
                    if path.exists() or path.is_symlink():
                        errors.append(f"{relative}: newly created path remains")
                elif (
                    not path.is_file()
                    or path.is_symlink()
                    or path.read_bytes() != data
                    or (mode is not None and stat.S_IMODE(path.stat().st_mode) != mode)
                ):
                    errors.append(f"{relative}: restored bytes or mode differ")
            except OSError as error:
                errors.append(f"{relative}: cannot verify rollback: {error}")
        return errors

    try:
        precondition()
        for relative in sorted(writes, key=lambda item: item.as_posix()):
            path = _target(project_root, relative)
            os.replace(staged.pop(relative), path)
        for relative in sorted(deletions, key=lambda item: item.as_posix(), reverse=True):
            path = _target(project_root, relative)
            path.unlink(missing_ok=True)
        postcondition()
    except BaseException as error:
        rollback_errors = restore()
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        if rollback_errors:
            raise ProcessError(
                "adoption transaction failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        raise ProcessError(
            f"adoption transaction failed and was rolled back: {error}"
        ) from error
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)

    for directory in sorted(
        {(_target(project_root, relative)).parent for relative in deletions},
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        while directory != project_root:
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def check_adoption(
    project_root: Path,
    process_root: Path,
    requirements_lock: Path,
) -> dict[str, Any]:
    _content, requirements_digest = _requirements(requirements_lock)
    writes, deletions, lock = _expected_files(
        project_root.resolve(), process_root.resolve(), requirements_digest
    )
    changed, present = _drift(project_root.resolve(), writes, deletions)
    issues = [f"managed file differs: {path.as_posix()}" for path in changed]
    issues.extend(f"obsolete managed file remains: {path.as_posix()}" for path in present)
    return {
        "version": VERSION,
        "digest": lock["process"]["digest"],
        "requirementsDigest": requirements_digest,
        "issues": issues,
        "status": "passed" if not issues else "failed",
    }


def apply_adoption(
    project_root: Path,
    process_root: Path,
    requirements_lock: Path,
    *,
    requirements_source: Path | None = None,
    expected_requirements_digest: str | None = None,
) -> dict[str, Any]:
    content, requirements_digest = _requirements(
        requirements_lock,
        requirements_source=requirements_source,
        expected_digest=expected_requirements_digest,
    )
    project_root = project_root.resolve()
    writes, deletions, lock = _expected_files(
        project_root, process_root.resolve(), requirements_digest
    )
    changed, present = _drift(project_root, writes, deletions)

    def requirements_unchanged() -> None:
        if requirements_lock.read_bytes() != content:
            raise ProcessError("private requirements snapshot changed during adoption")
        if (
            requirements_source is not None
            and requirements_source.read_bytes() != content
        ):
            raise ProcessError("checkout requirements lock changed during adoption")

    def converged() -> None:
        requirements_unchanged()
        remaining_changed, remaining_present = _drift(
            project_root, writes, deletions
        )
        if remaining_changed or remaining_present:
            raise ProcessError("adoption did not converge to the expected managed state")

    if changed or present:
        _apply_transaction(
            project_root,
            writes,
            deletions,
            precondition=requirements_unchanged,
            postcondition=converged,
        )
    else:
        converged()
    return {
        "version": VERSION,
        "digest": lock["process"]["digest"],
        "requirementsDigest": requirements_digest,
        "status": "applied" if changed or present else "unchanged",
        "changed": [path.as_posix() for path in changed],
        "deleted": [path.as_posix() for path in present],
    }
