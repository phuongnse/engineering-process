#!/usr/bin/env python3
"""Render the producer's reviewed release contents from its canonical manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ProcessError, load_and_validate  # noqa: E402
from engineering_process.distribution import schemas_root  # noqa: E402
from engineering_process.artifact_standards import resolve_standard  # noqa: E402
from engineering_process.release_notes import render_notes  # noqa: E402


REPOSITORY = "https://github.com/phuongnse/engineering-process"


def release_notes_data(release: dict) -> dict:
    version, previous = release["version"], release["previousVersion"]
    return {
        "schemaVersion": 1,
        "title": f"Engineering Process v{version}",
        "introduction": f"Changes since v{previous}.",
        "repositoryUrl": REPOSITORY,
        "changes": [{key: change[key] for key in ("type", "summary", "source")} for change in release["changes"]],
        "sections": {"upgrade": "\n\n".join([
            "Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.",
            "Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.",
            f"See [versioning and compatibility]({REPOSITORY}/blob/v{version}/VERSIONING.md) and [adoption guidance]({REPOSITORY}/blob/v{version}/SELF_HOSTING.md).",
        ])},
        "footer": f"[Full change comparison]({REPOSITORY}/compare/v{previous}...v{version})",
    }


def render_release_notes(release: dict) -> str:
    standard = resolve_standard(PROJECT_ROOT, PROJECT_ROOT, "release-notes")
    return render_notes(standard, release_notes_data(release), process_root=PROJECT_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    release = load_and_validate(PROJECT_ROOT / "release.json", "release", schema_root=schemas_root(PROJECT_ROOT))
    standard = resolve_standard(PROJECT_ROOT, PROJECT_ROOT, "release-notes")
    expected = render_notes(standard, release_notes_data(release), process_root=PROJECT_ROOT).encode("utf-8")
    if args.check:
        if args.check.read_bytes() != expected:
            raise ProcessError(f"release notes are stale: {args.check}")
        print(f"Release contents: PASSED; {standard.document['id']}@{standard.document['version']} {standard.metadata['digest']}")
    else:
        args.output.write_bytes(expected)
        print(f"Release contents written: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ProcessError) as error:
        print(f"Release contents: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
