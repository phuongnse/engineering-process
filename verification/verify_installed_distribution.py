from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys


MAX_RELEASE_BYTES = 1_000_000


class InstalledDistributionError(RuntimeError):
    pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise InstalledDistributionError(f"{path}: expected a regular file")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise InstalledDistributionError(f"{path}: expected a regular file")
    with resolved.open("rb") as stream:
        content = stream.read(MAX_RELEASE_BYTES + 1)
    if len(content) > MAX_RELEASE_BYTES:
        raise InstalledDistributionError(
            f"{path}: exceeds {MAX_RELEASE_BYTES} bytes"
        )
    return content


def verify_installed_distribution(source_root: Path) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    source_release_path = source_root / "release.json"
    source_release_bytes = _bounded_bytes(source_release_path)
    try:
        source_release = json.loads(source_release_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstalledDistributionError(
            f"{source_release_path}: invalid JSON: {error}"
        ) from error

    import engineering_process
    from engineering_process.contracts import validate_release
    from engineering_process.distribution import (
        asset_root,
        distribution_digest,
        skills_root,
    )
    from engineering_process.runtime import assert_runtime_dependencies
    from engineering_process.skills import validate_skills
    from engineering_process.syncing import default_process_root

    package_path = Path(engineering_process.__file__).resolve(strict=True)
    if _is_within(package_path, source_root):
        raise InstalledDistributionError(
            "installed engineering_process resolves inside source checkout: "
            f"{package_path}"
        )

    release = validate_release(source_release, str(source_release_path))
    installed_version = metadata.version("engineering-process")
    if (
        installed_version != release.version
        or engineering_process.VERSION != release.version
    ):
        raise InstalledDistributionError(
            "installed version does not match release.json: "
            f"metadata={installed_version}, runtime={engineering_process.VERSION}, "
            f"release={release.version}"
        )

    process_root = default_process_root().resolve(strict=True)
    if _is_within(process_root, source_root):
        raise InstalledDistributionError(
            f"installed process root resolves inside source checkout: {process_root}"
        )
    installed_assets = asset_root(process_root).resolve(strict=True)
    installed_release_path = installed_assets / "release.json"
    if _bounded_bytes(installed_release_path) != source_release_bytes:
        raise InstalledDistributionError(
            "installed release.json bytes do not match the verified source"
        )

    skill_root = skills_root(process_root)
    selected_skills = tuple(
        sorted(
            path.name
            for path in skill_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        )
    )
    skill_issues = validate_skills(skill_root, selected_skills)
    if skill_issues:
        raise InstalledDistributionError("\n".join(skill_issues))
    assert_runtime_dependencies()

    entry_point = shutil.which("processctl")
    if entry_point is None:
        raise InstalledDistributionError("processctl entry point is not available")
    entry_point_path = Path(entry_point).resolve(strict=True)
    if _is_within(entry_point_path, source_root):
        raise InstalledDistributionError(
            f"processctl entry point resolves inside source checkout: {entry_point_path}"
        )

    return {
        "command": "verify installed distribution",
        "digest": distribution_digest(process_root, selected_skills),
        "entryPoint": str(entry_point_path),
        "package": "engineering-process",
        "packagePath": str(package_path),
        "processRoot": str(process_root),
        "skills": list(selected_skills),
        "status": "passed",
        "version": installed_version,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        result = verify_installed_distribution(arguments.source_root)
    except Exception as error:
        print(f"installed distribution verify: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, sort_keys=True))
