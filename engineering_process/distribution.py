"""Locate and fingerprint the portable process distribution."""

from __future__ import annotations

import hashlib
import sys
import sysconfig
from pathlib import Path
from typing import Iterable

from .contracts import ProcessError


MAX_DISTRIBUTION_FILES = 256
MAX_DISTRIBUTION_BYTES = 10_000_000


def distribution_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
        if not (root / "process_assets" / "skills").is_dir():
            raise ProcessError(f"{root}: process_assets/skills is missing")
        return root

    source = Path(__file__).resolve().parent.parent
    if (source / "process_assets" / "skills").is_dir():
        return source

    candidates = (
        Path(sysconfig.get_path("data")) / "share" / "engineering-process",
        Path(sys.prefix) / "share" / "engineering-process",
    )
    for candidate in candidates:
        if (candidate / "process_assets" / "skills").is_dir():
            return candidate.resolve()
        # Wheels built before 1.0 placed skills directly under share/.../skills.
        if (candidate / "skills").is_dir():
            return candidate.resolve()
    raise ProcessError("cannot locate installed engineering-process assets")


def skills_root(root: Path) -> Path:
    modern = root / "process_assets" / "skills"
    return modern if modern.is_dir() else root / "skills"


def schemas_root(root: Path) -> Path:
    path = root / "schemas"
    if not path.is_dir():
        raise ProcessError(f"{root}: schemas directory is missing")
    return path


def skill_names(root: Path) -> tuple[str, ...]:
    directory = skills_root(root)
    names = tuple(
        sorted(
            child.name
            for child in directory.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        )
    )
    if not names:
        raise ProcessError(f"{directory}: no skills found")
    return names


def _asset_paths(root: Path) -> Iterable[tuple[str, Path]]:
    skill_directory = skills_root(root)
    for path in sorted(skill_directory.rglob("*")):
        if path.is_file():
            yield f"skills/{path.relative_to(skill_directory).as_posix()}", path
    for path in sorted(schemas_root(root).glob("*.json")):
        yield f"schemas/{path.name}", path
    for name in (
        "AGENTS.process.md",
        "PULL_REQUEST_TEMPLATE.md",
        "adopt-process.py",
        "adopt-process-windows-job.py",
    ):
        path = root / "templates" / name
        if not path.is_file():
            raise ProcessError(f"{path}: required distribution asset is missing")
        yield f"templates/{name}", path
    graph = root / "process-graph.json"
    if not graph.is_file():
        raise ProcessError(f"{graph}: required distribution asset is missing")
    yield "process-graph.json", graph


def distribution_digest(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    total = 0
    for relative, path in _asset_paths(root):
        if path.is_symlink():
            raise ProcessError(f"{path}: distribution assets cannot be symlinks")
        data = path.read_bytes()
        count += 1
        total += len(data)
        if count > MAX_DISTRIBUTION_FILES or total > MAX_DISTRIBUTION_BYTES:
            raise ProcessError("distribution asset bounds exceeded")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def skill_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file() or file_path.name == ".engineering-process.json":
            continue
        relative = file_path.relative_to(path).as_posix().encode("utf-8")
        data = file_path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()
