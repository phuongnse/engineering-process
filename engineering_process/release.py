"""Small release identity checks and SemVer derivation."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Any, Iterable

from . import VERSION
from .contracts import ProcessError, load_and_validate
from .distribution import schemas_root


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ProcessError(f"version is not final SemVer: {value}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def derive_next_version(previous: str, change_types: Iterable[str]) -> str:
    major, minor, patch = parse_version(previous)
    kinds = set(change_types)
    if not kinds or not kinds <= {"fix", "capability", "breaking"}:
        raise ProcessError("release changes must use fix, capability, or breaking")
    if "breaking" in kinds:
        return f"{major + 1}.0.0"
    if "capability" in kinds:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def validate_release(
    project_root: Path,
    process_root: Path,
    *,
    tag: str | None = None,
) -> dict[str, Any]:
    document = load_and_validate(
        project_root / "release.json",
        "release",
        schema_root=schemas_root(process_root),
    )
    try:
        pyproject = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ProcessError(f"cannot read pyproject.toml: {error}") from error
    package_version = pyproject.get("project", {}).get("version")
    if package_version != document["version"] or VERSION != document["version"]:
        raise ProcessError(
            "release.json, pyproject.toml, and engineering_process.VERSION must match"
        )
    if tag is not None and tag != f"v{document['version']}":
        raise ProcessError(f"tag must be v{document['version']}")
    expected = derive_next_version(
        document["previousVersion"],
        (change["type"] for change in document["changes"]),
    )
    if document["version"] != expected:
        raise ProcessError(
            f"release version {document['version']} does not match derived {expected}"
        )
    return {
        "version": document["version"],
        "previousVersion": document["previousVersion"],
        "tag": f"v{document['version']}",
        "changeCount": len(document["changes"]),
    }
