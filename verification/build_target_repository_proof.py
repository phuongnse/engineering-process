from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (
    ContractError,
    canonical_json_digest,
    read_json,
)
from engineering_process.transition import validate_target_repository_proof


def _target(identity: dict[str, Any]) -> dict[str, Any]:
    kind = identity.get("kind")
    if kind == "engineering-process-authority-transition-request":
        target = identity.get("target")
    elif kind == "engineering-process-bootstrap-adoption-intent":
        release = identity.get("targetRelease")
        target = (
            {**release, "repository": identity.get("repository")}
            if isinstance(release, dict)
            else None
        )
    else:
        target = None
    if not isinstance(target, dict):
        raise ContractError("repository proof identity has no transition target")
    return target


def build(
    identity: dict[str, Any],
    repository: dict[str, Any],
    release: dict[str, Any],
    tag_ref: dict[str, Any],
    tag_object: dict[str, Any] | None,
) -> dict[str, Any]:
    target = _target(identity)
    if (
        repository.get("full_name") != target["repository"]
        or release.get("tag_name") != target["tag"]
        or release.get("immutable") is not True
        or tag_ref.get("ref") != f"refs/tags/{target['tag']}"
    ):
        raise ContractError("GitHub repository, immutable release, or tag identity mismatch")
    ref_object = tag_ref.get("object")
    if not isinstance(ref_object, dict):
        raise ContractError("GitHub tag ref object is missing")
    if ref_object.get("type") == "commit":
        commit = ref_object.get("sha")
        if tag_object is not None:
            raise ContractError("lightweight Git tag must not supply an annotated tag object")
    elif ref_object.get("type") == "tag":
        nested = tag_object.get("object") if isinstance(tag_object, dict) else None
        if (
            not isinstance(tag_object, dict)
            or tag_object.get("sha") != ref_object.get("sha")
            or tag_object.get("tag") != target["tag"]
            or not isinstance(nested, dict)
            or nested.get("type") != "commit"
        ):
            raise ContractError("annotated Git tag service identity is invalid")
        commit = nested.get("sha")
    else:
        raise ContractError("GitHub tag ref must resolve to a commit or annotated tag")
    if commit != target["commit"]:
        raise ContractError("GitHub tag does not resolve to the target commit")
    assets_value = release.get("assets")
    if not isinstance(assets_value, list) or len(assets_value) > 16:
        raise ContractError("GitHub release asset set is invalid or oversized")
    release_assets: list[dict[str, Any]] = []
    for index, item in enumerate(assets_value):
        if not isinstance(item, dict):
            raise ContractError(
                f"GitHub release asset {index} has an invalid contract"
            )
        try:
            if not isinstance(item["name"], str):
                raise ContractError(
                    f"GitHub release asset {index} name is invalid"
                )
            release_assets.append(
                {
                    "artifactId": str(item["id"]),
                    "name": item["name"],
                    "url": item["url"],
                    "sizeBytes": item["size"],
                    "sha256": item["digest"],
                }
            )
        except KeyError as error:
            raise ContractError(
                f"GitHub release asset {index} is missing field: {error.args[0]}"
            ) from error
    release_assets.sort(key=lambda item: item["name"])
    assets_by_name = {item["name"]: item for item in release_assets}
    if len(assets_by_name) != len(release_assets):
        raise ContractError("GitHub release asset names must be unique")
    target_artifacts = target.get("artifacts")
    if not isinstance(target_artifacts, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        for item in target_artifacts
    ):
        raise ContractError("transition target artifact contract is invalid")
    try:
        assets = [assets_by_name[item["name"]] for item in target_artifacts]
    except KeyError as error:
        raise ContractError(
            f"GitHub release is missing registered target artifact: {error.args[0]}"
        ) from error
    proof = {
        "schemaVersion": 1,
        "kind": "engineering-process-target-repository-proof",
        "provider": "github",
        "repository": repository["full_name"],
        "repositoryId": str(repository["id"]),
        "repositoryUrl": repository["url"],
        "releaseId": str(release["id"]),
        "releaseUrl": release["url"],
        "tag": target["tag"],
        "commit": commit,
        "immutable": True,
        "assets": assets,
    }
    return validate_target_repository_proof(proof, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic GitHub service proof for a transition target"
    )
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--tag-ref", type=Path, required=True)
    parser.add_argument("--tag-object", type=Path)
    parser.add_argument("--verify-bound-digest", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    identity = read_json(args.identity)
    proof = build(
        identity,
        read_json(args.repository),
        read_json(args.release),
        read_json(args.tag_ref),
        read_json(args.tag_object) if args.tag_object is not None else None,
    )
    digest = canonical_json_digest(proof)
    target = _target(identity)
    if args.verify_bound_digest and target.get("repositoryProofSha256") != digest:
        raise ContractError("transition target does not bind repository proof bytes")
    if os.path.lexists(args.output):
        raise ContractError(f"{args.output}: refusing to replace repository proof")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(proof, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"repositoryProofSha256": digest, "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, KeyError, OSError, TypeError, ValueError) as error:
        print(f"target repository proof failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
