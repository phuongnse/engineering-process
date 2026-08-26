from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time

from .contracts import ContractError
from .process_graph import load_process_graph
from .skills import MARKER_NAME, validate_skills


MAX_DISTRIBUTION_ENTRIES = 8_192
MAX_DISTRIBUTION_FILES = 4_096
MAX_DISTRIBUTION_FILE_BYTES = 2_000_000
MAX_DISTRIBUTION_TOTAL_BYTES = 32_000_000
MAX_DISTRIBUTION_PATH_BYTES = 1_000_000
DISTRIBUTION_TRAVERSAL_TIMEOUT_SECONDS = 10.0


def asset_root(process_root: Path) -> Path:
    candidates = (
        process_root,
        process_root / "share" / "engineering-process",
    )
    for candidate in candidates:
        if (candidate / "bundles.json").is_file() and (candidate / "skills").is_dir():
            return candidate
        if (
            (candidate / "bundles.json").is_file()
            and (candidate / "process_assets" / "skills").is_dir()
        ):
            return candidate
    raise ContractError(f"{process_root}: cannot locate engineering-process assets")


def skills_root(process_root: Path) -> Path:
    root = asset_root(process_root)
    candidates = (root / "process_assets" / "skills", root / "skills")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ContractError(f"{process_root}: cannot locate engineering-process skills")


def _files(
    root: Path, *, deadline: float, budget: dict[str, int]
) -> list[Path]:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ContractError(f"cannot resolve distribution root {root}: {error}") from error
    if not resolved_root.is_dir():
        raise ContractError(f"distribution root must be a directory: {root}")
    files: list[Path] = []
    stack = [resolved_root]
    while stack:
        if time.monotonic() >= deadline:
            raise ContractError("distribution traversal exceeded 10 seconds")
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise ContractError(f"cannot enumerate distribution path {directory}: {error}") from error
        directories: list[Path] = []
        for child in children:
            budget["entries"] += 1
            if budget["entries"] > MAX_DISTRIBUTION_ENTRIES:
                raise ContractError(
                    f"distribution traversal exceeds {MAX_DISTRIBUTION_ENTRIES} entries"
                )
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise ContractError(
                    f"cannot inspect distribution path {child.path}: {error}"
                ) from error
            path = Path(child.path)
            try:
                relative = path.relative_to(resolved_root).as_posix()
                budget["pathBytes"] += len(relative.encode("utf-8"))
            except (UnicodeEncodeError, ValueError) as error:
                raise ContractError(
                    f"distribution path is not portable: {path}"
                ) from error
            if budget["pathBytes"] > MAX_DISTRIBUTION_PATH_BYTES:
                raise ContractError(
                    f"distribution paths exceed {MAX_DISTRIBUTION_PATH_BYTES} bytes"
                )
            if stat.S_ISLNK(metadata.st_mode):
                raise ContractError(f"distribution path must not be a symlink: {path}")
            if stat.S_ISDIR(metadata.st_mode):
                if child.name != "__pycache__":
                    directories.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ContractError(
                    f"distribution path must be a regular file or directory: {path}"
                )
            if child.name == MARKER_NAME:
                continue
            files.append(path)
            if len(files) > MAX_DISTRIBUTION_FILES:
                raise ContractError(
                    f"distribution traversal exceeds {MAX_DISTRIBUTION_FILES} files"
                )
        stack.extend(reversed(directories))
    return sorted(files)


def _update_digest_from_file(
    digest: object,
    path: Path,
    *,
    deadline: float,
    total_bytes: int,
) -> int:
    if time.monotonic() >= deadline:
        raise ContractError("distribution hashing exceeded 10 seconds")
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"distribution input must be a regular file: {path}")
        if before.st_size > MAX_DISTRIBUTION_FILE_BYTES:
            raise ContractError(
                f"distribution file exceeds {MAX_DISTRIBUTION_FILE_BYTES} bytes: {path}"
            )
        total_bytes += before.st_size
        if total_bytes > MAX_DISTRIBUTION_TOTAL_BYTES:
            raise ContractError(
                f"distribution bytes exceed {MAX_DISTRIBUTION_TOTAL_BYTES}"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ContractError(f"distribution file changed while opening: {path}")
            count = 0
            while chunk := stream.read(64 * 1024):
                if time.monotonic() >= deadline:
                    raise ContractError("distribution hashing exceeded 10 seconds")
                count += len(chunk)
                if count > before.st_size:
                    raise ContractError(f"distribution file changed while hashing: {path}")
                digest.update(chunk)
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"cannot hash distribution file {path}: {error}") from error
    if (
        count != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"distribution file changed while hashing: {path}")
    return total_bytes


def distribution_digest(
    process_root: Path,
    selected_skills: tuple[str, ...],
    *,
    package_root: Path | None = None,
) -> str:
    deadline = time.monotonic() + DISTRIBUTION_TRAVERSAL_TIMEOUT_SECONDS
    traversal_budget = {"entries": 0, "pathBytes": 0}
    root = asset_root(process_root).resolve()
    skill_root = skills_root(process_root).resolve()
    issues = validate_skills(skill_root, selected_skills)
    if issues:
        raise ContractError("\n".join(issues))
    graph_required = "run-change" in selected_skills
    if graph_required:
        load_process_graph(process_root, skill_root, selected_skills)

    runtime_root = (
        Path(__file__).resolve().parent
        if package_root is None
        else package_root.resolve()
    )
    entries: list[tuple[str, Path]] = []
    for path in _files(
        runtime_root, deadline=deadline, budget=traversal_budget
    ):
        if path.suffix in {".py", ".txt"}:
            entries.append(
                (
                    f"runtime/engineering_process/{path.relative_to(runtime_root).as_posix()}",
                    path,
                )
            )

    entries.append(("assets/bundles.json", root / "bundles.json"))
    if graph_required:
        entries.append(("assets/process-graph.json", root / "process-graph.json"))
    release_contract = root / "release.json"
    if release_contract.is_file():
        entries.append(("assets/release.json", release_contract))
    improvement_catalog = root / "improvement-catalog.json"
    if improvement_catalog.is_file():
        entries.append(("assets/improvement-catalog.json", improvement_catalog))
    for policy_name in (
        "PROCESS_IMPROVEMENT.md",
        "PRODUCTION_STANDARD.md",
        "VERSIONING.md",
    ):
        policy = root / policy_name
        if policy.is_file():
            entries.append((f"assets/{policy_name}", policy))
    for directory in ("schemas", "examples", "templates"):
        candidate = root / directory
        if candidate.is_dir():
            entries.extend(
                (f"assets/{path.relative_to(root).as_posix()}", path)
                for path in _files(
                    candidate, deadline=deadline, budget=traversal_budget
                )
            )
    for skill in selected_skills:
        directory = skill_root / skill
        entries.extend(
            (f"assets/skills/{skill}/{path.relative_to(directory).as_posix()}", path)
            for path in _files(
                directory, deadline=deadline, budget=traversal_budget
            )
        )

    if len(entries) > MAX_DISTRIBUTION_FILES:
        raise ContractError(
            f"distribution digest exceeds {MAX_DISTRIBUTION_FILES} files"
        )
    digest = hashlib.sha256()
    total_bytes = 0
    for logical_path, path in sorted(entries):
        if time.monotonic() >= deadline:
            raise ContractError("distribution hashing exceeded 10 seconds")
        digest.update(logical_path.encode("utf-8"))
        digest.update(b"\0")
        total_bytes = _update_digest_from_file(
            digest,
            path,
            deadline=deadline,
            total_bytes=total_bytes,
        )
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
