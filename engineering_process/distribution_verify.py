from __future__ import annotations

import shutil
import stat
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .artifact_attestation import create_distribution_attestation
from .contracts import ContractError, read_json, validate_release
from .environment import execute_command
from .git import tracked_index_paths, run_git


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
    "PRODUCTION_STANDARD.md",
    "engineering_process/requirements-release.txt",
    "release.json",
    "schemas/change.schema.json",
    "schemas/evidence-receipt.schema.json",
    "schemas/release.schema.json",
}


def _tracked_paths(project_root: Path) -> list[PurePosixPath]:
    encoded_paths = tracked_index_paths(
        project_root,
        label="distribution snapshot tracked paths",
        timeout_seconds=30,
        max_stdout_bytes=MAX_TRACKED_LIST_BYTES,
        max_paths=MAX_TRACKED_FILES,
    )
    return [PurePosixPath(encoded.decode("utf-8")) for encoded in encoded_paths]


def _copy_tracked_snapshot(
    project_root: Path, destination: Path
) -> list[PurePosixPath]:
    total = 0
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS
    resolved_root = project_root.resolve(strict=True)
    paths = _tracked_paths(project_root)
    for relative in paths:
        if time.monotonic() >= deadline:
            raise ContractError("tracked distribution snapshot exceeded 30 seconds")
        source = project_root.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        try:
            source_stat = source.lstat()
            source.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise ContractError(f"cannot inspect tracked file {relative}: {error}") from error
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise ContractError(
                f"tracked distribution input must be a regular file: {relative}"
            )
        if source_stat.st_size > MAX_TRACKED_FILE_BYTES:
            raise ContractError(
                f"tracked distribution file exceeds {MAX_TRACKED_FILE_BYTES} bytes: {relative}"
            )
        total += source_stat.st_size
        if total > MAX_TRACKED_TOTAL_BYTES:
            raise ContractError(
                f"tracked distribution snapshot exceeds {MAX_TRACKED_TOTAL_BYTES} bytes"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        try:
            with source.open("rb") as input_stream, target.open("xb") as output_stream:
                while chunk := input_stream.read(1024 * 1024):
                    if time.monotonic() >= deadline:
                        raise ContractError(
                            "tracked distribution snapshot exceeded 30 seconds"
                        )
                    copied += len(chunk)
                    if copied > source_stat.st_size:
                        raise ContractError(
                            f"tracked distribution file changed while copying: {relative}"
                        )
                    output_stream.write(chunk)
            after = source.lstat()
        except OSError as error:
            raise ContractError(f"cannot copy tracked file {relative}: {error}") from error
        if (
            copied != source_stat.st_size
            or after.st_size != source_stat.st_size
            or after.st_mtime_ns != source_stat.st_mtime_ns
            or after.st_mode != source_stat.st_mode
        ):
            raise ContractError(
                f"tracked distribution file changed while copying: {relative}"
            )
        target.chmod(stat.S_IMODE(source_stat.st_mode))
    return paths


def _forbidden_archive_path(name: str, *, allow_sdist_metadata: bool) -> bool:
    canonical = name[:-1] if name.endswith("/") else name
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
    actual = tuple(sorted(path.name for path in artifact_root.iterdir() if path.is_file()))
    if actual != expected_names:
        raise ContractError(
            "distribution artifacts do not match release identity: "
            f"expected {list(expected_names)}, got {list(actual)}"
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
    release = validate_release(
        read_json(project_root / "release.json"), str(project_root / "release.json")
    )
    if not release.artifacts:
        raise ContractError("release schemaVersion 2 identity is required for distribution verification")
    try:
        with tempfile.TemporaryDirectory(prefix="engineering-process-build-") as directory:
            temporary = Path(directory)
            snapshot = temporary / "source"
            artifacts = temporary / "artifacts"
            snapshot.mkdir()
            artifacts.mkdir()
            _copy_tracked_snapshot(project_root, snapshot)
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
    finally:
        generated_after = _checkout_generated_state(project_root)
        if generated_after != generated_before:
            raise ContractError(
                "distribution verification changed checkout build state: "
                f"before {generated_before}, after {generated_after}"
            )
    attestation = None
    if attestation_path is not None:
        assert resolved_output is not None
        attestation = create_distribution_attestation(
            project_root,
            resolved_output,
            attestation_path,
            receipt_path=receipt_path,
        )
    return {
        "artifacts": list(release.artifacts),
        "attestation": attestation,
        "checkoutGeneratedState": [],
        "output": str(resolved_output) if resolved_output is not None else None,
    }
