from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping
import urllib.request
from urllib.parse import urljoin, urlsplit
import zipfile

from .contracts import ContractError, ManagedTool, ManagedToolArtifact


DOWNLOAD_READ_TIMEOUT_SECONDS = 30
MARKER_NAME = ".engineering-process-tool.json"
USER_AGENT = "engineering-process/0.1.0"


def _validated_https_target(base_url: str, target_url: str) -> str:
    resolved = urljoin(base_url, target_url)
    try:
        parsed = urlsplit(resolved)
        parsed.port
    except ValueError as error:
        raise ContractError(f"invalid HTTPS redirect URL: {error}") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ContractError(f"tool download redirect is not a safe HTTPS URL: {resolved}")
    return resolved


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target = _validated_https_target(request.full_url, new_url)
        return super().redirect_request(
            request,
            response,
            code,
            message,
            headers,
            target,
        )


_HTTPS_OPENER = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())


def platform_identifier(
    *,
    system: str | None = None,
    machine: str | None = None,
    libc: str | None = None,
) -> str:
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()
    architectures = {
        "aarch64": "arm64",
        "amd64": "x64",
        "arm64": "arm64",
        "x86_64": "x64",
    }
    if machine_name not in architectures:
        raise ContractError(f"unsupported tool architecture: {machine_name}")
    architecture = architectures[machine_name]
    if system_name == "linux":
        libc_name = (libc if libc is not None else platform.libc_ver()[0]).lower()
        if libc_name in {"glibc", "gnu libc"}:
            libc_name = "glibc"
        elif "musl" in libc_name:
            libc_name = "musl"
        else:
            raise ContractError(
                f"unsupported or undetected Linux C library: {libc_name or 'unknown'}"
            )
        return f"linux-{libc_name}-{architecture}"
    if system_name == "darwin":
        return f"macos-{architecture}"
    if system_name == "windows":
        return f"windows-{architecture}"
    raise ContractError(f"unsupported tool operating system: {system_name}")


def managed_tools_root(
    *,
    environment: Mapping[str, str] | None = None,
    user_home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    home = Path.home() if user_home is None else user_home
    current = (platform_name or platform.system()).lower()
    if current == "windows":
        local_app_data = values.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else home / "AppData" / "Local"
        result = base / "EngineeringProcess" / "tools"
        if not result.is_absolute():
            raise ContractError("managed tool user-data directory must be absolute")
        return result
    if current == "darwin":
        result = home / "Library" / "Application Support" / "EngineeringProcess" / "tools"
        if not result.is_absolute():
            raise ContractError("managed tool user-data directory must be absolute")
        return result
    xdg_data_home = values.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else home / ".local" / "share"
    result = base / "engineering-process" / "tools"
    if not result.is_absolute():
        raise ContractError("managed tool user-data directory must be absolute")
    return result


def selected_artifact(
    tool: ManagedTool, *, current_platform: str | None = None
) -> ManagedToolArtifact:
    selected = current_platform or platform_identifier()
    artifact = tool.artifacts.get(selected)
    if artifact is None:
        available = ", ".join(sorted(tool.artifacts))
        raise ContractError(
            f"managed tool {tool.identifier} {tool.version} has no artifact for "
            f"{selected}; declared platforms: {available}"
        )
    return artifact


def _artifact_key(artifact: ManagedToolArtifact) -> str:
    return artifact.checksum.split(":", 1)[1][:24]


def install_root(
    tool: ManagedTool,
    artifact: ManagedToolArtifact,
    *,
    tools_root: Path | None = None,
) -> Path:
    root = managed_tools_root() if tools_root is None else tools_root
    return root / tool.identifier / tool.version / _artifact_key(artifact)


def managed_tool_preflight(
    tool: ManagedTool,
    *,
    current_platform: str | None = None,
    tools_root: Path | None = None,
) -> str | None:
    artifact = selected_artifact(tool, current_platform=current_platform)
    base = managed_tools_root() if tools_root is None else tools_root
    root = install_root(tool, artifact, tools_root=base)
    for directory in (base, base / tool.identifier, root.parent):
        if directory.is_symlink():
            return f"managed tool directory must not be a symlink: {directory}"
        if directory.exists() and not directory.is_dir():
            return f"managed tool directory is not a directory: {directory}"
    if root.is_symlink():
        return f"managed tool installation must not be a symlink: {root}"
    if root.exists() and not installed_commands(
        tool,
        current_platform=artifact.platform,
        tools_root=base,
    ):
        return f"managed tool installation is invalid or unmanaged: {root}"
    return None


def _marker_document(tool: ManagedTool, artifact: ManagedToolArtifact) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "tool": tool.identifier,
        "version": tool.version,
        "platform": artifact.platform,
        "checksum": artifact.checksum,
        "commands": artifact.commands,
    }


def _contained_command(root: Path, relative: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ContractError(f"managed command escapes its install root: {relative}") from error
    if not resolved.is_file():
        raise ContractError(f"managed command is not a file: {relative}")
    return candidate


def installed_commands(
    tool: ManagedTool,
    *,
    current_platform: str | None = None,
    tools_root: Path | None = None,
) -> dict[str, Path]:
    artifact = selected_artifact(tool, current_platform=current_platform)
    root = install_root(tool, artifact, tools_root=tools_root)
    marker_path = root / MARKER_NAME
    if not marker_path.is_file():
        return {}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if marker != _marker_document(tool, artifact):
        return {}
    try:
        return {
            name: _contained_command(root, relative)
            for name, relative in artifact.commands.items()
        }
    except ContractError:
        return {}


def managed_path_entries(
    tools: Iterable[ManagedTool],
    *,
    current_platform: str | None = None,
    tools_root: Path | None = None,
) -> tuple[Path, ...]:
    entries: list[Path] = []
    for tool in tools:
        commands = installed_commands(
            tool,
            current_platform=current_platform,
            tools_root=tools_root,
        )
        for command in commands.values():
            parent = command.parent
            if parent not in entries:
                entries.append(parent)
    return tuple(entries)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContractError("managed tool installation exceeded its timeout")
    return remaining


def download_artifact(
    artifact: ManagedToolArtifact,
    destination: Path,
    *,
    deadline: float,
) -> None:
    request = urllib.request.Request(artifact.url, headers={"User-Agent": USER_AGENT})
    with _HTTPS_OPENER.open(
        request,
        timeout=min(DOWNLOAD_READ_TIMEOUT_SECONDS, _remaining(deadline)),
    ) as response:
        _validated_https_target(artifact.url, response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise ContractError("tool response has an invalid Content-Length") from error
            if declared_size > artifact.max_download_bytes:
                raise ContractError(
                    f"tool artifact exceeds maxDownloadBytes: {declared_size} > "
                    f"{artifact.max_download_bytes}"
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with destination.open("xb") as output:
            while True:
                _remaining(deadline)
                block = response.read(min(1024 * 1024, artifact.max_download_bytes - total + 1))
                if not block:
                    break
                total += len(block)
                if total > artifact.max_download_bytes:
                    raise ContractError(
                        f"tool artifact exceeds maxDownloadBytes: {artifact.max_download_bytes}"
                    )
                output.write(block)


def verify_checksum(path: Path, checksum: str, *, deadline: float) -> None:
    algorithm, expected = checksum.split(":", 1)
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            _remaining(deadline)
            digest.update(block)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise ContractError(
            f"checksum mismatch for {path.name}: expected {expected}, got {actual}"
        )


def _safe_member_path(name: str, target: Path) -> Path:
    portable = PurePosixPath(name.replace("\\", "/"))
    if (
        portable.is_absolute()
        or ".." in portable.parts
        or any(":" in part for part in portable.parts)
    ):
        raise ContractError(f"unsafe archive member: {name}")
    candidate = (target / Path(*portable.parts)).resolve()
    resolved_target = target.resolve()
    if candidate != resolved_target and resolved_target not in candidate.parents:
        raise ContractError(f"unsafe archive member: {name}")
    return candidate


def _safe_tar_link(member: tarfile.TarInfo, target: Path) -> None:
    link = PurePosixPath(member.linkname.replace("\\", "/"))
    if link.is_absolute():
        raise ContractError(
            f"unsafe archive link: {member.name} -> {member.linkname}"
        )
    combined = link if member.islnk() else PurePosixPath(member.name).parent / link
    normalized = posixpath.normpath(str(combined))
    if normalized == ".." or normalized.startswith("../"):
        raise ContractError(
            f"unsafe archive link: {member.name} -> {member.linkname}"
        )
    _safe_member_path(normalized, target)


def _validate_archive_limits(
    *,
    count: int,
    size: int,
    artifact: ManagedToolArtifact,
) -> None:
    if count > artifact.max_files:
        raise ContractError(
            f"tool archive exceeds maxFiles: {count} > {artifact.max_files}"
        )
    if size > artifact.max_extracted_bytes:
        raise ContractError(
            "tool archive exceeds maxExtractedBytes: "
            f"{size} > {artifact.max_extracted_bytes}"
        )


def _copy_stream(
    source: BinaryIO,
    destination: Path,
    *,
    deadline: float,
    max_bytes: int,
    expected_bytes: int | None = None,
) -> int:
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        while True:
            _remaining(deadline)
            block = source.read(min(1024 * 1024, max_bytes - written + 1))
            if not block:
                break
            written += len(block)
            if written > max_bytes:
                raise ContractError(
                    f"extracted artifact exceeds its {max_bytes}-byte limit"
                )
            output.write(block)
    if expected_bytes is not None and written != expected_bytes:
        raise ContractError(
            f"archive member size mismatch: expected {expected_bytes}, got {written}"
        )
    return written


def _apply_mode(path: Path, mode: int) -> None:
    if os.name != "nt" and mode:
        path.chmod(mode & 0o777)


def _extract_zip(
    archive: Path,
    target: Path,
    artifact: ManagedToolArtifact,
    *,
    deadline: float,
) -> None:
    with zipfile.ZipFile(archive) as handle:
        members = handle.infolist()
        normalized_names = [
            member.filename.replace("\\", "/").casefold() for member in members
        ]
        if len(normalized_names) != len(set(normalized_names)):
            raise ContractError("tool archive contains duplicate member paths")
        _validate_archive_limits(
            count=len(members),
            size=sum(member.file_size for member in members),
            artifact=artifact,
        )
        for member in members:
            _remaining(deadline)
            destination = _safe_member_path(member.filename, target)
            member_mode = member.external_attr >> 16
            if stat.S_ISLNK(member_mode):
                raise ContractError(f"zip symlinks are unsupported: {member.filename}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                _apply_mode(destination, member_mode)
                continue
            with handle.open(member, "r") as source:
                _copy_stream(
                    source,
                    destination,
                    deadline=deadline,
                    max_bytes=member.file_size,
                    expected_bytes=member.file_size,
                )
            _apply_mode(destination, member_mode)


def _extract_tar(
    archive: Path,
    target: Path,
    artifact: ManagedToolArtifact,
    *,
    deadline: float,
) -> None:
    names: set[str] = set()
    count = 0
    extracted_bytes = 0
    directory_modes: list[tuple[Path, int]] = []
    pending_hard_links: list[tuple[Path, Path]] = []
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle:
            _remaining(deadline)
            normalized_name = member.name.replace("\\", "/").casefold()
            if normalized_name in names:
                raise ContractError("tool archive contains duplicate member paths")
            names.add(normalized_name)
            count += 1
            if member.isfile():
                extracted_bytes += member.size
            _validate_archive_limits(
                count=count,
                size=extracted_bytes,
                artifact=artifact,
            )
            destination = _safe_member_path(member.name, target)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                directory_modes.append((destination, member.mode))
            elif member.isfile():
                source = handle.extractfile(member)
                if source is None:
                    raise ContractError(f"cannot read archive member: {member.name}")
                with source:
                    _copy_stream(
                        source,
                        destination,
                        deadline=deadline,
                        max_bytes=member.size,
                        expected_bytes=member.size,
                    )
                _apply_mode(destination, member.mode)
            elif member.issym():
                _safe_tar_link(member, target)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, destination)
            elif member.islnk():
                _safe_tar_link(member, target)
                source = _safe_member_path(member.linkname, target)
                pending_hard_links.append((source, destination))
            else:
                raise ContractError(f"unsupported archive member type: {member.name}")
    for source, destination in pending_hard_links:
        _remaining(deadline)
        if not source.is_file():
            raise ContractError(f"archive hard-link target is missing: {source.name}")
        _safe_member_path(str(destination.relative_to(target)), target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, destination)
    for directory, mode in reversed(directory_modes):
        _remaining(deadline)
        _apply_mode(directory, mode)


def extract_artifact(
    archive: Path,
    target: Path,
    artifact: ManagedToolArtifact,
    *,
    deadline: float,
) -> None:
    target.mkdir(parents=True, exist_ok=False)
    if artifact.archive_format == "zip":
        try:
            _extract_zip(archive, target, artifact, deadline=deadline)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as error:
            raise ContractError(f"invalid zip artifact: {error}") from error
        return
    if artifact.archive_format == "tar.gz":
        try:
            _extract_tar(archive, target, artifact, deadline=deadline)
        except (tarfile.TarError, EOFError) as error:
            raise ContractError(f"invalid tar artifact: {error}") from error
        return
    raise ContractError(f"unsupported archive format: {artifact.archive_format}")


def _payload_root(payload: Path, strip_components: int) -> Path:
    if strip_components == 0:
        return payload
    children = [child for child in payload.iterdir()]
    if (
        len(children) != 1
        or children[0].is_symlink()
        or not children[0].is_dir()
    ):
        raise ContractError(
            "stripComponents 1 requires exactly one top-level directory"
        )
    return children[0]


Downloader = Callable[[ManagedToolArtifact, Path, float], None]


def _default_downloader(
    artifact: ManagedToolArtifact, destination: Path, deadline: float
) -> None:
    download_artifact(artifact, destination, deadline=deadline)


def install_managed_tool(
    tool: ManagedTool,
    *,
    timeout_seconds: int,
    current_platform: str | None = None,
    tools_root: Path | None = None,
    downloader: Downloader = _default_downloader,
) -> dict[str, Path]:
    artifact = selected_artifact(tool, current_platform=current_platform)
    root = install_root(tool, artifact, tools_root=tools_root)
    preflight_issue = managed_tool_preflight(
        tool,
        current_platform=artifact.platform,
        tools_root=tools_root,
    )
    if preflight_issue is not None:
        raise ContractError(preflight_issue)
    existing = installed_commands(
        tool,
        current_platform=artifact.platform,
        tools_root=tools_root,
    )
    if existing:
        return existing
    deadline = time.monotonic() + timeout_seconds
    parent = root.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as directory:
        temporary = Path(directory)
        archive = temporary / "artifact"
        downloader(artifact, archive, deadline)
        if not archive.is_file():
            raise ContractError("managed tool downloader did not create an artifact file")
        if archive.stat().st_size > artifact.max_download_bytes:
            raise ContractError(
                f"tool artifact exceeds maxDownloadBytes: {artifact.max_download_bytes}"
            )
        verify_checksum(archive, artifact.checksum, deadline=deadline)

        staged = temporary / "staged"
        if artifact.archive_format == "file":
            if archive.stat().st_size > artifact.max_extracted_bytes:
                raise ContractError(
                    "file artifact exceeds maxExtractedBytes: "
                    f"{archive.stat().st_size} > {artifact.max_extracted_bytes}"
                )
            staged.mkdir()
            relative = next(iter(artifact.commands.values()))
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("rb") as source:
                _copy_stream(
                    source,
                    target,
                    deadline=deadline,
                    max_bytes=artifact.max_extracted_bytes,
                    expected_bytes=archive.stat().st_size,
                )
        else:
            payload = temporary / "payload"
            extract_artifact(archive, payload, artifact, deadline=deadline)
            source = _payload_root(payload, artifact.strip_components)
            source.rename(staged)

        for relative in artifact.commands.values():
            command = _contained_command(staged, relative)
            if os.name != "nt":
                command.chmod(command.stat().st_mode | 0o111)
        marker = _marker_document(tool, artifact)
        (staged / MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _remaining(deadline)
        try:
            staged.rename(root)
        except FileExistsError:
            concurrent = installed_commands(
                tool,
                current_platform=artifact.platform,
                tools_root=tools_root,
            )
            if concurrent:
                return concurrent
            raise ContractError(
                f"concurrent managed tool installation produced an invalid target: {root}"
            )

    installed = installed_commands(
        tool,
        current_platform=artifact.platform,
        tools_root=tools_root,
    )
    if not installed:
        raise ContractError("managed tool installation did not pass ownership validation")
    return installed
