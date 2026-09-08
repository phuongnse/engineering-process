#!/usr/bin/env python3
"""Render the producer's reviewed release contents from its canonical manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ProcessError, load_and_validate  # noqa: E402
from engineering_process.distribution import schemas_root  # noqa: E402


REPOSITORY = "https://github.com/phuongnse/engineering-process"


def _text(value: str) -> str:
    return re.sub(r"[\\`*_{}\[\]<>#|]", lambda match: "\\" + match.group(), " ".join(value.split()))


def render_release_notes(release: dict) -> str:
    version, previous = release["version"], release["previousVersion"]
    lines = [f"# Engineering Process v{version}", "", f"Changes since v{previous}.", ""]
    for kind, heading in (("breaking", "Breaking changes"), ("capability", "Features"), ("fix", "Fixes")):
        changes = [change for change in release["changes"] if change["type"] == kind]
        if not changes:
            continue
        lines.extend([f"## {heading}", ""])
        for change in changes:
            source = change["source"].strip()
            if source.startswith(("https://", "http://")):
                issue = re.fullmatch(re.escape(REPOSITORY) + r"/(?:issues|pull)/([1-9][0-9]*)", source)
                label = f"#{issue.group(1)}" if issue else "Source"
                reference = f"[{label}]({quote(source, safe=':/?#&=%@+~')})"
            else:
                reference = _text(source)
            lines.append(f"- {_text(change['summary'])} ({reference})")
        lines.append("")
    lines.extend([
        "## Upgrade and compatibility", "",
        "Merge the complete hash-locked package/adoption PR, update the local and CI environments to the selected version, and start a fresh agent session.", "",
        "Consumer CI, naming conventions and branch-protection settings remain consumer-owned; adoption does not configure them automatically.", "",
        f"See [versioning and compatibility]({REPOSITORY}/blob/v{version}/VERSIONING.md) and [adoption guidance]({REPOSITORY}/blob/v{version}/SELF_HOSTING.md).", "",
        f"[Full change comparison]({REPOSITORY}/compare/v{previous}...v{version})", "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check", type=Path)
    args = parser.parse_args(argv)
    release = load_and_validate(PROJECT_ROOT / "release.json", "release", schema_root=schemas_root(PROJECT_ROOT))
    expected = render_release_notes(release).encode("utf-8")
    if args.check:
        if args.check.read_bytes() != expected:
            raise ProcessError(f"release notes are stale: {args.check}")
        print("Release contents: PASSED")
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
