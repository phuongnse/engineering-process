from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError


MAX_METADATA_BYTES = 1_000_000
MAX_ACTION_ARTIFACTS = 100
MAX_RELEASE_ASSETS = 256
MAX_EVIDENCE_BYTES = 8_000_000
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _bounded_document(path: Path, *, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise ContractError(f"{label} must be a regular non-symlink file")
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_METADATA_BYTES + 1)
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if len(content) > MAX_METADATA_BYTES:
        raise ContractError(f"{label} exceeds {MAX_METADATA_BYTES} bytes")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} is not valid UTF-8 JSON") from error


def _bounded_positive_size(value: object, *, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_EVIDENCE_BYTES
    ):
        raise ContractError(
            f"{label} must be between 1 and {MAX_EVIDENCE_BYTES} bytes"
        )
    return value


def _actions_selection(document: object, *, expected_artifact: str) -> dict[str, object] | None:
    if not isinstance(document, dict):
        raise ContractError("Actions artifact metadata must be a JSON object")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ACTION_ARTIFACTS:
        raise ContractError(
            f"Actions artifact metadata must contain at most {MAX_ACTION_ARTIFACTS} artifacts"
        )
    candidates: list[tuple[str, int, int]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") != expected_artifact:
            continue
        expired = artifact.get("expired")
        if not isinstance(expired, bool):
            raise ContractError("matching Actions artifact has invalid expiration state")
        if expired:
            continue
        size = _bounded_positive_size(
            artifact.get("size_in_bytes"), label="Actions artifact"
        )
        created_at = artifact.get("created_at")
        workflow_run = artifact.get("workflow_run")
        run_id = workflow_run.get("id") if isinstance(workflow_run, dict) else None
        if (
            not isinstance(created_at, str)
            or not created_at
            or len(created_at) > 64
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id < 1
        ):
            raise ContractError("matching Actions artifact identity is invalid")
        candidates.append((created_at, run_id, size))
    if not candidates:
        return None
    created_at, run_id, size = max(candidates)
    return {
        "artifact": expected_artifact,
        "createdAt": created_at,
        "runId": run_id,
        "size": size,
        "source": "actions",
    }


def _published_release_selection(
    document: object, *, expected_tag: str, evidence_asset: str
) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ContractError("published release metadata must be a JSON object")
    if document.get("tagName") != expected_tag or document.get("name") != expected_tag:
        raise ContractError("published release tag and name must match the release contract")
    if document.get("isDraft") is not False:
        raise ContractError("published release recovery rejects draft releases")
    published_at = document.get("publishedAt")
    if not isinstance(published_at, str) or not published_at or len(published_at) > 64:
        raise ContractError("published release recovery requires a bounded publication time")
    if document.get("immutabilityVerified") is not True:
        raise ContractError("published release recovery requires verified immutability")
    assets = document.get("assets")
    if not isinstance(assets, list) or len(assets) > MAX_RELEASE_ASSETS:
        raise ContractError(
            f"published release metadata must contain at most {MAX_RELEASE_ASSETS} assets"
        )
    matches = [
        asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("name") == evidence_asset
    ]
    if len(matches) != 1:
        raise ContractError("published release must contain exactly one declared evidence asset")
    asset = matches[0]
    if asset.get("state") != "uploaded":
        raise ContractError("published release evidence asset is not completely uploaded")
    size = _bounded_positive_size(asset.get("size"), label="published evidence asset")
    digest = asset.get("digest")
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError("published release evidence asset digest is invalid")
    return {
        "asset": evidence_asset,
        "digest": digest,
        "publishedAt": published_at,
        "size": size,
        "source": "published-release",
        "tag": expected_tag,
    }


def select_release_evidence(
    *,
    actions_document: object,
    expected_actions_artifact: str,
    expected_tag: str,
    evidence_asset: str,
    release_document: object | None = None,
) -> dict[str, object]:
    for value, label in (
        (expected_actions_artifact, "expected Actions artifact"),
        (expected_tag, "expected release tag"),
        (evidence_asset, "expected evidence asset"),
    ):
        if not value or value != value.strip() or len(value) > 255 or "/" in value:
            raise ContractError(f"{label} is invalid")
    primary = _actions_selection(
        actions_document, expected_artifact=expected_actions_artifact
    )
    if primary is not None:
        return primary
    if release_document is None:
        return {"source": "published-release-required"}
    return _published_release_selection(
        release_document,
        expected_tag=expected_tag,
        evidence_asset=evidence_asset,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--expected-actions-artifact", required=True)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--evidence-asset", required=True)
    arguments = parser.parse_args()
    try:
        actions = _bounded_document(arguments.actions, label="Actions artifact metadata")
        release = (
            _bounded_document(arguments.release, label="published release metadata")
            if arguments.release is not None
            else None
        )
        selection = select_release_evidence(
            actions_document=actions,
            expected_actions_artifact=arguments.expected_actions_artifact,
            expected_tag=arguments.expected_tag,
            evidence_asset=arguments.evidence_asset,
            release_document=release,
        )
    except ContractError as error:
        print(f"release evidence selection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(selection, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
