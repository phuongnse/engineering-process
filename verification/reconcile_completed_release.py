from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError


MAX_OPEN_PULL_REQUEST_STATE_BYTES = 200_000
MAX_RELEASE_PULL_REQUEST_BODY_BYTES = 65_536
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BRANCH_PATTERN = re.compile(
    r"^automation/[a-z0-9]+(?:-[a-z0-9]+)*/"
    r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$"
)


def _read_bounded(path: Path, *, limit: int, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if len(content) > limit:
        raise ContractError(f"{label} exceeds {limit} bytes")
    return content


def reconcile_completed_release(
    *,
    open_pull_requests: object,
    remote_head_sha: str,
    expected_head_sha: str,
    expected_base: str,
    expected_branch: str,
    expected_title: str,
    expected_body: str,
) -> str:
    if SHA_PATTERN.fullmatch(expected_head_sha) is None:
        raise ContractError("expected release head must be a full lowercase Git SHA")
    if remote_head_sha and SHA_PATTERN.fullmatch(remote_head_sha) is None:
        raise ContractError("remote release head must be empty or a full lowercase Git SHA")
    if BRANCH_PATTERN.fullmatch(expected_branch) is None:
        raise ContractError("expected release branch must be an automation branch")
    if not expected_base or len(expected_base) > 255:
        raise ContractError("expected release base must be a bounded non-empty branch")
    if not expected_title or len(expected_title) > 256:
        raise ContractError("expected Release PR title must be bounded and non-empty")
    if not expected_body or len(expected_body.encode("utf-8")) > MAX_RELEASE_PULL_REQUEST_BODY_BYTES:
        raise ContractError("expected Release PR body must be bounded and non-empty")
    if not isinstance(open_pull_requests, list):
        raise ContractError("open Release PR state must be a JSON array")
    if len(open_pull_requests) > 20:
        raise ContractError("Release PR history exceeds the bounded reconciliation set")
    records: list[dict[str, object]] = []
    for pull_request in open_pull_requests:
        if not isinstance(pull_request, dict):
            raise ContractError("Release PR state contains an invalid record")
        records.append(pull_request)
    matching_branch = [
        pull_request
        for pull_request in records
        if pull_request.get("baseRefName") == expected_base
        and pull_request.get("headRefName") == expected_branch
    ]
    current = [
        pull_request
        for pull_request in matching_branch
        if pull_request.get("headRefOid") == expected_head_sha
    ]
    if len(current) > 1:
        raise ContractError("multiple Release PRs identify the exact completed head")
    if current:
        pull_request = current[0]
        expected = {
            "baseRefName": expected_base,
            "body": expected_body,
            "headRefName": expected_branch,
            "headRefOid": expected_head_sha,
            "isDraft": False,
            "title": expected_title,
        }
        mismatches = [
            field
            for field, value in expected.items()
            if pull_request.get(field) != value
        ]
        number = pull_request.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            mismatches.append("number")
        state = pull_request.get("state")
        if state == "OPEN":
            if remote_head_sha != expected_head_sha:
                mismatches.append("remoteHeadSha")
        elif state == "MERGED":
            merged_at = pull_request.get("mergedAt")
            if not isinstance(merged_at, str) or not merged_at:
                mismatches.append("mergedAt")
        else:
            mismatches.append("state")
        if mismatches:
            raise ContractError(
                "existing Release PR does not match the exact completed publication: "
                + ", ".join(sorted(set(mismatches)))
            )
        return "existing" if state == "OPEN" else "merged"

    conflicting_open = [
        pull_request
        for pull_request in matching_branch
        if pull_request.get("state") == "OPEN"
    ]
    if conflicting_open:
        raise ContractError("open Release PR does not match the completed head")
    if not records or not matching_branch:
        if not remote_head_sha:
            return "publish-and-create"
        if remote_head_sha == expected_head_sha:
            return "create"
        raise ContractError("existing release branch does not match the completed head")
    if not remote_head_sha:
        return "publish-and-create"
    if remote_head_sha == expected_head_sha:
        return "create"
    raise ContractError("existing release branch does not match the completed head")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-pull-requests", type=Path, required=True)
    parser.add_argument("--remote-head-sha", default="")
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--expected-title", required=True)
    parser.add_argument("--expected-body", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        pull_requests = json.loads(
            _read_bounded(
                arguments.open_pull_requests,
                limit=MAX_OPEN_PULL_REQUEST_STATE_BYTES,
                label="open Release PR state",
            ).decode("utf-8")
        )
        body = _read_bounded(
            arguments.expected_body,
            limit=MAX_RELEASE_PULL_REQUEST_BODY_BYTES,
            label="expected Release PR body",
        ).decode("utf-8")
        action = reconcile_completed_release(
            open_pull_requests=pull_requests,
            remote_head_sha=arguments.remote_head_sha,
            expected_head_sha=arguments.expected_head_sha,
            expected_base=arguments.expected_base,
            expected_branch=arguments.expected_branch,
            expected_title=arguments.expected_title,
            expected_body=body,
        )
    except (ContractError, UnicodeDecodeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
