from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError


MAX_IDENTITY_DOCUMENT_BYTES = 65_536
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label} must be a regular non-symlink file")
        if before.st_size > MAX_IDENTITY_DOCUMENT_BYTES:
            raise ContractError(
                f"{label} exceeds {MAX_IDENTITY_DOCUMENT_BYTES} bytes"
            )
        content = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if (
        len(content) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"{label} changed while reading")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{label} must be a JSON object")
    return document


def validate_release_completion_identity(
    *,
    completion_summary: dict[str, Any],
    release_change: dict[str, Any],
    process_lock: dict[str, Any],
    expected_checkpoint: str,
) -> dict[str, str]:
    if SHA_PATTERN.fullmatch(expected_checkpoint) is None:
        raise ContractError("expected release checkpoint must be a full lowercase SHA")
    if completion_summary.get("status") != "passed":
        raise ContractError("completion evidence validation status must be passed")

    change_id = release_change.get("id")
    lifecycle_base = release_change.get("comparisonBase")
    projects = release_change.get("affectedProjects")
    if not isinstance(change_id, str) or not change_id:
        raise ContractError("release lifecycle change id must be non-empty")
    if not isinstance(lifecycle_base, str) or SHA_PATTERN.fullmatch(lifecycle_base) is None:
        raise ContractError(
            "release lifecycle comparison base must be a full lowercase SHA"
        )
    if (
        not isinstance(projects, list)
        or len(projects) != 1
        or not isinstance(projects[0], str)
        or not projects[0]
    ):
        raise ContractError("release lifecycle must identify exactly one project")

    process = process_lock.get("process")
    if not isinstance(process, dict):
        raise ContractError("process lock must contain the process identity")
    process_version = process.get("version")
    process_digest = process.get("digest")
    if not isinstance(process_version, str) or not process_version:
        raise ContractError("process lock version must be non-empty")
    if not isinstance(process_digest, str) or DIGEST_PATTERN.fullmatch(process_digest) is None:
        raise ContractError("process lock digest must be a SHA-256 identity")

    expected = {
        "changeId": change_id,
        "checkpoint": expected_checkpoint,
        "comparisonBase": lifecycle_base,
        "processVersion": process_version,
        "processDigest": process_digest,
        "project": projects[0],
    }
    for field, value in expected.items():
        if completion_summary.get(field) != value:
            raise ContractError(
                f"completion evidence {field} does not match its release owner"
            )
    return {
        "changeId": change_id,
        "checkpoint": expected_checkpoint,
        "lifecycleComparisonBase": lifecycle_base,
        "processDigest": process_digest,
        "processVersion": process_version,
        "project": projects[0],
        "status": "valid",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--release-change", type=Path, required=True)
    parser.add_argument("--process-lock", type=Path, required=True)
    parser.add_argument("--expected-checkpoint", required=True)
    arguments = parser.parse_args()
    try:
        result = validate_release_completion_identity(
            completion_summary=_read_object(
                arguments.summary, label="completion evidence summary"
            ),
            release_change=_read_object(
                arguments.release_change, label="release lifecycle contract"
            ),
            process_lock=_read_object(arguments.process_lock, label="process lock"),
            expected_checkpoint=arguments.expected_checkpoint,
        )
    except ContractError as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
