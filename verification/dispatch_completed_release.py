from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from engineering_process.evidence_transport import (
    MAX_ENCODED_COMPLETION_EVIDENCE_BYTES,
)


REPOSITORY = "phuongnse/engineering-process"
WORKFLOW = "release-approval.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
BASE64_PATTERN = re.compile(rb"^[A-Za-z0-9+/]+={0,2}$")


def publication_dispatch_request(
    *,
    verified_run_id: str,
    comparison_base: str,
    release_head_sha: str,
    encoded_evidence: bytes,
    token: str,
) -> Request:
    if RUN_ID_PATTERN.fullmatch(verified_run_id) is None:
        raise ContractError("verified run id must be a positive decimal integer")
    for value, label in (
        (comparison_base, "comparison base"),
        (release_head_sha, "release head SHA"),
    ):
        if SHA_PATTERN.fullmatch(value) is None:
            raise ContractError(f"{label} must be a full lowercase Git SHA")
    if (
        not encoded_evidence
        or len(encoded_evidence) > MAX_ENCODED_COMPLETION_EVIDENCE_BYTES
        or BASE64_PATTERN.fullmatch(encoded_evidence) is None
    ):
        raise ContractError("completion evidence transport is not bounded canonical base64")
    if not token or token != token.strip() or len(token) > 2_000:
        raise ContractError("GH_TOKEN must be a bounded non-empty token")
    body = json.dumps(
        {
            "ref": "main",
            "inputs": {
                "verified_run_id": verified_run_id,
                "comparison_base": comparison_base,
                "release_head_sha": release_head_sha,
                "completion_evidence_gzip_base64": encoded_evidence.decode("ascii"),
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Request(
        "https://api.github.com/"
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "engineering-process-release-host",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )


def dispatch_completed_release(
    *,
    verified_run_id: str,
    comparison_base: str,
    release_head_sha: str,
    completion_evidence: Path,
    token: str,
    opener: Callable[..., object] = urlopen,
) -> None:
    try:
        before = completion_evidence.lstat()
        encoded = completion_evidence.read_bytes()
        after = completion_evidence.lstat()
    except OSError as error:
        raise ContractError(f"cannot read completion evidence transport: {error}") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or completion_evidence.is_symlink()
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(encoded) != before.st_size
    ):
        raise ContractError("completion evidence transport must be one stable regular file")
    request = publication_dispatch_request(
        verified_run_id=verified_run_id,
        comparison_base=comparison_base,
        release_head_sha=release_head_sha,
        encoded_evidence=encoded,
        token=token,
    )
    try:
        with opener(request, timeout=30) as response:
            status = response.status
            detail = response.read(1_025)
    except HTTPError as error:
        detail = error.read(1_025)
        raise ContractError(
            f"GitHub workflow dispatch returned HTTP {error.code}: "
            + detail[:1_024].decode("utf-8", errors="replace")
        ) from error
    except (OSError, URLError) as error:
        raise ContractError(f"GitHub workflow dispatch failed: {error}") from error
    if status != 204 or detail:
        raise ContractError(
            f"GitHub workflow dispatch returned unexpected HTTP {status} response"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified-run-id", required=True)
    parser.add_argument("--comparison-base", required=True)
    parser.add_argument("--release-head-sha", required=True)
    parser.add_argument("--completion-evidence", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        dispatch_completed_release(
            verified_run_id=arguments.verified_run_id,
            comparison_base=arguments.comparison_base,
            release_head_sha=arguments.release_head_sha,
            completion_evidence=arguments.completion_evidence,
            token=os.environ.get("GH_TOKEN", ""),
        )
    except ContractError as error:
        parser.error(str(error))
    print("dispatched completed release publication")
