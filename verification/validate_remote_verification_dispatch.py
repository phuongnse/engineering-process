from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


GIT_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_REF = re.compile(
    r"^refs/tags/engineering-process-verification/"
    r"(?P<change>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)/"
    r"cycle-(?P<cycle>[1-9][0-9]{0,6})/"
    r"(?P<base>[0-9a-f]{40})/(?P<head>[0-9a-f]{40})/"
    r"(?P<request>[0-9a-f]{64})$"
)
BOOTSTRAP_CHANGE = "evidence-valid-remote-verification"
BOOTSTRAP_CYCLE = "1"
BOOTSTRAP_BASE = "842627fe8d6cc4e7cb58112d63a32c2e7df467c3"
BOOTSTRAP_AUTHORIZATION = (
    "sha256:d11be9e012dc98983b53949d2b7a5b191044e393281f46d72566794f46d78eac"
)


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )


def validate_dispatch(
    project_root: Path,
    *,
    source_ref: str,
    checkpoint: str,
    comparison_base: str,
    request_sha256: str,
    expected_workflow_sha: str,
    actual_workflow_sha: str,
    event_name: str,
    bootstrap_authorization_sha256: str | None = None,
) -> dict[str, object]:
    match = SOURCE_REF.fullmatch(source_ref)
    if match is None:
        raise ValueError("remote source ref is not a bounded verification tag")
    for value, label in (
        (checkpoint, "checkpoint"),
        (comparison_base, "comparison base"),
        (expected_workflow_sha, "expected workflow checkpoint"),
        (actual_workflow_sha, "actual workflow checkpoint"),
    ):
        if GIT_OID.fullmatch(value) is None:
            raise ValueError(f"{label} is not a full Git object id")
    if DIGEST.fullmatch(request_sha256) is None:
        raise ValueError("request digest is invalid")
    if event_name != "workflow_dispatch":
        raise ValueError("remote verification requires workflow_dispatch")
    if (
        match.group("base") != comparison_base
        or match.group("head") != checkpoint
        or match.group("request") != request_sha256.removeprefix("sha256:")
    ):
        raise ValueError("verification tag identity does not match the request")
    if actual_workflow_sha != expected_workflow_sha:
        raise ValueError("workflow checkpoint does not match the request")
    bootstrap = expected_workflow_sha != comparison_base
    if bootstrap and not (
        bootstrap_authorization_sha256 == BOOTSTRAP_AUTHORIZATION
        and match.group("change") == BOOTSTRAP_CHANGE
        and match.group("cycle") == BOOTSTRAP_CYCLE
        and comparison_base == BOOTSTRAP_BASE
        and expected_workflow_sha == checkpoint
    ):
        raise ValueError("remote verification workflow is not owned by the exact base")
    head = _git(project_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head.returncode != 0 or head.stdout.strip() != checkpoint:
        raise ValueError("checked-out source does not match the requested checkpoint")
    resolved_ref = _git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{source_ref}^{{commit}}"],
    )
    if resolved_ref.returncode != 0 or resolved_ref.stdout.strip() != checkpoint:
        raise ValueError("verification tag does not resolve to the requested checkpoint")
    ancestor = _git(
        project_root,
        ["merge-base", "--is-ancestor", comparison_base, checkpoint],
    )
    if ancestor.returncode != 0:
        raise ValueError("comparison base is not an ancestor of the checkpoint")
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-remote-verification-dispatch",
        "sourceRef": source_ref,
        "checkpoint": checkpoint,
        "comparisonBase": comparison_base,
        "requestSha256": request_sha256,
        "workflowSha": actual_workflow_sha,
        "bootstrap": bootstrap,
        "status": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an exact no-authority remote verification dispatch"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--comparison-base", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--expected-workflow-sha", required=True)
    parser.add_argument("--actual-workflow-sha", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--bootstrap-authorization-sha256")
    args = parser.parse_args(argv)
    try:
        result = validate_dispatch(
            args.project_root.resolve(strict=True),
            source_ref=args.source_ref,
            checkpoint=args.checkpoint,
            comparison_base=args.comparison_base,
            request_sha256=args.request_sha256,
            expected_workflow_sha=args.expected_workflow_sha,
            actual_workflow_sha=args.actual_workflow_sha,
            event_name=args.event_name,
            bootstrap_authorization_sha256=args.bootstrap_authorization_sha256,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"remote dispatch validation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
