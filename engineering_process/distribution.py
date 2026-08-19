from __future__ import annotations

import hashlib
from pathlib import Path

from .contracts import ContractError
from .skills import MARKER_NAME, validate_skills


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


def _files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != MARKER_NAME
        and "__pycache__" not in path.parts
    )


def distribution_digest(
    process_root: Path,
    selected_skills: tuple[str, ...],
    *,
    package_root: Path | None = None,
) -> str:
    root = asset_root(process_root)
    skill_root = skills_root(process_root)
    issues = validate_skills(skill_root, selected_skills)
    if issues:
        raise ContractError("\n".join(issues))

    runtime_root = (
        Path(__file__).resolve().parent
        if package_root is None
        else package_root.resolve()
    )
    entries: list[tuple[str, Path]] = []
    for path in _files(runtime_root):
        if path.suffix in {".py", ".txt"}:
            entries.append(
                (
                    f"runtime/engineering_process/{path.relative_to(runtime_root).as_posix()}",
                    path,
                )
            )

    entries.append(("assets/bundles.json", root / "bundles.json"))
    release_contract = root / "release.json"
    if release_contract.is_file():
        entries.append(("assets/release.json", release_contract))
    for policy_name in ("PRODUCTION_STANDARD.md", "VERSIONING.md"):
        policy = root / policy_name
        if policy.is_file():
            entries.append((f"assets/{policy_name}", policy))
    for directory in ("schemas", "examples", "templates"):
        candidate = root / directory
        if candidate.is_dir():
            entries.extend(
                (f"assets/{path.relative_to(root).as_posix()}", path)
                for path in _files(candidate)
            )
    for skill in selected_skills:
        directory = skill_root / skill
        entries.extend(
            (f"assets/skills/{skill}/{path.relative_to(directory).as_posix()}", path)
            for path in _files(directory)
        )

    digest = hashlib.sha256()
    for logical_path, path in sorted(entries):
        digest.update(logical_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
