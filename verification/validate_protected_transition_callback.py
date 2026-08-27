from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any


OID = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CallbackError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise CallbackError(f"{path}: must be a bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CallbackError(f"{path}: must contain UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise CallbackError(f"{path}: must contain an object")
    return value


def validate(
    policy: dict[str, Any],
    pull_request: dict[str, Any],
    *,
    repository: str,
    pull_request_number: int,
    source_base: str,
    candidate_head: str,
    current_base: str,
    workflow_sha: str,
    event_name: str,
) -> dict[str, Any]:
    if REPOSITORY.fullmatch(repository) is None:
        raise CallbackError("callback repository is invalid")
    if not all(OID.fullmatch(item) for item in (source_base, candidate_head, current_base, workflow_sha)):
        raise CallbackError("callback commits must be full lowercase Git ids")
    workflow = policy.get("workflow")
    verifier = policy.get("verifier")
    if not isinstance(workflow, dict) or not isinstance(verifier, dict):
        raise CallbackError("protected policy workflow or verifier is missing")
    entrypoint = verifier.get("entrypoint")
    entrypoint_path = PurePosixPath(entrypoint) if isinstance(entrypoint, str) else None
    if (
        event_name != "workflow_dispatch"
        or current_base != source_base
        or workflow_sha != source_base
        or policy.get("repository") != repository
        or workflow.get("repository") != repository
        or workflow.get("path") != ".github/workflows/authority-transition.yml"
        or verifier.get("repository") != repository
        or not isinstance(entrypoint_path, PurePosixPath)
        or entrypoint_path.is_absolute()
        or ".." in entrypoint_path.parts
        or entrypoint != "verification/validate_authority_transition.py"
    ):
        raise CallbackError("callback does not match the protected-base policy")
    if pull_request != {
        "number": pull_request_number,
        "baseRefOid": source_base,
        "headRefOid": candidate_head,
        "state": "OPEN",
    }:
        raise CallbackError("callback does not match one exact open pull request")
    return {
        "repository": repository,
        "pullRequest": pull_request_number,
        "sourceBase": source_base,
        "candidateHead": candidate_head,
        "workflowSha": workflow_sha,
        "verifierCommit": verifier.get("commit"),
        "verifierEntrypoint": entrypoint,
        "checkContext": workflow.get("checkContext"),
        "checkAppId": workflow.get("checkAppId"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the exact protected transition workflow callback"
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--pull-request", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request-number", type=int, required=True)
    parser.add_argument("--source-base", required=True)
    parser.add_argument("--candidate-head", required=True)
    parser.add_argument("--current-base", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--event-name", required=True)
    args = parser.parse_args(argv)
    result = validate(
        _read(args.policy),
        _read(args.pull_request),
        repository=args.repository,
        pull_request_number=args.pull_request_number,
        source_base=args.source_base,
        candidate_head=args.candidate_head,
        current_base=args.current_base,
        workflow_sha=args.workflow_sha,
        event_name=args.event_name,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CallbackError, OSError) as error:
        print(f"protected transition callback failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
