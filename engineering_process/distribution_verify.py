from __future__ import annotations

import hashlib
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .artifact_attestation import (
    bounded_artifact_names,
    create_distribution_attestation,
)
from .contracts import ContractError, read_json, validate_release
from .environment import execute_command
from .git import portable_git_path, remaining_seconds, tracked_index_paths, run_git


MAX_TRACKED_FILES = 5_000
MAX_TRACKED_LIST_BYTES = 1_000_000
MAX_TRACKED_FILE_BYTES = 8_000_000
MAX_TRACKED_TOTAL_BYTES = 64_000_000
MAX_ARCHIVE_BYTES = 128_000_000
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_NAME_BYTES = 1_000_000
MAX_ARCHIVE_MEMBER_BYTES = 16_000_000
MAX_ARCHIVE_EXPANDED_BYTES = 64_000_000
MAX_ARCHIVE_COMPRESSION_RATIO = 200
SNAPSHOT_TIMEOUT_SECONDS = 30.0
FORBIDDEN_PARTS = {".agents", ".process", "__pycache__", "build", "dist"}
REQUIRED_SUFFIXES = {
    "PROCESS_IMPROVEMENT.md",
    "PRODUCTION_STANDARD.md",
    "engineering_process/requirements-release.txt",
    "release.json",
    "improvement-catalog.json",
    "schemas/adoption-migration.schema.json",
    "schemas/automation-policy.schema.json",
    "schemas/automation-proposal-policy.schema.json",
    "schemas/automation-proposal.schema.json",
    "schemas/change.schema.json",
    "schemas/evidence-receipt.schema.json",
    "schemas/improvement-catalog.schema.json",
    "schemas/improvement-disposition.schema.json",
    "schemas/improvement-reproduction.schema.json",
    "schemas/improvement-resolution.schema.json",
    "schemas/improvement-signal.schema.json",
    "schemas/release-change.schema.json",
    "schemas/release.schema.json",
    "schemas/remote-verification-evidence.schema.json",
    "schemas/remote-verification-request.schema.json",
    "schemas/supplemental-verification.schema.json",
}


def _tracked_entries(
    project_root: Path, *, checkpoint: str | None, deadline: float
) -> list[tuple[PurePosixPath, str, int, int]]:
    indexed_paths = tracked_index_paths(
        project_root,
        label="distribution snapshot tracked paths",
        timeout_seconds=remaining_seconds(
            deadline, label="distribution snapshot tracked paths"
        ),
        max_stdout_bytes=MAX_TRACKED_LIST_BYTES,
        max_paths=MAX_TRACKED_FILES,
    )
    checkpoint = checkpoint or _head_checkpoint(project_root)
    result = run_git(
        project_root,
        ["ls-tree", "-r", "-z", "--long", checkpoint, "--"],
        label="distribution snapshot HEAD tree",
        timeout_seconds=remaining_seconds(
            deadline, label="distribution snapshot HEAD tree"
        ),
        max_stdout_bytes=MAX_TRACKED_LIST_BYTES,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            "distribution snapshot HEAD tree failed"
            + (f": {detail}" if detail else "")
        )
    records = [record for record in result.stdout.split(b"\0") if record]
    if len(records) > MAX_TRACKED_FILES:
        raise ContractError(
            f"distribution snapshot exceeds {MAX_TRACKED_FILES} tracked files"
        )
    entries: list[tuple[PurePosixPath, str, int, int]] = []
    tree_paths: list[bytes] = []
    total = 0
    for record in records:
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 4:
            raise ContractError("distribution snapshot HEAD tree record is invalid")
        encoded_mode, object_type, encoded_oid, encoded_size = fields
        path = portable_git_path(
            encoded_path, label="distribution snapshot HEAD tree"
        )
        if encoded_mode not in {b"100644", b"100755"} or object_type != b"blob":
            raise ContractError(
                f"tracked distribution input must be a regular file: {path}"
            )
        try:
            oid = encoded_oid.decode("ascii")
            size = int(encoded_size.decode("ascii"))
            mode = int(encoded_mode, 8)
        except (UnicodeDecodeError, ValueError) as error:
            raise ContractError(
                f"distribution snapshot HEAD tree metadata is invalid: {path}"
            ) from error
        if len(oid) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise ContractError(
                f"distribution snapshot HEAD tree object id is invalid: {path}"
            )
        if size < 0 or size > MAX_TRACKED_FILE_BYTES:
            raise ContractError(
                f"tracked distribution file exceeds {MAX_TRACKED_FILE_BYTES} bytes: {path}"
            )
        total += size
        if total > MAX_TRACKED_TOTAL_BYTES:
            raise ContractError(
                f"tracked distribution snapshot exceeds {MAX_TRACKED_TOTAL_BYTES} bytes"
            )
        tree_paths.append(encoded_path)
        entries.append((PurePosixPath(path), oid, size, mode))
    if sorted(indexed_paths) != sorted(tree_paths):
        raise ContractError(
            "distribution snapshot index does not match the verified HEAD tree"
        )
    return entries


def _head_checkpoint(project_root: Path) -> str:
    result = run_git(
        project_root,
        ["rev-parse", "--verify", "HEAD^{commit}"],
        label="distribution snapshot checkpoint",
        timeout_seconds=30,
        max_stdout_bytes=128,
    )
    if result.returncode != 0:
        raise ContractError("distribution snapshot requires a Git HEAD checkpoint")
    try:
        checkpoint = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ContractError("distribution snapshot checkpoint must be ASCII") from error
    if len(checkpoint) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in checkpoint
    ):
        raise ContractError("distribution snapshot checkpoint is not a full object id")
    return checkpoint


def _tracked_paths(project_root: Path) -> list[PurePosixPath]:
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS
    return [
        path
        for path, _oid, _size, _mode in _tracked_entries(
            project_root, checkpoint=None, deadline=deadline
        )
    ]


def _copy_tracked_snapshot(
    project_root: Path, destination: Path, *, checkpoint: str | None = None
) -> list[PurePosixPath]:
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS
    checkpoint = checkpoint or _head_checkpoint(project_root)
    entries = _tracked_entries(
        project_root, checkpoint=checkpoint, deadline=deadline
    )
    requests = b"".join(
        oid.encode("ascii") + b"\n"
        for _path, oid, _size, _mode in entries
    )
    batch = run_git(
        project_root,
        ["cat-file", "--batch"],
        label="distribution snapshot HEAD blobs",
        timeout_seconds=remaining_seconds(
            deadline, label="distribution snapshot HEAD blobs"
        ),
        max_stdout_bytes=MAX_TRACKED_TOTAL_BYTES + MAX_TRACKED_LIST_BYTES,
        input_bytes=requests,
    )
    if batch.returncode != 0:
        detail = batch.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            "cannot materialize tracked HEAD blobs"
            + (f": {detail}" if detail else "")
        )

    cursor = 0
    for relative, oid, size, mode in entries:
        header_end = batch.stdout.find(b"\n", cursor)
        expected_header = f"{oid} blob {size}".encode("ascii")
        if header_end < 0 or batch.stdout[cursor:header_end] != expected_header:
            raise ContractError(
                f"tracked HEAD blob protocol metadata is invalid: {relative}"
            )
        content_start = header_end + 1
        content_end = content_start + size
        if (
            content_end >= len(batch.stdout)
            or batch.stdout[content_end : content_end + 1] != b"\n"
        ):
            raise ContractError(
                f"tracked HEAD blob protocol size is invalid: {relative}"
            )
        content = batch.stdout[content_start:content_end]
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        digest = hashlib.new(algorithm, usedforsecurity=False)
        digest.update(f"blob {size}\0".encode("ascii"))
        digest.update(content)
        if digest.hexdigest() != oid:
            raise ContractError(
                f"tracked HEAD blob differs from its object id: {relative}"
            )
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output_stream:
                output_stream.write(content)
            target.chmod(stat.S_IMODE(mode))
        except OSError as error:
            raise ContractError(
                f"cannot materialize tracked HEAD blob {relative}: {error}"
            ) from error
        cursor = content_end + 1
    if cursor != len(batch.stdout):
        raise ContractError("tracked HEAD blob protocol returned unexpected output")
    return [path for path, _oid, _size, _mode in entries]


def _forbidden_archive_path(name: str, *, allow_sdist_metadata: bool) -> bool:
    canonical = name[:-1] if name.endswith("/") else name
    try:
        portable_git_path(
            canonical.encode("utf-8"), label="distribution archive member"
        )
    except (ContractError, UnicodeEncodeError):
        return True
    candidate = PurePosixPath(canonical)
    parts = candidate.parts
    egg_info_indexes = [
        index for index, part in enumerate(parts) if part.endswith(".egg-info")
    ]
    allowed_egg_info = (
        allow_sdist_metadata
        and egg_info_indexes == [1]
        and len(parts) >= 2
    )
    return (
        not name
        or not canonical
        or "\\" in name
        or ":" in canonical
        or "//" in canonical
        or any(ord(character) < 32 for character in canonical)
        or candidate.is_absolute()
        or ".." in parts
        or candidate.as_posix() != canonical
        or any(part in FORBIDDEN_PARTS for part in parts)
        or (bool(egg_info_indexes) and not allowed_egg_info)
        or name.endswith((".pyc", ".pyo"))
    )


def _validate_archive_members(path: Path, names: list[str]) -> None:
    if len(names) > MAX_ARCHIVE_MEMBERS:
        raise ContractError(f"{path.name}: exceeds {MAX_ARCHIVE_MEMBERS} members")
    if sum(len(name.encode("utf-8")) for name in names) > MAX_ARCHIVE_NAME_BYTES:
        raise ContractError(
            f"{path.name}: member names exceed {MAX_ARCHIVE_NAME_BYTES} bytes"
        )
    canonical_names = [name[:-1] if name.endswith("/") else name for name in names]
    if len(set(canonical_names)) != len(canonical_names):
        raise ContractError(f"{path.name}: duplicate member names are not allowed")
    is_sdist = path.name.endswith(".tar.gz")
    invalid = sorted(
        name
        for name in names
        if _forbidden_archive_path(name, allow_sdist_metadata=is_sdist)
    )
    if invalid:
        raise ContractError(
            f"{path.name}: contains forbidden generated or managed state: {invalid[0]}"
        )
    normalized = {
        "/".join(PurePosixPath(name).parts[1:])
        if is_sdist
        else name
        for name in names
        if name and not name.endswith("/")
    }
    missing = sorted(
        required
        for required in REQUIRED_SUFFIXES
        if not any(name == required or name.endswith(f"/{required}") for name in normalized)
    )
    if missing:
        raise ContractError(
            f"{path.name}: missing required distribution assets: {', '.join(missing)}"
        )
    if is_sdist:
        roots = {PurePosixPath(name).parts[0] for name in names if name}
        expected_root = path.name.removesuffix(".tar.gz")
        if roots != {expected_root}:
            raise ContractError(
                f"{path.name}: source distribution root must be {expected_root}"
            )


def _validate_expanded_size(
    path: Path, *, name: str, size: int, compressed_size: int | None, total: int
) -> int:
    if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ContractError(
            f"{path.name}: member {name!r} exceeds {MAX_ARCHIVE_MEMBER_BYTES} expanded bytes"
        )
    total += size
    if total > MAX_ARCHIVE_EXPANDED_BYTES:
        raise ContractError(
            f"{path.name}: expanded content exceeds {MAX_ARCHIVE_EXPANDED_BYTES} bytes"
        )
    if size > 1_000_000 and compressed_size is not None:
        if compressed_size <= 0 or size > compressed_size * MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ContractError(
                f"{path.name}: member {name!r} exceeds the compression-ratio limit"
            )
    return total


def _validate_zip_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            _validate_archive_members(path, [member.filename for member in members])
            total = 0
            for member in members:
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if member.is_dir():
                    if member.file_size != 0:
                        raise ContractError(
                            f"{path.name}: directory member has content: {member.filename!r}"
                        )
                    continue
                if file_type not in {0, stat.S_IFREG}:
                    raise ContractError(
                        f"{path.name}: non-regular member is not allowed: {member.filename!r}"
                    )
                if member.flag_bits & 0x1:
                    raise ContractError(
                        f"{path.name}: encrypted members are not allowed: {member.filename!r}"
                    )
                total = _validate_expanded_size(
                    path,
                    name=member.filename,
                    size=member.file_size,
                    compressed_size=member.compress_size,
                    total=total,
                )
            if total > path.stat().st_size * MAX_ARCHIVE_COMPRESSION_RATIO:
                raise ContractError(
                    f"{path.name}: archive exceeds the aggregate compression-ratio limit"
                )
    except (OSError, zipfile.BadZipFile) as error:
        raise ContractError(f"{path}: invalid wheel: {error}") from error


def _validate_tar_archive(path: Path) -> None:
    names: list[str] = []
    total = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                names.append(member.name)
                if len(names) > MAX_ARCHIVE_MEMBERS:
                    raise ContractError(
                        f"{path.name}: exceeds {MAX_ARCHIVE_MEMBERS} members"
                    )
                if not (member.isdir() or member.isreg()):
                    raise ContractError(
                        f"{path.name}: non-regular member is not allowed: {member.name!r}"
                    )
                if member.isdir():
                    if member.size != 0:
                        raise ContractError(
                            f"{path.name}: directory member has content: {member.name!r}"
                        )
                    continue
                total = _validate_expanded_size(
                    path,
                    name=member.name,
                    size=member.size,
                    compressed_size=None,
                    total=total,
                )
        _validate_archive_members(path, names)
        if total > path.stat().st_size * MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ContractError(
                f"{path.name}: archive exceeds the aggregate compression-ratio limit"
            )
    except (OSError, tarfile.TarError) as error:
        raise ContractError(f"{path}: invalid source distribution: {error}") from error


def _validate_archives(artifact_root: Path, expected_names: tuple[str, ...]) -> None:
    bounded_artifact_names(
        artifact_root,
        expected_names,
        label="distribution artifact output",
    )
    for path in (artifact_root / name for name in expected_names):
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ContractError(f"{path.name}: exceeds {MAX_ARCHIVE_BYTES} bytes")
        if path.suffix == ".whl":
            _validate_zip_archive(path)
        elif path.name.endswith(".tar.gz"):
            _validate_tar_archive(path)
        else:
            raise ContractError(f"unexpected distribution artifact type: {path.name}")


def _checkout_generated_state(project_root: Path) -> list[str]:
    candidates = [project_root / "build", project_root / "dist"]
    candidates.extend(sorted(project_root.glob("*.egg-info")))
    return sorted(path.relative_to(project_root).as_posix() for path in candidates if path.exists())


def verify_distribution(
    project_root: Path,
    *,
    output_root: Path | None = None,
    receipt_path: Path | None = None,
    authorization_path: Path | None = None,
    attestation_path: Path | None = None,
) -> dict[str, object]:
    project_root = project_root.resolve(strict=True)
    resolved_output: Path | None = None
    if output_root is not None:
        resolved_output = output_root.resolve()
        try:
            resolved_output.relative_to(project_root)
        except ValueError:
            pass
        else:
            raise ContractError("verified distribution output must stay outside the checkout")
        if resolved_output.exists():
            raise ContractError(f"{resolved_output}: refusing to replace existing output")
    if attestation_path is not None and resolved_output is None:
        raise ContractError("artifact attestation requires preserved distribution output")
    checkpoint = _head_checkpoint(project_root)
    generated_before = _checkout_generated_state(project_root)
    if generated_before:
        raise ContractError(
            "source checkout contains generated build state: "
            + ", ".join(generated_before)
        )
    checkout = run_git(
        project_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        label="inspect distribution checkout state",
        timeout_seconds=30,
        max_stdout_bytes=500_000,
    )
    if checkout.returncode != 0 or checkout.stdout:
        raise ContractError("distribution verification requires a clean checkout")
    attestation = None
    try:
        with tempfile.TemporaryDirectory(prefix="engineering-process-build-") as directory:
            temporary = Path(directory)
            snapshot = temporary / "source"
            artifacts = temporary / "artifacts"
            snapshot.mkdir()
            artifacts.mkdir()
            _copy_tracked_snapshot(
                project_root, snapshot, checkpoint=checkpoint
            )
            release = validate_release(
                read_json(snapshot / "release.json"), str(snapshot / "release.json")
            )
            if not release.artifacts:
                raise ContractError(
                    "an identity-bearing release contract is required for "
                    "distribution verification"
                )
            result = execute_command(
                project_root,
                identifier="isolated-distribution-build",
                run=(
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--outdir",
                    str(artifacts),
                    str(snapshot),
                ),
                timeout_seconds=900,
                working_directory=".",
                environment_overrides={"PYTHONDONTWRITEBYTECODE": None},
                stream_output=True,
            )
            if result["status"] != "passed":
                raise ContractError(
                    "isolated distribution build failed: "
                    f"{result['status']} (exit {result['exitCode']})"
                )
            _validate_archives(artifacts, release.artifacts)
            if resolved_output is not None:
                try:
                    resolved_output.mkdir(parents=True)
                    for name in release.artifacts:
                        shutil.copy2(artifacts / name, resolved_output / name)
                except OSError as error:
                    if resolved_output.exists():
                        shutil.rmtree(resolved_output, ignore_errors=True)
                    raise ContractError(
                        f"cannot preserve verified distributions: {error}"
                    ) from error
            if attestation_path is not None:
                assert resolved_output is not None
                attestation = create_distribution_attestation(
                    project_root,
                    resolved_output,
                    attestation_path,
                    receipt_path=receipt_path,
                    authorization_path=authorization_path,
                    checkpoint=checkpoint,
                )
    finally:
        generated_after = _checkout_generated_state(project_root)
        if generated_after != generated_before:
            raise ContractError(
                "distribution verification changed checkout build state: "
                f"before {generated_before}, after {generated_after}"
            )
    if _head_checkpoint(project_root) != checkpoint:
        raise ContractError("distribution snapshot checkpoint changed during verification")
    return {
        "artifacts": list(release.artifacts),
        "attestation": attestation,
        "checkoutGeneratedState": [],
        "output": str(resolved_output) if resolved_output is not None else None,
    }
