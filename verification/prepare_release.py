#!/usr/bin/env python3
"""Materialize one deterministic version-changing Release PR."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (  # noqa: E402
    ProcessError,
    load_and_validate,
    validate_document,
    write_json_atomic,
)
from engineering_process.distribution import schemas_root  # noqa: E402
from engineering_process.release import derive_next_version  # noqa: E402


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ProcessError(f"{path}: expected exactly one {old!r}")
    temporary = path.with_name(f".{path.name}.release.tmp")
    temporary.write_text(text.replace(old, new), encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the next engineering-process release")
    parser.add_argument("version")
    args = parser.parse_args(argv)
    schema_root = schemas_root(PROJECT_ROOT)
    current = load_and_validate(
        PROJECT_ROOT / "release.json", "release", schema_root=schema_root
    )
    fragment_paths = sorted((PROJECT_ROOT / "release-changes").glob("*.json"))
    if not fragment_paths:
        raise ProcessError("no release change fragments exist")
    fragments = [
        load_and_validate(path, "release-change", schema_root=schema_root)
        for path in fragment_paths
    ]
    expected = derive_next_version(
        current["version"], (fragment["type"] for fragment in fragments)
    )
    if args.version != expected:
        raise ProcessError(f"requested {args.version}, but change types derive {expected}")
    release = {
        "schemaVersion": 5,
        "version": expected,
        "previousVersion": current["version"],
        "changes": [
            {key: fragment[key] for key in ("id", "type", "summary", "source")}
            for fragment in fragments
        ],
    }
    validate_document(release, "release", schema_root=schema_root, source="next release")
    _replace_once(
        PROJECT_ROOT / "pyproject.toml",
        f'version = "{current["version"]}"',
        f'version = "{expected}"',
    )
    _replace_once(
        PROJECT_ROOT / "engineering_process" / "__init__.py",
        f'VERSION = "{current["version"]}"',
        f'VERSION = "{expected}"',
    )
    write_json_atomic(PROJECT_ROOT / "release.json", release)
    for path in fragment_paths:
        path.unlink()
    print(expected)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ProcessError) as error:
        print(f"release preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
