#!/usr/bin/env python3
"""Consumer publication checks using the installed process authority."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from engineering_process.contracts import ProcessError
from engineering_process.publication_compat import branch_issues, commit_issues, validate_pull_request, validate_range


def verify_publication(root: Path, *, pull_request: bool) -> list[str]:
    if not pull_request:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise ProcessError("local publication verification requires a named branch; PR CI must supply --pull-request context")
        branch = result.stdout.strip()
        # The consumer's integration branch is not a publication proposal.
        return [] if branch == "main" else branch_issues(branch)

    names = ("BRANCH", "TITLE", "BODY", "DRAFT", "BASE", "HEAD")
    missing = [name for name in names if f"PUBLICATION_{name}" not in os.environ]
    if missing:
        raise ProcessError("missing PR publication context: " + ", ".join(missing))
    context = {name: os.environ[f"PUBLICATION_{name}"] for name in names}
    if context["DRAFT"] not in {"true", "false"}:
        raise ProcessError("PR draft state must be true or false")
    if any(not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", context[name]) for name in ("BASE", "HEAD")):
        raise ProcessError("PR base and head must be full commit SHAs")
    with tempfile.TemporaryDirectory(prefix="publication-metadata-") as directory:
        body = Path(directory) / "body.md"
        body.write_bytes(context["BODY"].encode("utf-8"))
        result = validate_pull_request(
            title=context["TITLE"], branch=context["BRANCH"],
            state="draft" if context["DRAFT"] == "true" else "ready", body_path=body,
            project_root=root,
        )
    head = subprocess.run(
        ["git", "log", "-1", "--format=%s", "--end-of-options", context["HEAD"]],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    if head.returncode:
        raise ProcessError("cannot read the exact PR head commit subject")
    commit_result = validate_range(root, context["BRANCH"], f"{context['BASE']}..{context['HEAD']}")
    return result["issues"] + commit_issues(head.stdout.rstrip("\r\n")) + commit_result["issues"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-request", action="store_true")
    args = parser.parse_args()
    try:
        issues = verify_publication(Path.cwd(), pull_request=args.pull_request)
    except (OSError, ProcessError, subprocess.TimeoutExpired) as error:
        print(f"Publication policy: FAILED: {error}", file=sys.stderr)
        return 1
    if issues:
        print("Publication policy: FAILED\n" + "\n".join(issues), file=sys.stderr)
        return 1
    print("Publication policy: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
