from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from .contracts import ContractError, Release, read_json, validate_release
from .evidence import MAX_RECEIPT_BYTES, validate_receipt
from .git import run_git


ATTESTATION_KIND = "engineering-process-distribution-attestation"
MAX_ATTESTATION_BYTES = 256_000
MAX_ARTIFACT_BYTES = 128_000_000
MAX_ARTIFACT_TOTAL_BYTES = 256_000_000
ARTIFACT_HASH_TIMEOUT_SECONDS = 30.0
MAX_ARTIFACT_NAME_BYTES = 4_096
ARTIFACT_ENUMERATION_TIMEOUT_SECONDS = 5.0


def attestation_asset_name(release: Release) -> str:
    if release.package_name is None or release.tag is None:
        raise ContractError("artifact attestation requires release identity schemaVersion 2")
    return f"{release.package_name}-{release.tag}-artifacts.json"


def _sha256_bytes(path: Path, *, limit: int, label: str) -> tuple[int, str]:
    deadline = time.monotonic() + ARTIFACT_HASH_TIMEOUT_SECONDS
    try:
        before = path.lstat()
    except OSError as error:
        raise ContractError(f"{label}: cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ContractError(f"{label}: must be a regular non-symlink file: {path}")
    if before.st_size > limit:
        raise ContractError(f"{label}: exceeds {limit} bytes: {path}")
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ContractError(f"{label}: file changed while opening: {path}")
            while chunk := stream.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise ContractError(
                        f"{label}: hashing exceeded "
                        f"{ARTIFACT_HASH_TIMEOUT_SECONDS:g} seconds: {path}"
                    )
                count += len(chunk)
                if count > before.st_size:
                    raise ContractError(f"{label}: file changed while hashing: {path}")
                digest.update(chunk)
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"{label}: cannot hash {path}: {error}") from error
    if (
        count != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"{label}: file changed while hashing: {path}")
    return count, f"sha256:{digest.hexdigest()}"


def _bounded_bytes(path: Path, *, limit: int, label: str) -> bytes:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label}: must be a regular non-symlink file")
        if before.st_size > limit:
            raise ContractError(f"{label}: exceeds {limit} bytes")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"{label}: cannot read {path}: {error}") from error
    if len(data) > limit:
        raise ContractError(f"{label}: exceeds {limit} bytes")
    if (
        len(data) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"{label}: changed while reading")
    return data


def _checkpoint(project_root: Path) -> str:
    result = run_git(
        project_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        label="artifact attestation checkpoint",
        timeout_seconds=30,
        max_stdout_bytes=128,
    )
    if result.returncode != 0:
        raise ContractError("artifact attestation requires a Git HEAD checkpoint")
    try:
        checkpoint = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ContractError("artifact attestation checkpoint must be ASCII") from error
    if len(checkpoint) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in checkpoint
    ):
        raise ContractError("artifact attestation checkpoint is not a full object id")
    return checkpoint


def _receipt_identity(
    release: Release, receipt_path: Path | None, *, checkpoint: str
) -> dict[str, Any] | None:
    if release.provenance_mode == "governed":
        if receipt_path is None:
            raise ContractError("governed artifact attestation requires a lifecycle receipt")
        if receipt_path.name != release.receipt_asset:
            raise ContractError("artifact attestation receipt filename does not match release identity")
        receipt = validate_receipt(receipt_path)
        if (
            receipt["project"] != release.receipt_project
            or receipt["changeId"] != release.receipt_change_id
            or receipt["cycle"] != release.receipt_cycle
            or receipt["checkpoint"] != checkpoint
        ):
            raise ContractError("artifact attestation receipt does not match release checkpoint")
        _, receipt_digest = _sha256_bytes(
            receipt_path,
            limit=MAX_RECEIPT_BYTES,
            label="artifact attestation lifecycle receipt",
        )
        return {
            "asset": receipt_path.name,
            "sha256": receipt_digest,
            "processVersion": receipt["processVersion"],
            "processDigest": receipt["processDigest"],
            "project": receipt["project"],
            "changeId": receipt["changeId"],
            "cycle": receipt["cycle"],
            "checkpoint": receipt["checkpoint"],
        }
    if receipt_path is not None:
        raise ContractError("bootstrap or legacy artifact attestation must not claim a receipt")
    return None


def _artifact_entries(artifact_root: Path, release: Release) -> list[dict[str, Any]]:
    if not artifact_root.is_dir() or artifact_root.is_symlink():
        raise ContractError("artifact attestation input must be a non-symlink directory")
    expected = list(release.artifacts)
    actual: list[str] = []
    name_bytes = 0
    deadline = time.monotonic() + ARTIFACT_ENUMERATION_TIMEOUT_SECONDS
    try:
        with os.scandir(artifact_root) as iterator:
            for item in iterator:
                if time.monotonic() >= deadline:
                    raise ContractError(
                        "artifact attestation enumeration exceeded "
                        f"{ARTIFACT_ENUMERATION_TIMEOUT_SECONDS:g} seconds"
                    )
                if len(actual) >= len(expected):
                    raise ContractError(
                        "artifact attestation input exceeds the declared artifact count"
                    )
                try:
                    encoded_name = item.name.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ContractError(
                        "artifact attestation names must use UTF-8"
                    ) from error
                name_bytes += len(encoded_name)
                if name_bytes > MAX_ARTIFACT_NAME_BYTES:
                    raise ContractError(
                        "artifact attestation names exceed "
                        f"{MAX_ARTIFACT_NAME_BYTES} bytes"
                    )
                item_stat = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(
                    item_stat.st_mode
                ):
                    raise ContractError(
                        "artifact attestation inputs must be regular non-symlink files"
                    )
                actual.append(item.name)
    except OSError as error:
        raise ContractError(f"cannot enumerate distribution artifacts: {error}") from error
    actual.sort()
    if actual != expected:
        raise ContractError(
            "artifact attestation inputs do not match release identity: "
            f"expected {expected}, got {actual}"
        )
    entries: list[dict[str, Any]] = []
    total = 0
    for name in release.artifacts:
        size, digest = _sha256_bytes(
            artifact_root / name,
            limit=MAX_ARTIFACT_BYTES,
            label="distribution artifact",
        )
        total += size
        if total > MAX_ARTIFACT_TOTAL_BYTES:
            raise ContractError(
                f"distribution artifacts exceed {MAX_ARTIFACT_TOTAL_BYTES} bytes"
            )
        entries.append({"name": name, "sizeBytes": size, "sha256": digest})
    return entries


def _expected_attestation(
    project_root: Path,
    artifact_root: Path,
    receipt_path: Path | None,
    *,
    checkpoint: str,
) -> dict[str, Any]:
    release_path = project_root / "release.json"
    release = validate_release(read_json(release_path), str(release_path))
    if not release.artifacts or release.package_name is None or release.tag is None:
        raise ContractError("artifact attestation requires release identity schemaVersion 2")
    _, contract_digest = _sha256_bytes(
        release_path,
        limit=1_000_000,
        label="artifact attestation release contract",
    )
    return {
        "schemaVersion": 1,
        "kind": ATTESTATION_KIND,
        "checkpoint": checkpoint,
        "release": {
            "contractSha256": contract_digest,
            "package": release.package_name,
            "version": release.version,
            "tag": release.tag,
            "releaseName": release.release_name,
            "artifacts": list(release.artifacts),
        },
        "lifecycleReceipt": _receipt_identity(
            release, receipt_path, checkpoint=checkpoint
        ),
        "artifacts": _artifact_entries(artifact_root, release),
    }


def create_distribution_attestation(
    project_root: Path,
    artifact_root: Path,
    output_path: Path,
    *,
    receipt_path: Path | None,
    checkpoint: str | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    release = validate_release(
        read_json(project_root / "release.json"), str(project_root / "release.json")
    )
    if output_path.name != attestation_asset_name(release):
        raise ContractError(
            f"artifact attestation output must be named {attestation_asset_name(release)}"
        )
    checkpoint = checkpoint or _checkpoint(project_root)
    if len(checkpoint) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in checkpoint
    ):
        raise ContractError("artifact attestation checkpoint is not a full object id")
    document = _expected_attestation(
        project_root,
        artifact_root,
        receipt_path,
        checkpoint=checkpoint,
    )
    data = json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    if len(data) > MAX_ATTESTATION_BYTES:
        raise ContractError(
            f"artifact attestation exceeds {MAX_ATTESTATION_BYTES} bytes"
        )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("xb") as stream:
            stream.write(data)
    except OSError as error:
        raise ContractError(f"cannot preserve artifact attestation: {error}") from error
    return document


def validate_distribution_attestation(
    project_root: Path,
    artifact_root: Path,
    attestation_path: Path,
    *,
    receipt_path: Path | None,
    checkpoint: str,
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    if checkpoint != _checkpoint(project_root):
        raise ContractError("artifact attestation checkpoint does not match checkout HEAD")
    release = validate_release(
        read_json(project_root / "release.json"), str(project_root / "release.json")
    )
    if attestation_path.name != attestation_asset_name(release):
        raise ContractError("artifact attestation filename does not match release identity")
    data = _bounded_bytes(
        attestation_path,
        limit=MAX_ATTESTATION_BYTES,
        label="artifact attestation",
    )
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid artifact attestation: {error}") from error
    expected = _expected_attestation(
        project_root,
        artifact_root,
        receipt_path,
        checkpoint=checkpoint,
    )
    if document != expected:
        raise ContractError(
            "artifact attestation does not match release, receipt, checkpoint, and bytes"
        )
    return document
