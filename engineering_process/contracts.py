from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

import regex as bounded_regex


NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PROFILE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FINAL_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SKILL_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MUTATION_SCOPES = {
    "host-configuration",
    "network",
    "project-files",
    "user-files",
}
TOOL_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]*[0-9A-Za-z])?$")
PLATFORM_PATTERN = re.compile(
    r"^(?:linux-(?:glibc|musl)-(?:x64|arm64)|macos-(?:x64|arm64)|windows-(?:x64|arm64))$"
)
COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
CHECKSUM_PATTERN = re.compile(r"^(?:sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128})$")
BASE_REF_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
MAX_JSON_BYTES = 1_000_000
MAX_IMPACT_BASE_REFS = 16
MAX_IMPACT_COMPONENTS = 256
MAX_IMPACT_PATTERNS_PER_COMPONENT = 64
MAX_IMPACT_PATTERNS = 1024
MAX_PROJECT_PROFILES = 64
MAX_CHECKS_PER_PROFILE = 256
MAX_PROJECT_CHECKS = 1_024
MAX_CONTRACT_ITEMS = 256
PRODUCTION_STANDARD = "production-v1"
CORE_QUALITY_DIMENSIONS = (
    "compatibility",
    "correctness",
    "maintainability",
    "observability",
    "operability",
    "performance",
    "privacy",
    "reliability",
    "security",
    "supply-chain",
)


class ContractError(ValueError):
    """Raised when a process contract is invalid."""


@dataclass(frozen=True)
class ReleaseVersionPlan:
    previous_version: str
    version: str
    classification: str
    compatibility: str
    change_types: tuple[str, ...]


def derive_release_version(
    previous_version: str, change_types: Iterable[str]
) -> ReleaseVersionPlan:
    """Derive the only permitted next package version from public change types."""
    previous_match = FINAL_SEMVER_PATTERN.fullmatch(previous_version)
    if previous_match is None:
        raise ContractError("previous version must be final SemVer X.Y.Z")
    normalized = tuple(sorted(set(change_types)))
    if not normalized:
        raise ContractError(
            "release version planning requires at least one change type"
        )
    allowed = {"fix", "capability", "breaking"}
    unknown = sorted(set(normalized) - allowed)
    if unknown:
        raise ContractError(
            "unknown release change types: " + ", ".join(unknown)
        )

    previous = tuple(int(part) for part in previous_match.groups())
    if "breaking" in normalized:
        classification = "minor" if previous[0] == 0 else "major"
        compatibility = "incompatible"
    elif "capability" in normalized:
        classification = "minor"
        compatibility = "backward-compatible"
    else:
        classification = "patch"
        compatibility = "backward-compatible"

    version = {
        "patch": (previous[0], previous[1], previous[2] + 1),
        "minor": (previous[0], previous[1] + 1, 0),
        "major": (previous[0] + 1, 0, 0),
    }[classification]
    return ReleaseVersionPlan(
        previous_version=previous_version,
        version=".".join(str(part) for part in version),
        classification=classification,
        compatibility=compatibility,
        change_types=normalized,
    )


@dataclass(frozen=True)
class Check:
    identifier: str
    run: tuple[str, ...]
    timeout_seconds: int
    working_directory: str
    components: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ImpactComponent:
    identifier: str
    paths: tuple[str, ...]
    affects: tuple[str, ...]


@dataclass(frozen=True)
class ProjectImpact:
    base_refs: tuple[str, ...]
    unmatched_paths: str
    components: dict[str, ImpactComponent]


@dataclass(frozen=True)
class EnvironmentProbe:
    run: tuple[str, ...]
    timeout_seconds: int
    working_directory: str
    output_stream: str
    output_regex: str | None


@dataclass(frozen=True)
class EnvironmentRequirement:
    identifier: str
    description: str
    probe: EnvironmentProbe
    remediation: str
    setup_action: str | None


@dataclass(frozen=True)
class ManagedCommand:
    executable: str
    script: str | None


@dataclass(frozen=True)
class ManagedToolArtifact:
    platform: str
    url: str
    checksum: str
    archive_format: str
    strip_components: int
    max_download_bytes: int
    max_extracted_bytes: int
    max_files: int
    commands: dict[str, ManagedCommand]


@dataclass(frozen=True)
class ManagedTool:
    identifier: str
    version: str
    artifacts: dict[str, ManagedToolArtifact]


@dataclass(frozen=True)
class SetupAction:
    identifier: str
    kind: str
    run: tuple[str, ...]
    tool: str | None
    timeout_seconds: int
    working_directory: str
    mutations: tuple[str, ...]
    requires: tuple[str, ...]


@dataclass(frozen=True)
class ProjectEnvironment:
    default_profile: str
    foreground_only: bool
    profiles: dict[str, tuple[str, ...]]
    requirements: dict[str, EnvironmentRequirement]
    managed_tools: dict[str, ManagedTool]
    setup_actions: dict[str, SetupAction]


@dataclass(frozen=True)
class Project:
    identifier: str
    profiles: dict[str, tuple[Check, ...]]
    required_profiles: tuple[str, ...] = ()
    environment: ProjectEnvironment | None = None
    impact: ProjectImpact | None = None
    quality_extensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessLock:
    version: str
    digest: str
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Release:
    previous_version: str
    version: str
    classification: str
    compatibility: str
    schema_impact: str
    migration: str | None
    package_name: str | None = None
    distribution_name: str | None = None
    tag: str | None = None
    release_name: str | None = None
    runtime_version_file: str | None = None
    runtime_version_variable: str | None = None
    artifacts: tuple[str, ...] = ()
    receipt_asset: str | None = None
    authorization_asset: str | None = None
    receipt_change_id: str | None = None
    receipt_project: str | None = None
    receipt_cycle: int | None = None
    provenance_mode: str = "legacy"


@dataclass(frozen=True)
class ReleaseChange:
    identifier: str
    change_type: str
    surfaces: tuple[str, ...]
    rationale: str
    schema_impact: str
    migration: str | None


def read_json(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if len(data) > MAX_JSON_BYTES:
        raise ContractError(f"{path}: contract exceeds the 1 MB limit")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ContractError(f"{path}: UTF-8 BOM is not allowed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"{path}: must be UTF-8: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(
            f"{path}:{error.lineno}:{error.colno}: invalid JSON: {error.msg}"
        ) from error


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: must be an object")
    return value


def _exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    path: str,
) -> None:
    optional = optional or set()
    actual = set(value)
    missing = sorted(required - actual)
    extra = sorted(actual - required - optional)
    if missing:
        raise ContractError(f"{path}: missing properties: {', '.join(missing)}")
    if extra:
        raise ContractError(f"{path}: unknown properties: {', '.join(extra)}")


def _string(value: Any, path: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError(f"{path}: must be a non-empty trimmed string")
    if "\x00" in value:
        raise ContractError(f"{path}: must not contain NUL")
    if len(value) > max_length:
        raise ContractError(f"{path}: exceeds {max_length} characters")
    return value


def _string_list(
    value: Any,
    path: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"{path}: must contain at least {minimum} item(s)")
    if maximum is not None and len(value) > maximum:
        raise ContractError(f"{path}: exceeds {maximum} items")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _string(item, f"{path}[{index}]")
        if pattern is not None and pattern.fullmatch(text) is None:
            raise ContractError(f"{path}[{index}]: has an invalid format")
        result.append(text)
    if len(set(result)) != len(result):
        raise ContractError(f"{path}: duplicate items are not allowed")
    return result


def _schema_version(document: dict[str, Any], path: str) -> None:
    if document.get("schemaVersion") != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")


def _timeout(value: Any, path: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > 86_400
    ):
        raise ContractError(f"{path}: must be an integer from 1 to 86400")
    return value


def _working_directory(value: Any, path: str) -> str:
    working_directory = _string(value, path, max_length=512)
    work_path = Path(working_directory)
    if work_path.is_absolute() or ".." in work_path.parts:
        raise ContractError(f"{path}: must stay within the project")
    return working_directory


def _portable_glob(value: Any, path: str) -> str:
    pattern = _string(value, path, max_length=512)
    candidate = PurePosixPath(pattern)
    if (
        "\\" in pattern
        or ":" in pattern
        or any("**" in segment and segment != "**" for segment in pattern.split("/"))
        or any(ord(character) < 32 for character in pattern)
        or candidate.is_absolute()
        or ".." in candidate.parts
        or pattern in {".", ".."}
        or pattern.endswith("/")
        or candidate.as_posix() != pattern
    ):
        raise ContractError(
            f"{path}: must use canonical portable relative glob syntax"
        )
    return pattern


def _validate_impact(value: Any, path: str) -> ProjectImpact:
    impact = _object(value, path)
    _exact_keys(
        impact,
        required={"baseRefs", "components", "unmatchedPaths"},
        path=path,
    )
    base_refs = _string_list(impact["baseRefs"], f"{path}.baseRefs")
    if len(base_refs) > MAX_IMPACT_BASE_REFS:
        raise ContractError(
            f"{path}.baseRefs: exceeds {MAX_IMPACT_BASE_REFS} items"
        )
    for index, base_ref in enumerate(base_refs):
        if (
            BASE_REF_PATTERN.fullmatch(base_ref) is None
            or ".." in base_ref
            or "//" in base_ref
            or base_ref.endswith(("/", "."))
        ):
            raise ContractError(
                f"{path}.baseRefs[{index}]: must be a portable Git ref or object id"
            )

    unmatched_paths = _string(
        impact["unmatchedPaths"], f"{path}.unmatchedPaths", max_length=32
    )
    if unmatched_paths != "all-scoped-checks":
        raise ContractError(
            f"{path}.unmatchedPaths: must be all-scoped-checks"
        )

    raw_components = impact["components"]
    if not isinstance(raw_components, list) or not raw_components:
        raise ContractError(f"{path}.components: must contain at least 1 item(s)")
    if len(raw_components) > MAX_IMPACT_COMPONENTS:
        raise ContractError(
            f"{path}.components: exceeds {MAX_IMPACT_COMPONENTS} items"
        )

    components: dict[str, ImpactComponent] = {}
    total_patterns = 0
    for index, raw_component in enumerate(raw_components):
        component_path = f"{path}.components[{index}]"
        component = _object(raw_component, component_path)
        _exact_keys(
            component,
            required={"id", "paths", "affects"},
            path=component_path,
        )
        identifier = _string(
            component["id"], f"{component_path}.id", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{component_path}.id: invalid component name")
        if identifier in components:
            raise ContractError(
                f"{path}.components: duplicate component {identifier}"
            )

        raw_paths = component["paths"]
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ContractError(
                f"{component_path}.paths: must contain at least 1 item(s)"
            )
        if len(raw_paths) > MAX_IMPACT_PATTERNS_PER_COMPONENT:
            raise ContractError(
                f"{component_path}.paths: exceeds "
                f"{MAX_IMPACT_PATTERNS_PER_COMPONENT} items"
            )
        total_patterns += len(raw_paths)
        if total_patterns > MAX_IMPACT_PATTERNS:
            raise ContractError(
                f"{path}.components: exceeds {MAX_IMPACT_PATTERNS} total patterns"
            )
        patterns = [
            _portable_glob(item, f"{component_path}.paths[{path_index}]")
            for path_index, item in enumerate(raw_paths)
        ]
        if len(set(patterns)) != len(patterns):
            raise ContractError(
                f"{component_path}.paths: duplicate items are not allowed"
            )
        if patterns != sorted(patterns):
            raise ContractError(f"{component_path}.paths: patterns must be sorted")

        affects = _string_list(
            component["affects"],
            f"{component_path}.affects",
            minimum=0,
            pattern=PROFILE_PATTERN,
        )
        if affects != sorted(affects):
            raise ContractError(
                f"{component_path}.affects: components must be sorted"
            )
        components[identifier] = ImpactComponent(
            identifier=identifier,
            paths=tuple(patterns),
            affects=tuple(affects),
        )

    for component in components.values():
        unknown = sorted(set(component.affects) - set(components))
        if unknown:
            raise ContractError(
                f"{path}.components.{component.identifier}.affects: "
                f"undefined components: {', '.join(unknown)}"
            )
        if component.identifier in component.affects:
            raise ContractError(
                f"{path}.components.{component.identifier}.affects: "
                "cannot affect itself"
            )
    return ProjectImpact(
        base_refs=tuple(base_refs),
        unmatched_paths=unmatched_paths,
        components=components,
    )


def _bounded_integer(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ContractError(
            f"{path}: must be an integer from {minimum} to {maximum}"
        )
    return value


def _relative_tool_path(value: Any, path: str, *, strict_portable: bool) -> str:
    text = _string(value, path, max_length=512)
    if not strict_portable:
        legacy_candidate = Path(text)
        if (
            legacy_candidate.is_absolute()
            or ".." in legacy_candidate.parts
            or text in {".", ".."}
        ):
            raise ContractError(f"{path}: must be a contained relative file path")
        return legacy_candidate.as_posix()
    candidate = PurePosixPath(text)
    if (
        "\\" in text
        or ":" in text
        or candidate.is_absolute()
        or ".." in candidate.parts
        or text in {".", ".."}
        or text.endswith("/")
        or candidate.as_posix() != text
    ):
        raise ContractError(f"{path}: must be a contained relative file path")
    return text


def _https_url(value: Any, path: str) -> str:
    text = _string(value, path, max_length=2048)
    if "\\" in text or any(
        ord(character) < 0x21 or ord(character) > 0x7e for character in text
    ):
        raise ContractError(f"{path}: must contain only printable ASCII URI characters")
    try:
        parsed = urlsplit(text)
        parsed_port = parsed.port
    except ValueError as error:
        raise ContractError(f"{path}: invalid HTTPS URL: {error}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ContractError(
            f"{path}: must be an HTTPS URL without credentials or a fragment"
        )
    if parsed_port == 0:
        raise ContractError(f"{path}: HTTPS port must be from 1 to 65535")
    return text


def _validate_environment(
    document: Any,
    path: str,
    *,
    require_foreground_only: bool,
    require_native_windows_commands: bool,
    bounded_commands: bool,
) -> ProjectEnvironment:
    value = _object(document, path)
    required = {
        "defaultProfile",
        "managedTools",
        "profiles",
        "requirements",
        "setupActions",
    }
    if require_foreground_only:
        required.add("foregroundOnly")
    _exact_keys(
        value,
        required=required,
        path=path,
    )
    default_profile = _string(
        value["defaultProfile"], f"{path}.defaultProfile", max_length=64
    )
    if PROFILE_PATTERN.fullmatch(default_profile) is None:
        raise ContractError(f"{path}.defaultProfile: invalid profile name")
    if require_foreground_only and value["foregroundOnly"] is not True:
        raise ContractError(f"{path}.foregroundOnly: must attest true")

    raw_tools = value["managedTools"]
    if not isinstance(raw_tools, list):
        raise ContractError(f"{path}.managedTools: must be an array")
    if len(raw_tools) > 64:
        raise ContractError(f"{path}.managedTools: exceeds 64 items")
    managed_tools: dict[str, ManagedTool] = {}
    for tool_index, raw_tool in enumerate(raw_tools):
        tool_path = f"{path}.managedTools[{tool_index}]"
        tool = _object(raw_tool, tool_path)
        _exact_keys(
            tool,
            required={"id", "version", "artifacts"},
            path=tool_path,
        )
        identifier = _string(tool["id"], f"{tool_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{tool_path}.id: invalid tool name")
        if identifier in managed_tools:
            raise ContractError(f"{path}.managedTools: duplicate tool id {identifier}")
        version = _string(tool["version"], f"{tool_path}.version", max_length=128)
        if TOOL_VERSION_PATTERN.fullmatch(version) is None:
            raise ContractError(f"{tool_path}.version: invalid portable version")
        raw_artifacts = tool["artifacts"]
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            raise ContractError(f"{tool_path}.artifacts: must contain at least one item")
        if len(raw_artifacts) > 16:
            raise ContractError(f"{tool_path}.artifacts: exceeds 16 items")
        artifacts: dict[str, ManagedToolArtifact] = {}
        for artifact_index, raw_artifact in enumerate(raw_artifacts):
            artifact_path = f"{tool_path}.artifacts[{artifact_index}]"
            artifact = _object(raw_artifact, artifact_path)
            _exact_keys(
                artifact,
                required={
                    "archiveFormat",
                    "checksum",
                    "commands",
                    "maxDownloadBytes",
                    "maxExtractedBytes",
                    "maxFiles",
                    "platform",
                    "stripComponents",
                    "url",
                },
                path=artifact_path,
            )
            platform_name = _string(
                artifact["platform"], f"{artifact_path}.platform", max_length=32
            )
            if PLATFORM_PATTERN.fullmatch(platform_name) is None:
                raise ContractError(f"{artifact_path}.platform: unsupported platform")
            if platform_name in artifacts:
                raise ContractError(
                    f"{tool_path}.artifacts: duplicate platform {platform_name}"
                )
            archive_format = artifact["archiveFormat"]
            if archive_format not in {"file", "tar.gz", "zip"}:
                raise ContractError(
                    f"{artifact_path}.archiveFormat: unsupported format"
                )
            strip_components = _bounded_integer(
                artifact["stripComponents"],
                f"{artifact_path}.stripComponents",
                minimum=0,
                maximum=1,
            )
            raw_commands = _object(artifact["commands"], f"{artifact_path}.commands")
            if not raw_commands:
                raise ContractError(
                    f"{artifact_path}.commands: must define at least one command"
                )
            commands: dict[str, ManagedCommand] = {}
            for command_name, raw_command in raw_commands.items():
                if COMMAND_PATTERN.fullmatch(command_name) is None:
                    raise ContractError(
                        f"{artifact_path}.commands.{command_name}: invalid command name"
                    )
                command_path = f"{artifact_path}.commands.{command_name}"
                if isinstance(raw_command, str):
                    executable = _relative_tool_path(
                        raw_command,
                        command_path,
                        strict_portable=require_native_windows_commands,
                    )
                    script = None
                elif require_native_windows_commands:
                    command = _object(raw_command, command_path)
                    _exact_keys(
                        command,
                        required={"executable", "script"},
                        path=command_path,
                    )
                    executable = _relative_tool_path(
                        command["executable"],
                        f"{command_path}.executable",
                        strict_portable=True,
                    )
                    script = _relative_tool_path(
                        command["script"],
                        f"{command_path}.script",
                        strict_portable=True,
                    )
                    if executable == script:
                        raise ContractError(
                            f"{command_path}: executable and script must differ"
                        )
                else:
                    raise ContractError(
                        f"{command_path}: legacy manifests require a relative command path"
                    )
                basename = Path(executable).name
                if platform_name.startswith("windows-"):
                    if require_native_windows_commands:
                        if Path(executable).suffix.casefold() != ".exe":
                            raise ContractError(
                                f"{command_path}: executable must be a native .exe"
                            )
                        allowed_basenames = {
                            basename.casefold()
                            if script is not None
                            else f"{command_name}.exe".casefold()
                        }
                    else:
                        allowed_basenames = {
                            command_name.casefold(),
                            f"{command_name}.bat".casefold(),
                            f"{command_name}.cmd".casefold(),
                            f"{command_name}.exe".casefold(),
                        }
                else:
                    allowed_basenames = (
                        {basename.casefold()}
                        if script is not None
                        else {command_name.casefold()}
                    )
                if basename.casefold() not in allowed_basenames:
                    raise ContractError(
                        f"{artifact_path}.commands.{command_name}: executable basename "
                        + (
                            "must be the matching native .exe command"
                            if platform_name.startswith("windows-")
                            and require_native_windows_commands
                            else "must match the command name"
                        )
                    )
                commands[command_name] = ManagedCommand(
                    executable=executable,
                    script=script,
                )
            if list(commands) != sorted(commands):
                raise ContractError(f"{artifact_path}.commands: must be sorted")
            if archive_format == "file" and (
                strip_components != 0
                or len(commands) != 1
                or next(iter(commands.values())).script is not None
            ):
                raise ContractError(
                    f"{artifact_path}: file artifacts require stripComponents 0 and one direct command"
                )
            checksum = _string(
                artifact["checksum"], f"{artifact_path}.checksum", max_length=136
            )
            if CHECKSUM_PATTERN.fullmatch(checksum) is None:
                raise ContractError(f"{artifact_path}.checksum: invalid checksum")
            artifacts[platform_name] = ManagedToolArtifact(
                platform=platform_name,
                url=_https_url(artifact["url"], f"{artifact_path}.url"),
                checksum=checksum,
                archive_format=archive_format,
                strip_components=strip_components,
                max_download_bytes=_bounded_integer(
                    artifact["maxDownloadBytes"],
                    f"{artifact_path}.maxDownloadBytes",
                    minimum=1,
                    maximum=4_294_967_296,
                ),
                max_extracted_bytes=_bounded_integer(
                    artifact["maxExtractedBytes"],
                    f"{artifact_path}.maxExtractedBytes",
                    minimum=1,
                    maximum=8_589_934_592,
                ),
                max_files=_bounded_integer(
                    artifact["maxFiles"],
                    f"{artifact_path}.maxFiles",
                    minimum=1,
                    maximum=1_000_000,
                ),
                commands=commands,
            )
        if list(artifacts) != sorted(artifacts):
            raise ContractError(f"{tool_path}.artifacts: must be sorted by platform")
        managed_tools[identifier] = ManagedTool(
            identifier=identifier,
            version=version,
            artifacts=artifacts,
        )
    if list(managed_tools) != sorted(managed_tools):
        raise ContractError(f"{path}.managedTools: must be sorted by id")

    raw_actions = value["setupActions"]
    if not isinstance(raw_actions, list):
        raise ContractError(f"{path}.setupActions: must be an array")
    if len(raw_actions) > 128:
        raise ContractError(f"{path}.setupActions: exceeds 128 items")
    actions: dict[str, SetupAction] = {}
    for index, raw_action in enumerate(raw_actions):
        action_path = f"{path}.setupActions[{index}]"
        action = _object(raw_action, action_path)
        kind = action.get("kind")
        if kind == "command":
            _exact_keys(
                action,
                required={"id", "kind", "run", "timeoutSeconds", "mutations"},
                optional={"workingDirectory", "requires"},
                path=action_path,
            )
        elif kind == "managed-tool":
            _exact_keys(
                action,
                required={"id", "kind", "timeoutSeconds", "tool"},
                optional={"requires"},
                path=action_path,
            )
        else:
            raise ContractError(
                f"{action_path}.kind: must be command or managed-tool"
            )
        identifier = _string(action["id"], f"{action_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{action_path}.id: invalid action name")
        if identifier in actions:
            raise ContractError(f"{path}.setupActions: duplicate action id {identifier}")
        if kind == "command":
            mutations = _string_list(
                action["mutations"], f"{action_path}.mutations", minimum=1
            )
            invalid_mutations = sorted(set(mutations) - MUTATION_SCOPES)
            if invalid_mutations:
                raise ContractError(
                    f"{action_path}.mutations: unsupported scopes: "
                    + ", ".join(invalid_mutations)
                )
            run = tuple(
                _string_list(
                    action["run"],
                    f"{action_path}.run",
                    maximum=MAX_CONTRACT_ITEMS if bounded_commands else None,
                )
            )
            tool_identifier = None
            working_directory = _working_directory(
                action.get("workingDirectory", "."),
                f"{action_path}.workingDirectory",
            )
        else:
            mutations = ["network", "user-files"]
            run = ()
            tool_identifier = _string(
                action["tool"], f"{action_path}.tool", max_length=64
            )
            if tool_identifier not in managed_tools:
                raise ContractError(
                    f"{action_path}.tool: undefined managed tool {tool_identifier}"
                )
            working_directory = "."
        requires = _string_list(
            action.get("requires", []),
            f"{action_path}.requires",
            minimum=0,
            pattern=PROFILE_PATTERN,
        )
        if mutations != sorted(mutations):
            raise ContractError(f"{action_path}.mutations: must be sorted")
        if requires != sorted(requires):
            raise ContractError(f"{action_path}.requires: must be sorted")
        actions[identifier] = SetupAction(
            identifier=identifier,
            kind=kind,
            run=run,
            tool=tool_identifier,
            timeout_seconds=_timeout(
                action["timeoutSeconds"], f"{action_path}.timeoutSeconds"
            ),
            working_directory=working_directory,
            mutations=tuple(sorted(mutations)),
            requires=tuple(requires),
        )
    if list(actions) != sorted(actions):
        raise ContractError(f"{path}.setupActions: must be sorted by id")

    for identifier, action in actions.items():
        missing = sorted(set(action.requires) - set(actions))
        if missing:
            raise ContractError(
                f"{path}.setupActions.{identifier}.requires: undefined actions: "
                + ", ".join(missing)
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ContractError(
                f"{path}.setupActions: dependency cycle includes {identifier}"
            )
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in actions[identifier].requires:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in actions:
        visit(identifier)

    raw_requirements = value["requirements"]
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise ContractError(f"{path}.requirements: must contain at least one item")
    if len(raw_requirements) > 128:
        raise ContractError(f"{path}.requirements: exceeds 128 items")
    requirements: dict[str, EnvironmentRequirement] = {}
    for index, raw_requirement in enumerate(raw_requirements):
        requirement_path = f"{path}.requirements[{index}]"
        requirement = _object(raw_requirement, requirement_path)
        _exact_keys(
            requirement,
            required={"id", "description", "probe", "remediation"},
            optional={"setupAction"},
            path=requirement_path,
        )
        identifier = _string(
            requirement["id"], f"{requirement_path}.id", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{requirement_path}.id: invalid requirement name")
        if identifier in requirements:
            raise ContractError(
                f"{path}.requirements: duplicate requirement id {identifier}"
            )
        raw_probe = _object(requirement["probe"], f"{requirement_path}.probe")
        _exact_keys(
            raw_probe,
            required={"run", "timeoutSeconds", "readOnly"},
            optional={"workingDirectory", "outputRegex", "outputStream"},
            path=f"{requirement_path}.probe",
        )
        if raw_probe["readOnly"] is not True:
            raise ContractError(
                f"{requirement_path}.probe.readOnly: must attest true"
            )
        output_stream = raw_probe.get("outputStream", "combined")
        if output_stream not in {"combined", "stderr", "stdout"}:
            raise ContractError(
                f"{requirement_path}.probe.outputStream: unsupported stream"
            )
        output_regex = raw_probe.get("outputRegex")
        if output_regex is not None:
            output_regex = _string(
                output_regex,
                f"{requirement_path}.probe.outputRegex",
                max_length=1024,
            )
            try:
                bounded_regex.compile(output_regex)
            except bounded_regex.error as error:
                raise ContractError(
                    f"{requirement_path}.probe.outputRegex: invalid regex: {error}"
                ) from error
        setup_action = requirement.get("setupAction")
        if setup_action is not None:
            setup_action = _string(
                setup_action, f"{requirement_path}.setupAction", max_length=64
            )
            if setup_action not in actions:
                raise ContractError(
                    f"{requirement_path}.setupAction: undefined action {setup_action}"
                )
        requirements[identifier] = EnvironmentRequirement(
            identifier=identifier,
            description=_string(
                requirement["description"],
                f"{requirement_path}.description",
                max_length=512,
            ),
            probe=EnvironmentProbe(
                run=tuple(
                    _string_list(
                        raw_probe["run"],
                        f"{requirement_path}.probe.run",
                        maximum=MAX_CONTRACT_ITEMS if bounded_commands else None,
                    )
                ),
                timeout_seconds=_timeout(
                    raw_probe["timeoutSeconds"],
                    f"{requirement_path}.probe.timeoutSeconds",
                ),
                working_directory=_working_directory(
                    raw_probe.get("workingDirectory", "."),
                    f"{requirement_path}.probe.workingDirectory",
                ),
                output_stream=output_stream,
                output_regex=output_regex,
            ),
            remediation=_string(
                requirement["remediation"],
                f"{requirement_path}.remediation",
                max_length=1024,
            ),
            setup_action=setup_action,
        )
    if list(requirements) != sorted(requirements):
        raise ContractError(f"{path}.requirements: must be sorted by id")

    raw_profiles = _object(value["profiles"], f"{path}.profiles")
    if not raw_profiles:
        raise ContractError(f"{path}.profiles: must define at least one profile")
    profiles: dict[str, tuple[str, ...]] = {}
    for profile_name, raw_ids in raw_profiles.items():
        if PROFILE_PATTERN.fullmatch(profile_name) is None:
            raise ContractError(f"{path}.profiles.{profile_name}: invalid profile name")
        identifiers = _string_list(
            raw_ids,
            f"{path}.profiles.{profile_name}",
            pattern=PROFILE_PATTERN,
        )
        if identifiers != sorted(identifiers):
            raise ContractError(
                f"{path}.profiles.{profile_name}: requirements must be sorted"
            )
        missing = sorted(set(identifiers) - set(requirements))
        if missing:
            raise ContractError(
                f"{path}.profiles.{profile_name}: undefined requirements: "
                + ", ".join(missing)
            )
        profiles[profile_name] = tuple(identifiers)
    if default_profile not in profiles:
        raise ContractError(f"{path}.defaultProfile: profile is not defined")
    return ProjectEnvironment(
        default_profile=default_profile,
        foreground_only=require_foreground_only,
        profiles=profiles,
        requirements=requirements,
        managed_tools=managed_tools,
        setup_actions=actions,
    )


def validate_project(document: Any, path: str = "project") -> Project:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {1, 2, 3, 4}:
        raise ContractError(f"{path}.schemaVersion: must be 1, 2, 3, or 4")
    _exact_keys(
        value,
        required={"schemaVersion", "project", "lifecycle", "profiles"}
        | ({"environment"} if schema_version >= 2 else set()),
        optional={"$schema"} | ({"impact"} if schema_version >= 3 else set()),
        path=path,
    )
    identifier = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.project: must use lowercase project-id format")

    lifecycle = _object(value["lifecycle"], f"{path}.lifecycle")
    _exact_keys(
        lifecycle,
        required={"requiredProfiles"},
        optional={"qualityExtensions"} if schema_version >= 3 else set(),
        path=f"{path}.lifecycle",
    )
    required_profiles = _string_list(
        lifecycle["requiredProfiles"],
        f"{path}.lifecycle.requiredProfiles",
        pattern=PROFILE_PATTERN,
        maximum=MAX_PROJECT_PROFILES if schema_version == 4 else None,
    )
    if required_profiles != sorted(required_profiles):
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: must be sorted"
        )
    quality_extensions = (
        _string_list(
            lifecycle["qualityExtensions"],
            f"{path}.lifecycle.qualityExtensions",
            minimum=0,
            maximum=MAX_CONTRACT_ITEMS - len(CORE_QUALITY_DIMENSIONS),
            pattern=PROFILE_PATTERN,
        )
        if "qualityExtensions" in lifecycle
        else []
    )
    if quality_extensions != sorted(quality_extensions):
        raise ContractError(
            f"{path}.lifecycle.qualityExtensions: must be sorted"
        )
    invalid_quality_extensions = [
        dimension
        for dimension in quality_extensions
        if not dimension.startswith("project-")
    ]
    if invalid_quality_extensions:
        raise ContractError(
            f"{path}.lifecycle.qualityExtensions: extensions must use project-* names"
        )

    impact = (
        _validate_impact(value["impact"], f"{path}.impact")
        if "impact" in value
        else None
    )

    raw_profiles = _object(value["profiles"], f"{path}.profiles")
    if not raw_profiles:
        raise ContractError(f"{path}.profiles: must define at least one profile")
    if schema_version == 4 and len(raw_profiles) > MAX_PROJECT_PROFILES:
        raise ContractError(f"{path}.profiles: exceeds {MAX_PROJECT_PROFILES} profiles")
    profiles: dict[str, tuple[Check, ...]] = {}
    total_checks = 0
    for profile_name, raw_checks in raw_profiles.items():
        if PROFILE_PATTERN.fullmatch(profile_name) is None:
            raise ContractError(
                f"{path}.profiles.{profile_name}: invalid profile name"
            )
        if not isinstance(raw_checks, list) or not raw_checks:
            raise ContractError(
                f"{path}.profiles.{profile_name}: must contain at least one check"
            )
        if schema_version == 4 and len(raw_checks) > MAX_CHECKS_PER_PROFILE:
            raise ContractError(
                f"{path}.profiles.{profile_name}: exceeds {MAX_CHECKS_PER_PROFILE} checks"
            )
        total_checks += len(raw_checks)
        if schema_version == 4 and total_checks > MAX_PROJECT_CHECKS:
            raise ContractError(
                f"{path}.profiles: exceeds {MAX_PROJECT_CHECKS} total checks"
            )
        checks: list[Check] = []
        identifiers: set[str] = set()
        for index, raw_check in enumerate(raw_checks):
            check_path = f"{path}.profiles.{profile_name}[{index}]"
            check = _object(raw_check, check_path)
            _exact_keys(
                check,
                required={"id", "run", "timeoutSeconds"},
                optional={"workingDirectory"}
                | ({"components"} if schema_version >= 3 else set()),
                path=check_path,
            )
            check_id = _string(check["id"], f"{check_path}.id", max_length=64)
            if PROFILE_PATTERN.fullmatch(check_id) is None:
                raise ContractError(f"{check_path}.id: invalid check name")
            if check_id in identifiers:
                raise ContractError(
                    f"{path}.profiles.{profile_name}: duplicate check id {check_id}"
                )
            identifiers.add(check_id)
            argv = _string_list(
                check["run"],
                f"{check_path}.run",
                maximum=MAX_CONTRACT_ITEMS if schema_version == 4 else None,
            )
            timeout = _timeout(check["timeoutSeconds"], f"{check_path}.timeoutSeconds")
            working_directory = _working_directory(
                check.get("workingDirectory", "."),
                f"{check_path}.workingDirectory",
            )
            components = (
                _string_list(
                    check["components"],
                    f"{check_path}.components",
                    pattern=PROFILE_PATTERN,
                    maximum=MAX_IMPACT_COMPONENTS,
                )
                if "components" in check
                else None
            )
            if components is not None:
                if impact is None:
                    raise ContractError(
                        f"{check_path}.components: requires a project impact contract"
                    )
                if components != sorted(components):
                    raise ContractError(
                        f"{check_path}.components: components must be sorted"
                    )
                unknown_components = sorted(set(components) - set(impact.components))
                if unknown_components:
                    raise ContractError(
                        f"{check_path}.components: undefined components: "
                        + ", ".join(unknown_components)
                    )
            checks.append(
                Check(
                    identifier=check_id,
                    run=tuple(argv),
                    timeout_seconds=timeout,
                    working_directory=working_directory,
                    components=tuple(components) if components is not None else None,
                )
            )
        profiles[profile_name] = tuple(checks)
    missing_required = sorted(set(required_profiles) - set(profiles))
    if missing_required:
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: undefined profiles: "
            f"{', '.join(missing_required)}"
        )
    environment = (
        _validate_environment(
            value["environment"],
            f"{path}.environment",
            require_foreground_only=schema_version >= 3,
            require_native_windows_commands=schema_version >= 3,
            bounded_commands=schema_version == 4,
        )
        if schema_version >= 2
        else None
    )
    if environment is not None:
        missing_environment_profiles = sorted(set(profiles) - set(environment.profiles))
        if missing_environment_profiles:
            raise ContractError(
                f"{path}.environment.profiles: missing verification profiles: "
                + ", ".join(missing_environment_profiles)
            )
    return Project(
        identifier=identifier,
        profiles=profiles,
        required_profiles=tuple(required_profiles),
        environment=environment,
        impact=impact,
        quality_extensions=tuple(quality_extensions),
    )


def validate_process_lock(document: Any, path: str = "process.lock") -> ProcessLock:
    value = _object(document, path)
    _exact_keys(
        value,
        required={"schemaVersion", "process", "skills"},
        optional={"$schema"},
        path=path,
    )
    _schema_version(value, path)
    process = _object(value["process"], f"{path}.process")
    _exact_keys(
        process,
        required={"version", "digest"},
        path=f"{path}.process",
    )
    version = _string(process["version"], f"{path}.process.version", max_length=64)
    if SEMVER_PATTERN.fullmatch(version) is None:
        raise ContractError(f"{path}.process.version: must be SemVer")
    digest = _string(process["digest"], f"{path}.process.digest", max_length=71)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(
            f"{path}.process.digest: must be a lowercase sha256 digest"
        )
    skills = _string_list(value["skills"], f"{path}.skills", pattern=SKILL_PATTERN)
    if skills != sorted(skills):
        raise ContractError(f"{path}.skills: must be sorted")
    return ProcessLock(version=version, digest=digest, skills=tuple(skills))


def validate_adoption_migration(
    document: Any, path: str = "adoption migration"
) -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "fromProcessVersion",
            "toProcessVersion",
            "sourceProjectDigest",
            "targetProjectDigest",
            "project",
        },
        optional={"$schema"},
        path=path,
    )
    _schema_version(value, path)
    versions: list[str] = []
    for name in ("fromProcessVersion", "toProcessVersion"):
        version = _string(value[name], f"{path}.{name}", max_length=64)
        if FINAL_SEMVER_PATTERN.fullmatch(version) is None:
            raise ContractError(f"{path}.{name}: must be final SemVer X.Y.Z")
        versions.append(version)
    if versions[0] == versions[1]:
        raise ContractError(
            f"{path}: fromProcessVersion and toProcessVersion must differ"
        )
    for name in ("sourceProjectDigest", "targetProjectDigest"):
        digest = _string(value[name], f"{path}.{name}", max_length=71)
        if DIGEST_PATTERN.fullmatch(digest) is None:
            raise ContractError(
                f"{path}.{name}: must be a lowercase sha256 digest"
            )
    validate_project(value["project"], f"{path}.project")
    target_content = (
        json.dumps(value["project"], ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    target_digest = f"sha256:{hashlib.sha256(target_content).hexdigest()}"
    if value["targetProjectDigest"] != target_digest:
        raise ContractError(
            f"{path}.targetProjectDigest: does not match project content"
        )


def validate_release(document: Any, path: str = "release") -> Release:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {1, 2, 3}:
        raise ContractError(f"{path}.schemaVersion: must be 1, 2, or 3")
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "previousVersion",
            "version",
            "classification",
            "compatibility",
            "schemaImpact",
            "migration",
        }
        | (
            {"identity", "provenance", "changes"}
            if schema_version in {2, 3}
            else set()
        ),
        optional={"$schema"},
        path=path,
    )
    previous = _string(
        value["previousVersion"], f"{path}.previousVersion", max_length=64
    )
    current = _string(value["version"], f"{path}.version", max_length=64)
    previous_match = FINAL_SEMVER_PATTERN.fullmatch(previous)
    current_match = FINAL_SEMVER_PATTERN.fullmatch(current)
    if previous_match is None:
        raise ContractError(f"{path}.previousVersion: must be final SemVer X.Y.Z")
    if current_match is None:
        raise ContractError(f"{path}.version: must be final SemVer X.Y.Z")
    previous_parts = tuple(int(part) for part in previous_match.groups())
    current_parts = tuple(int(part) for part in current_match.groups())

    classification = value["classification"]
    if classification not in {"patch", "minor", "major"}:
        raise ContractError(f"{path}.classification: must be patch, minor, or major")
    expected = {
        "patch": (
            previous_parts[0],
            previous_parts[1],
            previous_parts[2] + 1,
        ),
        "minor": (previous_parts[0], previous_parts[1] + 1, 0),
        "major": (previous_parts[0] + 1, 0, 0),
    }[classification]
    if current_parts != expected:
        expected_text = ".".join(str(part) for part in expected)
        raise ContractError(
            f"{path}.version: {classification} after {previous} must be {expected_text}"
        )

    compatibility = value["compatibility"]
    if compatibility not in {"backward-compatible", "incompatible"}:
        raise ContractError(
            f"{path}.compatibility: must be backward-compatible or incompatible"
        )
    schema_impact = value["schemaImpact"]
    if schema_impact not in {"unchanged", "additive", "breaking"}:
        raise ContractError(
            f"{path}.schemaImpact: must be unchanged, additive, or breaking"
        )
    migration = value["migration"]
    if migration is not None:
        _string(migration, f"{path}.migration", max_length=1000)

    if classification == "patch" and compatibility != "backward-compatible":
        raise ContractError(f"{path}: a patch release must be backward-compatible")
    if (
        classification == "minor"
        and previous_parts[0] > 0
        and compatibility != "backward-compatible"
    ):
        raise ContractError(
            f"{path}: an incompatible stable release requires a major classification"
        )
    if schema_impact == "breaking" and compatibility != "incompatible":
        raise ContractError(
            f"{path}: a breaking schema impact must declare incompatible compatibility"
        )
    if compatibility == "incompatible" and migration is None:
        raise ContractError(f"{path}.migration: incompatible releases require guidance")
    if compatibility == "backward-compatible" and migration is not None:
        raise ContractError(
            f"{path}.migration: backward-compatible releases must use null"
        )

    package_name: str | None = None
    distribution_name: str | None = None
    tag: str | None = None
    release_name: str | None = None
    runtime_version_file: str | None = None
    runtime_version_variable: str | None = None
    artifacts: tuple[str, ...] = ()
    receipt_asset: str | None = None
    authorization_asset: str | None = None
    receipt_change_id: str | None = None
    receipt_project: str | None = None
    receipt_cycle: int | None = None
    provenance_mode = "legacy"
    if schema_version in {2, 3}:
        changes = value["changes"]
        if not isinstance(changes, list) or not changes:
            raise ContractError(f"{path}.changes: must not be empty")
        if len(changes) > MAX_CONTRACT_ITEMS:
            raise ContractError(f"{path}.changes: exceeds {MAX_CONTRACT_ITEMS} items")
        change_ids: list[str] = []
        change_types: set[str] = set()
        for index, raw_change in enumerate(changes):
            change_path = f"{path}.changes[{index}]"
            change = _object(raw_change, change_path)
            _exact_keys(
                change,
                required={"id", "type", "surfaces", "rationale"},
                path=change_path,
            )
            change_id = _string(change["id"], f"{change_path}.id", max_length=64)
            if PROFILE_PATTERN.fullmatch(change_id) is None:
                raise ContractError(f"{change_path}.id: invalid change id")
            change_ids.append(change_id)
            change_type = change["type"]
            if change_type not in {"fix", "capability", "breaking"}:
                raise ContractError(
                    f"{change_path}.type: must be fix, capability, or breaking"
                )
            change_types.add(change_type)
            surfaces = _string_list(
                change["surfaces"],
                f"{change_path}.surfaces",
                pattern=PROFILE_PATTERN,
            )
            if surfaces != sorted(surfaces):
                raise ContractError(f"{change_path}.surfaces: must be sorted")
            _string(change["rationale"], f"{change_path}.rationale", max_length=1000)
        if change_ids != sorted(change_ids):
            raise ContractError(f"{path}.changes: must be sorted by id")
        if len(change_ids) != len(set(change_ids)):
            raise ContractError(f"{path}.changes: duplicate ids are not allowed")

        version_plan = derive_release_version(previous, change_types)
        if classification != version_plan.classification:
            raise ContractError(
                f"{path}.classification: changes require {version_plan.classification}, "
                f"not {classification}"
            )
        if compatibility != version_plan.compatibility:
            raise ContractError(
                f"{path}.compatibility: changes require {version_plan.compatibility}"
            )
        if schema_impact == "breaking" and "breaking" not in change_types:
            raise ContractError(
                f"{path}.changes: breaking schema impact requires a breaking change"
            )
        if schema_impact == "additive" and not change_types.intersection(
            {"capability", "breaking"}
        ):
            raise ContractError(
                f"{path}.changes: additive schema impact requires a capability change"
            )

        identity = _object(value["identity"], f"{path}.identity")
        identity_required = {
            "package",
            "distribution",
            "tag",
            "releaseName",
            "runtimeVersion",
            "artifacts",
            "receiptAsset",
        }
        if schema_version == 3:
            identity_required.add("authorizationAsset")
        _exact_keys(
            identity,
            required=identity_required,
            path=f"{path}.identity",
        )
        package_name = _string(identity["package"], f"{path}.identity.package", max_length=128)
        if NAME_PATTERN.fullmatch(package_name) is None:
            raise ContractError(f"{path}.identity.package: invalid package name")
        distribution_name = _string(
            identity["distribution"], f"{path}.identity.distribution", max_length=128
        )
        expected_distribution = re.sub(r"[-_.]+", "_", package_name)
        if distribution_name != expected_distribution:
            raise ContractError(
                f"{path}.identity.distribution: must be {expected_distribution}"
            )
        tag = _string(identity["tag"], f"{path}.identity.tag", max_length=80)
        expected_tag = f"v{current}"
        if tag != expected_tag:
            raise ContractError(f"{path}.identity.tag: must be {expected_tag}")
        release_name = _string(
            identity["releaseName"], f"{path}.identity.releaseName", max_length=128
        )
        runtime = _object(identity["runtimeVersion"], f"{path}.identity.runtimeVersion")
        _exact_keys(
            runtime,
            required={"path", "variable"},
            path=f"{path}.identity.runtimeVersion",
        )
        runtime_version_file = _relative_tool_path(
            runtime["path"], f"{path}.identity.runtimeVersion.path", strict_portable=True
        )
        runtime_version_variable = _string(
            runtime["variable"],
            f"{path}.identity.runtimeVersion.variable",
            max_length=64,
        )
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", runtime_version_variable) is None:
            raise ContractError(
                f"{path}.identity.runtimeVersion.variable: invalid constant name"
            )
        artifact_items = _string_list(
            identity["artifacts"], f"{path}.identity.artifacts"
        )
        expected_artifacts = sorted(
            [
                f"{distribution_name}-{current}-py3-none-any.whl",
                f"{distribution_name}-{current}.tar.gz",
            ]
        )
        if artifact_items != expected_artifacts:
            raise ContractError(
                f"{path}.identity.artifacts: must be canonical artifacts "
                + ", ".join(expected_artifacts)
            )
        artifacts = tuple(artifact_items)
        receipt_asset = identity["receiptAsset"]
        if receipt_asset is not None:
            receipt_asset = _string(
                receipt_asset, f"{path}.identity.receiptAsset", max_length=200
            )
        if schema_version == 3:
            authorization_asset = identity["authorizationAsset"]
            if authorization_asset is not None:
                authorization_asset = _string(
                    authorization_asset,
                    f"{path}.identity.authorizationAsset",
                    max_length=200,
                )

        provenance = _object(value["provenance"], f"{path}.provenance")
        _exact_keys(
            provenance,
            required={"mode", "statement", "lifecycleReceipt"},
            path=f"{path}.provenance",
        )
        provenance_mode = provenance["mode"]
        allowed_modes = {"bootstrap-history", "governed"}
        if schema_version == 3:
            allowed_modes.add("bootstrap-authority")
        if provenance_mode not in allowed_modes:
            raise ContractError(
                f"{path}.provenance.mode: invalid for schemaVersion {schema_version}"
            )
        _string(
            provenance["statement"], f"{path}.provenance.statement", max_length=1000
        )
        lifecycle_receipt = provenance["lifecycleReceipt"]
        if provenance_mode == "bootstrap-history":
            if lifecycle_receipt is not None:
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt: bootstrap history must use null"
                )
            if receipt_asset is not None:
                raise ContractError(
                    f"{path}.identity.receiptAsset: bootstrap history must use null"
                )
            if authorization_asset is not None:
                raise ContractError(
                    f"{path}.identity.authorizationAsset: bootstrap history must use null"
                )
        elif provenance_mode == "bootstrap-authority":
            if release_name != expected_tag:
                raise ContractError(
                    f"{path}.identity.releaseName: bootstrap authority must use {expected_tag}"
                )
            if lifecycle_receipt is not None:
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt: bootstrap authority must use null"
                )
            if receipt_asset is not None:
                raise ContractError(
                    f"{path}.identity.receiptAsset: bootstrap authority must use null"
                )
            expected_authorization_asset = (
                f"{package_name}-{expected_tag}-bootstrap-authorization.json"
            )
            if authorization_asset != expected_authorization_asset:
                raise ContractError(
                    f"{path}.identity.authorizationAsset: must be "
                    f"{expected_authorization_asset}"
                )
        else:
            if release_name != expected_tag:
                raise ContractError(
                    f"{path}.identity.releaseName: governed releases must use {expected_tag}"
                )
            expected_receipt_asset = f"{package_name}-{expected_tag}-evidence.json"
            if receipt_asset != expected_receipt_asset:
                raise ContractError(
                    f"{path}.identity.receiptAsset: must be {expected_receipt_asset}"
                )
            if authorization_asset is not None:
                raise ContractError(
                    f"{path}.identity.authorizationAsset: governed releases must use null"
                )
            receipt = _object(
                lifecycle_receipt, f"{path}.provenance.lifecycleReceipt"
            )
            _exact_keys(
                receipt,
                required={"asset", "project", "changeId", "cycle"},
                path=f"{path}.provenance.lifecycleReceipt",
            )
            if receipt["asset"] != receipt_asset:
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt.asset: must match receiptAsset"
                )
            receipt_project = _string(
                receipt["project"],
                f"{path}.provenance.lifecycleReceipt.project",
                max_length=128,
            )
            if NAME_PATTERN.fullmatch(receipt_project) is None:
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt.project: invalid project id"
                )
            receipt_change_id = _string(
                receipt["changeId"],
                f"{path}.provenance.lifecycleReceipt.changeId",
                max_length=64,
            )
            if PROFILE_PATTERN.fullmatch(receipt_change_id) is None:
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt.changeId: invalid change id"
                )
            if (
                isinstance(receipt["cycle"], bool)
                or not isinstance(receipt["cycle"], int)
                or receipt["cycle"] < 1
            ):
                raise ContractError(
                    f"{path}.provenance.lifecycleReceipt.cycle: must be a positive integer"
                )
            receipt_cycle = receipt["cycle"]
    return Release(
        previous_version=previous,
        version=current,
        classification=classification,
        compatibility=compatibility,
        schema_impact=schema_impact,
        migration=migration,
        package_name=package_name,
        distribution_name=distribution_name,
        tag=tag,
        release_name=release_name,
        runtime_version_file=runtime_version_file,
        runtime_version_variable=runtime_version_variable,
        artifacts=artifacts,
        receipt_asset=receipt_asset,
        authorization_asset=authorization_asset,
        receipt_change_id=receipt_change_id,
        receipt_project=receipt_project,
        receipt_cycle=receipt_cycle,
        provenance_mode=provenance_mode,
    )


def validate_release_change(
    document: Any, path: str = "release-change"
) -> ReleaseChange:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "id",
            "type",
            "surfaces",
            "rationale",
            "schemaImpact",
            "migration",
        },
        optional={"$schema"},
        path=path,
    )
    if value["schemaVersion"] != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
    identifier = _string(value["id"], f"{path}.id", max_length=64)
    if PROFILE_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.id: invalid change id")
    change_type = value["type"]
    if change_type not in {"fix", "capability", "breaking"}:
        raise ContractError(f"{path}.type: must be fix, capability, or breaking")
    surfaces = _string_list(
        value["surfaces"],
        f"{path}.surfaces",
        pattern=PROFILE_PATTERN,
        maximum=MAX_CONTRACT_ITEMS,
    )
    if surfaces != sorted(surfaces):
        raise ContractError(f"{path}.surfaces: must be sorted")
    rationale = _string(value["rationale"], f"{path}.rationale", max_length=1000)
    schema_impact = value["schemaImpact"]
    if schema_impact not in {"unchanged", "additive", "breaking"}:
        raise ContractError(
            f"{path}.schemaImpact: must be unchanged, additive, or breaking"
        )
    migration = value["migration"]
    if migration is not None:
        migration = _string(migration, f"{path}.migration", max_length=1000)
    if change_type == "breaking" and migration is None:
        raise ContractError(f"{path}.migration: breaking changes require guidance")
    if change_type != "breaking" and migration is not None:
        raise ContractError(
            f"{path}.migration: backward-compatible changes must use null"
        )
    if schema_impact == "breaking" and change_type != "breaking":
        raise ContractError(
            f"{path}.schemaImpact: breaking schema impact requires a breaking change"
        )
    if schema_impact == "additive" and change_type == "fix":
        raise ContractError(
            f"{path}.schemaImpact: additive schema impact requires a capability change"
        )
    return ReleaseChange(
        identifier=identifier,
        change_type=change_type,
        surfaces=tuple(surfaces),
        rationale=rationale,
        schema_impact=schema_impact,
        migration=migration,
    )


def validate_change(document: Any, path: str = "change") -> None:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {2, 3}:
        raise ContractError(f"{path}.schemaVersion: must be 2 or 3")
    required_keys = {
        "schemaVersion",
        "id",
        "summary",
        "source",
        "comparisonBase",
        "specification",
        "risk",
        "affectedProjects",
        "acceptanceCriteria",
        "requiredProfiles",
        "signOff",
    }
    if schema_version == 3:
        required_keys.add("quality")
    _exact_keys(
        value,
        required=required_keys,
        optional={"$schema"},
        path=path,
    )
    identifier = _string(value["id"], f"{path}.id", max_length=64)
    if PROFILE_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.id: invalid change id")
    _string(value["summary"], f"{path}.summary", max_length=500)
    _string(value["source"], f"{path}.source", max_length=1000)
    _string(value["comparisonBase"], f"{path}.comparisonBase", max_length=256)
    specification = _object(value["specification"], f"{path}.specification")
    _exact_keys(
        specification,
        required={"kind", "reference", "rationale"},
        path=f"{path}.specification",
    )
    if specification["kind"] not in {"project", "change-contract"}:
        raise ContractError(
            f"{path}.specification.kind: must be project or change-contract"
        )
    _string(
        specification["reference"],
        f"{path}.specification.reference",
        max_length=1000,
    )
    _string(
        specification["rationale"],
        f"{path}.specification.rationale",
        max_length=2000,
    )
    if value["risk"] not in {"low", "medium", "high"}:
        raise ContractError(f"{path}.risk: must be low, medium, or high")
    affected_projects = _string_list(
        value["affectedProjects"],
        f"{path}.affectedProjects",
        pattern=NAME_PATTERN,
        maximum=64 if schema_version == 3 else None,
    )
    required_profiles = _string_list(
        value["requiredProfiles"],
        f"{path}.requiredProfiles",
        pattern=PROFILE_PATTERN,
        maximum=MAX_PROJECT_PROFILES if schema_version == 3 else None,
    )

    criteria = value["acceptanceCriteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ContractError(f"{path}.acceptanceCriteria: must not be empty")
    if schema_version == 3 and len(criteria) > MAX_CONTRACT_ITEMS:
        raise ContractError(
            f"{path}.acceptanceCriteria: exceeds {MAX_CONTRACT_ITEMS} items"
        )
    criterion_ids: set[str] = set()
    for index, raw_criterion in enumerate(criteria):
        criterion_path = f"{path}.acceptanceCriteria[{index}]"
        criterion = _object(raw_criterion, criterion_path)
        _exact_keys(
            criterion,
            required={"id", "outcome"},
            path=criterion_path,
        )
        identifier = _string(
            criterion["id"], f"{criterion_path}.id", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{criterion_path}.id: invalid criterion id")
        if identifier in criterion_ids:
            raise ContractError(
                f"{path}.acceptanceCriteria: duplicate id {identifier}"
            )
        criterion_ids.add(identifier)
        _string(criterion["outcome"], f"{criterion_path}.outcome", max_length=1000)

    if schema_version == 3:
        quality = _object(value["quality"], f"{path}.quality")
        _exact_keys(
            quality,
            required={"standard", "assessments"},
            path=f"{path}.quality",
        )
        if quality["standard"] != PRODUCTION_STANDARD:
            raise ContractError(
                f"{path}.quality.standard: must be {PRODUCTION_STANDARD}"
            )
        assessments = quality["assessments"]
        if not isinstance(assessments, list) or not assessments:
            raise ContractError(f"{path}.quality.assessments: must not be empty")
        if len(assessments) > MAX_CONTRACT_ITEMS:
            raise ContractError(
                f"{path}.quality.assessments: exceeds {MAX_CONTRACT_ITEMS} items"
            )
        dimensions: list[str] = []
        for index, raw_assessment in enumerate(assessments):
            assessment_path = f"{path}.quality.assessments[{index}]"
            assessment = _object(raw_assessment, assessment_path)
            _exact_keys(
                assessment,
                required={"dimension", "status", "rationale", "criteria"},
                path=assessment_path,
            )
            dimension = _string(
                assessment["dimension"],
                f"{assessment_path}.dimension",
                max_length=64,
            )
            if (
                PROFILE_PATTERN.fullmatch(dimension) is None
                or (
                    dimension not in CORE_QUALITY_DIMENSIONS
                    and not dimension.startswith("project-")
                )
            ):
                raise ContractError(
                    f"{assessment_path}.dimension: must be a core or project-* dimension"
                )
            dimensions.append(dimension)
            status = assessment["status"]
            if status not in {"applicable", "not-applicable"}:
                raise ContractError(
                    f"{assessment_path}.status: must be applicable or not-applicable"
                )
            _string(
                assessment["rationale"],
                f"{assessment_path}.rationale",
                max_length=1000,
            )
            mapped = _string_list(
                assessment["criteria"],
                f"{assessment_path}.criteria",
                minimum=0,
                pattern=PROFILE_PATTERN,
            )
            if mapped != sorted(mapped):
                raise ContractError(
                    f"{assessment_path}.criteria: must be sorted"
                )
            unknown = sorted(set(mapped) - criterion_ids)
            if unknown:
                raise ContractError(
                    f"{assessment_path}.criteria: unknown acceptance criteria: "
                    + ", ".join(unknown)
                )
            if status == "applicable" and not mapped:
                raise ContractError(
                    f"{assessment_path}.criteria: applicable dimensions require criteria"
                )
            if status == "not-applicable" and mapped:
                raise ContractError(
                    f"{assessment_path}.criteria: not-applicable dimensions require an empty list"
                )
        if dimensions != sorted(dimensions):
            raise ContractError(
                f"{path}.quality.assessments: must be sorted by dimension"
            )
        if len(dimensions) != len(set(dimensions)):
            raise ContractError(
                f"{path}.quality.assessments: duplicate dimensions are not allowed"
            )
        missing_core = sorted(set(CORE_QUALITY_DIMENSIONS) - set(dimensions))
        if missing_core:
            raise ContractError(
                f"{path}.quality.assessments: missing core dimensions: "
                + ", ".join(missing_core)
            )
        correctness = assessments[dimensions.index("correctness")]
        if correctness["status"] != "applicable":
            raise ContractError(
                f"{path}.quality: correctness must be applicable"
            )

    sign_off = _object(value["signOff"], f"{path}.signOff")
    _exact_keys(
        sign_off,
        required={"required", "status", "evidence"},
        path=f"{path}.signOff",
    )
    required = sign_off["required"]
    if not isinstance(required, bool):
        raise ContractError(f"{path}.signOff.required: must be boolean")
    status = sign_off["status"]
    allowed_statuses = {"pending", "approved"} if required else {"not-required"}
    if status not in allowed_statuses:
        raise ContractError(
            f"{path}.signOff.status: invalid for required={str(required).lower()}"
        )
    evidence = sign_off["evidence"]
    if status == "approved":
        _string(evidence, f"{path}.signOff.evidence", max_length=1000)
    elif evidence is not None:
        raise ContractError(
            f"{path}.signOff.evidence: must be null unless status is approved"
        )


def validate_plan(document: Any, path: str = "plan") -> None:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise ContractError(f"{path}.schemaVersion: must be 1 or 2")
    bounded = schema_version == 2
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "changeId",
            "contractDigest",
            "approach",
            "workItems",
            "acceptancePlan",
            "risks",
            "openDecisions",
        },
        optional={"$schema"},
        path=path,
    )
    change_id = _string(value["changeId"], f"{path}.changeId", max_length=64)
    if PROFILE_PATTERN.fullmatch(change_id) is None:
        raise ContractError(f"{path}.changeId: invalid change id")
    digest = _string(
        value["contractDigest"], f"{path}.contractDigest", max_length=71
    )
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(f"{path}.contractDigest: must be a lowercase sha256 digest")
    _string(value["approach"], f"{path}.approach", max_length=4000)

    work_items = value["workItems"]
    if not isinstance(work_items, list) or not work_items:
        raise ContractError(f"{path}.workItems: must not be empty")
    if bounded and len(work_items) > MAX_CONTRACT_ITEMS:
        raise ContractError(f"{path}.workItems: exceeds {MAX_CONTRACT_ITEMS} items")
    work_item_ids: set[str] = set()
    for index, raw_item in enumerate(work_items):
        item_path = f"{path}.workItems[{index}]"
        item = _object(raw_item, item_path)
        _exact_keys(
            item,
            required={"id", "outcome", "affectedPaths", "verificationProfiles"},
            path=item_path,
        )
        item_id = _string(item["id"], f"{item_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(item_id) is None:
            raise ContractError(f"{item_path}.id: invalid work-item id")
        if item_id in work_item_ids:
            raise ContractError(f"{path}.workItems: duplicate id {item_id}")
        work_item_ids.add(item_id)
        _string(item["outcome"], f"{item_path}.outcome", max_length=1000)
        _string_list(
            item["affectedPaths"],
            f"{item_path}.affectedPaths",
            maximum=MAX_CONTRACT_ITEMS if bounded else None,
        )
        _string_list(
            item["verificationProfiles"],
            f"{item_path}.verificationProfiles",
            pattern=PROFILE_PATTERN,
            maximum=MAX_PROJECT_PROFILES if bounded else None,
        )

    acceptance_plan = value["acceptancePlan"]
    if not isinstance(acceptance_plan, list) or not acceptance_plan:
        raise ContractError(f"{path}.acceptancePlan: must not be empty")
    if bounded and len(acceptance_plan) > MAX_CONTRACT_ITEMS:
        raise ContractError(
            f"{path}.acceptancePlan: exceeds {MAX_CONTRACT_ITEMS} items"
        )
    criterion_ids: set[str] = set()
    for index, raw_mapping in enumerate(acceptance_plan):
        mapping_path = f"{path}.acceptancePlan[{index}]"
        mapping = _object(raw_mapping, mapping_path)
        _exact_keys(
            mapping,
            required={"criterionId", "workItems", "verificationProfiles"},
            path=mapping_path,
        )
        criterion_id = _string(
            mapping["criterionId"], f"{mapping_path}.criterionId", max_length=64
        )
        if PROFILE_PATTERN.fullmatch(criterion_id) is None:
            raise ContractError(f"{mapping_path}.criterionId: invalid criterion id")
        if criterion_id in criterion_ids:
            raise ContractError(
                f"{path}.acceptancePlan: duplicate criterion {criterion_id}"
            )
        criterion_ids.add(criterion_id)
        mapped_items = _string_list(
            mapping["workItems"],
            f"{mapping_path}.workItems",
            pattern=PROFILE_PATTERN,
            maximum=MAX_CONTRACT_ITEMS if bounded else None,
        )
        unknown_items = sorted(set(mapped_items) - work_item_ids)
        if unknown_items:
            raise ContractError(
                f"{mapping_path}.workItems: unknown ids: {', '.join(unknown_items)}"
            )
        _string_list(
            mapping["verificationProfiles"],
            f"{mapping_path}.verificationProfiles",
            pattern=PROFILE_PATTERN,
            maximum=MAX_PROJECT_PROFILES if bounded else None,
        )

    risks = value["risks"]
    if not isinstance(risks, list):
        raise ContractError(f"{path}.risks: must be an array")
    if bounded and len(risks) > MAX_CONTRACT_ITEMS:
        raise ContractError(f"{path}.risks: exceeds {MAX_CONTRACT_ITEMS} items")
    for index, raw_risk in enumerate(risks):
        risk_path = f"{path}.risks[{index}]"
        risk = _object(raw_risk, risk_path)
        _exact_keys(risk, required={"risk", "mitigation"}, path=risk_path)
        _string(risk["risk"], f"{risk_path}.risk", max_length=1000)
        _string(risk["mitigation"], f"{risk_path}.mitigation", max_length=1000)

    decisions = value["openDecisions"]
    if not isinstance(decisions, list):
        raise ContractError(f"{path}.openDecisions: must be an array")
    if bounded and len(decisions) > MAX_CONTRACT_ITEMS:
        raise ContractError(
            f"{path}.openDecisions: exceeds {MAX_CONTRACT_ITEMS} items"
        )
    if decisions:
        _string_list(
            decisions,
            f"{path}.openDecisions",
            maximum=MAX_CONTRACT_ITEMS if bounded else None,
        )


def _validate_actor(value: Any, path: str) -> dict[str, str]:
    actor = _object(value, path)
    _exact_keys(actor, required={"actorId", "contextId", "kind"}, path=path)
    actor_id = _string(actor["actorId"], f"{path}.actorId", max_length=256)
    context_id = _string(actor["contextId"], f"{path}.contextId", max_length=256)
    kind = actor["kind"]
    if kind not in {"agent", "human"}:
        raise ContractError(f"{path}.kind: must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def _validate_review(
    document: Any,
    path: str = "review",
    *,
    allow_legacy_unresolved_approval: bool = False,
) -> None:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {2, 3}:
        raise ContractError(f"{path}.schemaVersion: must be 2 or 3")
    required_keys = {
        "schemaVersion",
        "changeId",
        "cycle",
        "checkpoint",
        "workspaceFingerprint",
        "comparisonBase",
        "reviewer",
        "independence",
        "verdict",
        "findings",
    }
    if schema_version == 3:
        required_keys.add("quality")
    _exact_keys(
        value,
        required=required_keys,
        optional={"$schema"},
        path=path,
    )
    change_id = _string(value["changeId"], f"{path}.changeId", max_length=64)
    if PROFILE_PATTERN.fullmatch(change_id) is None:
        raise ContractError(f"{path}.changeId: invalid change id")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")
    _string(value["checkpoint"], f"{path}.checkpoint", max_length=256)
    fingerprint = _string(
        value["workspaceFingerprint"],
        f"{path}.workspaceFingerprint",
        max_length=71,
    )
    if DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise ContractError(
            f"{path}.workspaceFingerprint: must be a lowercase sha256 digest"
        )
    _string(value["comparisonBase"], f"{path}.comparisonBase", max_length=256)
    _validate_actor(value["reviewer"], f"{path}.reviewer")
    independence = _object(value["independence"], f"{path}.independence")
    _exact_keys(
        independence,
        required={"method", "attestedBy", "evidence"},
        path=f"{path}.independence",
    )
    if independence["method"] not in {"isolated-context", "separate-person"}:
        raise ContractError(f"{path}.independence.method: invalid method")
    _string(
        independence["attestedBy"],
        f"{path}.independence.attestedBy",
        max_length=256,
    )
    _string(
        independence["evidence"],
        f"{path}.independence.evidence",
        max_length=2000,
    )
    verdict = value["verdict"]
    if verdict not in {"approved", "changes-requested"}:
        raise ContractError(
            f"{path}.verdict: must be approved or changes-requested"
        )
    if schema_version == 3:
        quality = _object(value["quality"], f"{path}.quality")
        _exact_keys(
            quality,
            required={"standard", "assessments"},
            path=f"{path}.quality",
        )
        if quality["standard"] != PRODUCTION_STANDARD:
            raise ContractError(
                f"{path}.quality.standard: must be {PRODUCTION_STANDARD}"
            )
        assessments = quality["assessments"]
        if not isinstance(assessments, list) or not assessments:
            raise ContractError(f"{path}.quality.assessments: must not be empty")
        if len(assessments) > MAX_CONTRACT_ITEMS:
            raise ContractError(
                f"{path}.quality.assessments: exceeds {MAX_CONTRACT_ITEMS} items"
            )
        quality_dimensions: list[str] = []
        for index, raw_assessment in enumerate(assessments):
            assessment_path = f"{path}.quality.assessments[{index}]"
            assessment = _object(raw_assessment, assessment_path)
            _exact_keys(
                assessment,
                required={"dimension", "status", "criteria", "evidence"},
                path=assessment_path,
            )
            dimension = _string(
                assessment["dimension"],
                f"{assessment_path}.dimension",
                max_length=64,
            )
            if (
                PROFILE_PATTERN.fullmatch(dimension) is None
                or (
                    dimension not in CORE_QUALITY_DIMENSIONS
                    and not dimension.startswith("project-")
                )
            ):
                raise ContractError(f"{assessment_path}.dimension: invalid dimension")
            quality_dimensions.append(dimension)
            if assessment["status"] not in {
                "verified",
                "failed",
                "not-applicable-confirmed",
            }:
                raise ContractError(f"{assessment_path}.status: invalid review status")
            criteria = _string_list(
                assessment["criteria"],
                f"{assessment_path}.criteria",
                minimum=0,
                maximum=MAX_CONTRACT_ITEMS,
                pattern=PROFILE_PATTERN,
            )
            if criteria != sorted(criteria):
                raise ContractError(f"{assessment_path}.criteria: must be sorted")
            if assessment["status"] in {"verified", "failed"} and not criteria:
                raise ContractError(
                    f"{assessment_path}.criteria: applicable dimensions require criteria"
                )
            if assessment["status"] == "not-applicable-confirmed" and criteria:
                raise ContractError(
                    f"{assessment_path}.criteria: confirmed N/A requires an empty list"
                )
            _string(
                assessment["evidence"],
                f"{assessment_path}.evidence",
                max_length=2000,
            )
        if quality_dimensions != sorted(quality_dimensions):
            raise ContractError(
                f"{path}.quality.assessments: must be sorted by dimension"
            )
        if len(quality_dimensions) != len(set(quality_dimensions)):
            raise ContractError(
                f"{path}.quality.assessments: duplicate dimensions are not allowed"
            )
        missing_core = sorted(set(CORE_QUALITY_DIMENSIONS) - set(quality_dimensions))
        if missing_core:
            raise ContractError(
                f"{path}.quality.assessments: missing core dimensions: "
                + ", ".join(missing_core)
            )
        failed_quality = [
            assessment
            for assessment in assessments
            if assessment["status"] == "failed"
        ]
        if verdict == "approved" and failed_quality:
            raise ContractError(f"{path}.quality: approved review has failed dimensions")
        if verdict == "changes-requested" and not failed_quality:
            raise ContractError(
                f"{path}.quality: changes-requested review requires a failed dimension"
            )
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ContractError(f"{path}.findings: must be an array")
    if schema_version == 3 and len(findings) > MAX_CONTRACT_ITEMS:
        raise ContractError(f"{path}.findings: exceeds {MAX_CONTRACT_ITEMS} items")
    finding_ids: set[str] = set()
    unresolved_findings = 0
    for index, raw_finding in enumerate(findings):
        finding_path = f"{path}.findings[{index}]"
        finding = _object(raw_finding, finding_path)
        _exact_keys(
            finding,
            required={
                "id",
                "severity",
                "path",
                "line",
                "summary",
                "evidence",
                "status",
                "resolutionEvidence",
            },
            path=finding_path,
        )
        identifier = _string(finding["id"], f"{finding_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{finding_path}.id: invalid finding id")
        if identifier in finding_ids:
            raise ContractError(f"{path}.findings: duplicate id {identifier}")
        finding_ids.add(identifier)
        if finding["severity"] not in {"critical", "high", "medium", "low"}:
            raise ContractError(f"{finding_path}.severity: invalid severity")
        _string(finding["path"], f"{finding_path}.path", max_length=1000)
        line = finding["line"]
        if line is not None and (
            isinstance(line, bool) or not isinstance(line, int) or line < 1
        ):
            raise ContractError(
                f"{finding_path}.line: must be null or a positive integer"
            )
        _string(finding["summary"], f"{finding_path}.summary", max_length=1000)
        _string(finding["evidence"], f"{finding_path}.evidence", max_length=4000)
        if finding["status"] not in {
            "open",
            "resolved",
            "deferred",
            "false-positive",
        }:
            raise ContractError(f"{finding_path}.status: invalid status")
        if finding["status"] in {"open", "deferred"}:
            unresolved_findings += 1
        if finding["status"] == "open":
            if finding["resolutionEvidence"] is not None:
                raise ContractError(
                    f"{finding_path}.resolutionEvidence: must be null while open"
                )
        else:
            _string(
                finding["resolutionEvidence"],
                f"{finding_path}.resolutionEvidence",
                max_length=4000,
            )
    if (
        verdict == "approved"
        and unresolved_findings
        and not allow_legacy_unresolved_approval
    ):
        raise ContractError(
            f"{path}: approved review cannot contain open or deferred findings"
        )
    if verdict == "changes-requested" and not unresolved_findings:
        raise ContractError(
            f"{path}: changes-requested review must contain an open or deferred finding"
        )


def validate_review(document: Any, path: str = "review") -> None:
    _validate_review(document, path, allow_legacy_unresolved_approval=False)


def _validate_legacy_review(document: Any, path: str) -> None:
    _validate_review(document, path, allow_legacy_unresolved_approval=True)


def _artifact_reference(value: Any, path: str) -> None:
    reference = _object(value, path)
    _exact_keys(reference, required={"path", "digest"}, path=path)
    relative = _string(reference["path"], f"{path}.path", max_length=1000)
    candidate = PurePosixPath(relative)
    if (
        "\\" in relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != relative
    ):
        raise ContractError(f"{path}.path: must be a portable relative path")
    digest = _string(reference["digest"], f"{path}.digest", max_length=71)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError(f"{path}.digest: must be a lowercase sha256 digest")


def _verification_reference(value: Any, path: str) -> None:
    reference = _object(value, path)
    _exact_keys(
        reference,
        required={"profile", "path", "digest", "checkpoint", "workspaceFingerprint"},
        path=path,
    )
    profile = _string(reference["profile"], f"{path}.profile", max_length=64)
    if PROFILE_PATTERN.fullmatch(profile) is None:
        raise ContractError(f"{path}.profile: invalid profile")
    relative = _string(reference["path"], f"{path}.path", max_length=1000)
    candidate = PurePosixPath(relative)
    if (
        "\\" in relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != relative
    ):
        raise ContractError(f"{path}.path: must be a portable relative path")
    for name in ("digest", "workspaceFingerprint"):
        digest = _string(reference[name], f"{path}.{name}", max_length=71)
        if DIGEST_PATTERN.fullmatch(digest) is None:
            raise ContractError(f"{path}.{name}: must be a lowercase sha256 digest")
    checkpoint = _string(
        reference["checkpoint"], f"{path}.checkpoint", max_length=128
    )
    if re.fullmatch(r"[0-9a-f]{40,64}", checkpoint) is None:
        raise ContractError(f"{path}.checkpoint: invalid commit digest")


def _validate_diagnostics(document: Any, path: str) -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "policy",
            "status",
            "count",
            "matches",
            "matchesTruncated",
        },
        path=path,
    )
    if value["policy"] != "forbid-warning-error":
        raise ContractError(f"{path}.policy: invalid diagnostic policy")
    if value["status"] not in {"clean", "failed"}:
        raise ContractError(f"{path}.status: must be clean or failed")
    count = value["count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ContractError(f"{path}.count: must be a non-negative integer")
    matches = value["matches"]
    if not isinstance(matches, list) or len(matches) > 8:
        raise ContractError(f"{path}.matches: must contain at most 8 items")
    for index, raw_match in enumerate(matches):
        match_path = f"{path}.matches[{index}]"
        match = _object(raw_match, match_path)
        _exact_keys(
            match,
            required={"severity", "stream", "line", "lineSha256"},
            path=match_path,
        )
        if match["severity"] not in {"warning", "error"}:
            raise ContractError(f"{match_path}.severity: invalid severity")
        if match["stream"] not in {"stdout", "stderr"}:
            raise ContractError(f"{match_path}.stream: invalid stream")
        line = match["line"]
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ContractError(f"{match_path}.line: must be a positive integer")
        digest = _string(
            match["lineSha256"], f"{match_path}.lineSha256", max_length=64
        )
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ContractError(f"{match_path}.lineSha256: invalid sha256")
    truncated = value["matchesTruncated"]
    if not isinstance(truncated, bool):
        raise ContractError(f"{path}.matchesTruncated: must be boolean")
    if count < len(matches):
        raise ContractError(f"{path}.count: cannot be less than recorded matches")
    if truncated != (count > len(matches)):
        raise ContractError(
            f"{path}.matchesTruncated: does not match diagnostic count"
        )
    if value["status"] == "clean" and (
        count != 0 or matches or truncated
    ):
        raise ContractError(f"{path}: clean diagnostics contain findings")
    if value["status"] == "failed" and (count == 0 or not matches):
        raise ContractError(f"{path}: failed diagnostics require a recorded finding")


def validate_verification(document: Any, path: str = "verification") -> None:
    value = _object(document, path)
    schema_version = value.get("schemaVersion")
    if schema_version not in {1, 2, 3}:
        raise ContractError(f"{path}.schemaVersion: must be 1, 2, or 3")
    required = {
        "schemaVersion",
        "project",
        "profile",
        "checkpoint",
        "workingTreeDirty",
        "workspaceFingerprint",
        "completedWorkspaceFingerprint",
        "sourceChangedDuringVerification",
        "startedAt",
        "completedAt",
        "status",
        "checks",
    }
    _exact_keys(
        value,
        required=required | ({"impact"} if schema_version >= 2 else set()),
        optional={"impact"} if schema_version == 1 else set(),
        path=path,
    )
    project = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(project) is None:
        raise ContractError(f"{path}.project: invalid project")
    profile = _string(value["profile"], f"{path}.profile", max_length=64)
    if PROFILE_PATTERN.fullmatch(profile) is None:
        raise ContractError(f"{path}.profile: invalid profile")
    checkpoint = value["checkpoint"]
    if checkpoint is not None:
        _string(checkpoint, f"{path}.checkpoint", max_length=128)
    dirty = value["workingTreeDirty"]
    if dirty is not None and not isinstance(dirty, bool):
        raise ContractError(f"{path}.workingTreeDirty: must be boolean or null")
    for name in ("workspaceFingerprint", "completedWorkspaceFingerprint"):
        fingerprint = value[name]
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or DIGEST_PATTERN.fullmatch(fingerprint) is None
        ):
            raise ContractError(f"{path}.{name}: must be null or a sha256 digest")
    if not isinstance(value["sourceChangedDuringVerification"], bool):
        raise ContractError(f"{path}.sourceChangedDuringVerification: must be boolean")
    _string(value["startedAt"], f"{path}.startedAt", max_length=64)
    _string(value["completedAt"], f"{path}.completedAt", max_length=64)
    if value["status"] not in {"passed", "failed"}:
        raise ContractError(f"{path}.status: must be passed or failed")

    checks = value["checks"]
    if not isinstance(checks, list):
        raise ContractError(f"{path}.checks: must be an array")
    if schema_version == 1 and not checks:
        raise ContractError(f"{path}.checks: schema 1 requires at least one check")
    if schema_version >= 2 and len(checks) > MAX_CONTRACT_ITEMS:
        raise ContractError(f"{path}.checks: exceeds {MAX_CONTRACT_ITEMS} items")
    check_ids: list[str] = []
    for index, raw_check in enumerate(checks):
        check_path = f"{path}.checks[{index}]"
        check = _object(raw_check, check_path)
        required_check = {
            "id",
            "status",
            "exitCode",
            "startedAt",
            "durationMs",
            "workingDirectory",
            "command",
            "commandSha256",
        }
        evidence_fields = {
            "impactSha256",
            "impactIntegrity",
            "stdoutBytes",
            "stderrBytes",
            "stdoutSha256",
            "stderrSha256",
            "outputTruncated",
            "streamOutputTruncated",
        }
        diagnostic_fields = {"diagnostics"}
        _exact_keys(
            check,
            required=required_check
            | (evidence_fields if schema_version >= 2 else set())
            | (diagnostic_fields if schema_version == 3 else set()),
            optional={"error", "pathEntries", "timeoutSeconds"}
            | (evidence_fields if schema_version == 1 else set()),
            path=check_path,
        )
        identifier = _string(check["id"], f"{check_path}.id", max_length=64)
        if PROFILE_PATTERN.fullmatch(identifier) is None:
            raise ContractError(f"{check_path}.id: invalid check id")
        check_ids.append(identifier)
        if check["status"] not in {"passed", "failed", "timed-out", "failed-to-start"}:
            raise ContractError(f"{check_path}.status: invalid status")
        exit_code = check["exitCode"]
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ContractError(f"{check_path}.exitCode: must be integer or null")
        duration = check["durationMs"]
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise ContractError(f"{check_path}.durationMs: must be a non-negative integer")
        if "timeoutSeconds" in check:
            _timeout(check["timeoutSeconds"], f"{check_path}.timeoutSeconds")
        _string(check["startedAt"], f"{check_path}.startedAt", max_length=64)
        _string(check["workingDirectory"], f"{check_path}.workingDirectory", max_length=512)
        command = check["command"]
        if not isinstance(command, list) or not command:
            raise ContractError(f"{check_path}.command: must not be empty")
        if schema_version >= 2 and len(command) > MAX_CONTRACT_ITEMS:
            raise ContractError(
                f"{check_path}.command: exceeds {MAX_CONTRACT_ITEMS} items"
            )
        for argument_index, argument in enumerate(command):
            _string(argument, f"{check_path}.command[{argument_index}]")
        command_digest = _string(
            check["commandSha256"], f"{check_path}.commandSha256", max_length=64
        )
        if re.fullmatch(r"[0-9a-f]{64}", command_digest) is None:
            raise ContractError(f"{check_path}.commandSha256: invalid sha256")
        expected_command_digest = hashlib.sha256(
            json.dumps(
                command, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if command_digest != expected_command_digest:
            raise ContractError(
                f"{check_path}.commandSha256: does not match command"
            )
        if "pathEntries" in check:
            entries = check["pathEntries"]
            if not isinstance(entries, list):
                raise ContractError(f"{check_path}.pathEntries: must be an array")
            for entry_index, entry in enumerate(entries):
                _string(entry, f"{check_path}.pathEntries[{entry_index}]")
        if "error" in check:
            _string(check["error"], f"{check_path}.error", max_length=4000)
        for name in ("impactSha256", "stdoutSha256", "stderrSha256"):
            if name in check and re.fullmatch(r"[0-9a-f]{64}", check[name]) is None:
                raise ContractError(f"{check_path}.{name}: invalid sha256")
        if "impactIntegrity" in check and check["impactIntegrity"] not in {
            "verified",
            "failed",
        }:
            raise ContractError(f"{check_path}.impactIntegrity: invalid status")
        for name in ("stdoutBytes", "stderrBytes"):
            if name in check and (
                isinstance(check[name], bool)
                or not isinstance(check[name], int)
                or check[name] < 0
            ):
                raise ContractError(f"{check_path}.{name}: must be non-negative")
        for name in ("outputTruncated", "streamOutputTruncated"):
            if name in check and not isinstance(check[name], bool):
                raise ContractError(f"{check_path}.{name}: must be boolean")
        if schema_version == 3:
            _validate_diagnostics(
                check["diagnostics"], f"{check_path}.diagnostics"
            )
        if check["status"] == "passed" and (
            check["exitCode"] != 0
            or check.get("impactIntegrity") == "failed"
            or (
                schema_version == 3
                and check["diagnostics"]["status"] != "clean"
            )
            or "error" in check
        ):
            raise ContractError(f"{check_path}: passing check has contradictory evidence")
    if len(check_ids) != len(set(check_ids)):
        raise ContractError(f"{path}.checks: duplicate ids are not allowed")

    if schema_version >= 2:
        impact = _object(value["impact"], f"{path}.impact")
        _exact_keys(
            impact,
            required={
                "schemaVersion",
                "mode",
                "profile",
                "selectedCheckIds",
                "skippedCheckIds",
                "checkSelection",
            },
            optional={
                "baseRef",
                "baseCommit",
                "headCommit",
                "mergeBase",
                "changedPaths",
                "directlyChangedComponents",
                "affectedComponents",
                "unmatchedPaths",
            },
            path=f"{path}.impact",
        )
        if impact["schemaVersion"] != 1:
            raise ContractError(f"{path}.impact.schemaVersion: must be 1")
        if impact["mode"] not in {"full-profile", "affected-checks"}:
            raise ContractError(f"{path}.impact.mode: invalid mode")
        if impact["profile"] != profile:
            raise ContractError(f"{path}.impact.profile: does not match report")
        selected = _string_list(
            impact["selectedCheckIds"],
            f"{path}.impact.selectedCheckIds",
            minimum=0,
            maximum=MAX_CONTRACT_ITEMS,
        )
        skipped = _string_list(
            impact["skippedCheckIds"],
            f"{path}.impact.skippedCheckIds",
            minimum=0,
            maximum=MAX_CONTRACT_ITEMS,
        )
        if selected != check_ids:
            raise ContractError(f"{path}.impact.selectedCheckIds: does not match checks")
        if set(selected).intersection(skipped):
            raise ContractError(
                f"{path}.impact: selected and skipped check ids must be disjoint"
            )
        for name in ("baseRef", "baseCommit", "headCommit", "mergeBase"):
            if name in impact:
                _string(impact[name], f"{path}.impact.{name}", max_length=512)
        for name in ("changedPaths", "unmatchedPaths"):
            if name in impact:
                _string_list(
                    impact[name],
                    f"{path}.impact.{name}",
                    minimum=0,
                    maximum=5_000,
                )
        for name in ("directlyChangedComponents", "affectedComponents"):
            if name in impact:
                _string_list(
                    impact[name],
                    f"{path}.impact.{name}",
                    minimum=0,
                    maximum=MAX_CONTRACT_ITEMS,
                )
        if impact["mode"] == "affected-checks":
            required_scope = {
                "baseRef",
                "baseCommit",
                "headCommit",
                "mergeBase",
                "changedPaths",
                "directlyChangedComponents",
                "affectedComponents",
                "unmatchedPaths",
            }
            missing_scope = sorted(required_scope - set(impact))
            if missing_scope:
                raise ContractError(
                    f"{path}.impact: affected-checks evidence is missing: "
                    + ", ".join(missing_scope)
                )
        selection = impact["checkSelection"]
        if not isinstance(selection, list) or len(selection) > MAX_CONTRACT_ITEMS:
            raise ContractError(f"{path}.impact.checkSelection: invalid selection")
        selection_ids: list[str] = []
        for index, raw_selection in enumerate(selection):
            selection_path = f"{path}.impact.checkSelection[{index}]"
            item = _object(raw_selection, selection_path)
            _exact_keys(
                item,
                required={
                    "id",
                    "selected",
                    "reason",
                    "components",
                    "matchedComponents",
                },
                path=selection_path,
            )
            selection_id = _string(
                item["id"], f"{selection_path}.id", max_length=64
            )
            if PROFILE_PATTERN.fullmatch(selection_id) is None:
                raise ContractError(f"{selection_path}.id: invalid check id")
            selection_ids.append(selection_id)
            if not isinstance(item["selected"], bool):
                raise ContractError(f"{selection_path}.selected: must be boolean")
            if item["reason"] not in {
                "profile-has-no-impact-contract",
                "unscoped-always-run",
                "unmatched-path-fallback",
                "affected-component",
                "no-affected-component",
            }:
                raise ContractError(f"{selection_path}.reason: invalid reason")
            components = _string_list(
                item["components"],
                f"{selection_path}.components",
                minimum=0,
                maximum=MAX_CONTRACT_ITEMS,
            )
            matched = _string_list(
                item["matchedComponents"],
                f"{selection_path}.matchedComponents",
                minimum=0,
                maximum=MAX_CONTRACT_ITEMS,
            )
            if not set(matched).issubset(components):
                raise ContractError(
                    f"{selection_path}.matchedComponents: must be declared components"
                )
            if item["selected"] != (selection_id in selected):
                raise ContractError(
                    f"{selection_path}.selected: does not match selectedCheckIds"
                )
        if len(selection_ids) != len(set(selection_ids)):
            raise ContractError(f"{path}.impact.checkSelection: duplicate ids")
        if set(selection_ids) != set(selected).union(skipped):
            raise ContractError(
                f"{path}.impact.checkSelection: does not cover selected and skipped ids"
            )
        impact_digest = hashlib.sha256(
            (
                json.dumps(impact, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        if any(check["impactSha256"] != impact_digest for check in checks):
            raise ContractError(f"{path}.checks: impactSha256 does not match impact")

    if value["status"] == "passed" and (
        value["sourceChangedDuringVerification"]
        or any(check["status"] != "passed" for check in checks)
    ):
        raise ContractError(f"{path}: passing verification contains failed evidence")
    before_fingerprint = value["workspaceFingerprint"]
    after_fingerprint = value["completedWorkspaceFingerprint"]
    if (
        before_fingerprint is not None
        and after_fingerprint is not None
        and value["sourceChangedDuringVerification"]
        != (before_fingerprint != after_fingerprint)
    ):
        raise ContractError(
            f"{path}.sourceChangedDuringVerification: contradicts workspace fingerprints"
        )


def validate_completion(document: Any, path: str = "completion") -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "changeId",
            "cycle",
            "checkpoint",
            "workspaceFingerprint",
            "comparisonBase",
            "completedAt",
            "completedBy",
            "contract",
            "plan",
            "verification",
            "review",
        },
        path=path,
    )
    if value["schemaVersion"] != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
    change_id = _string(value["changeId"], f"{path}.changeId", max_length=64)
    if PROFILE_PATTERN.fullmatch(change_id) is None:
        raise ContractError(f"{path}.changeId: invalid change id")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")
    checkpoint = _string(value["checkpoint"], f"{path}.checkpoint", max_length=128)
    if re.fullmatch(r"[0-9a-f]{40,64}", checkpoint) is None:
        raise ContractError(f"{path}.checkpoint: invalid commit digest")
    fingerprint = _string(
        value["workspaceFingerprint"], f"{path}.workspaceFingerprint", max_length=71
    )
    if DIGEST_PATTERN.fullmatch(fingerprint) is None:
        raise ContractError(f"{path}.workspaceFingerprint: invalid sha256 digest")
    _string(value["comparisonBase"], f"{path}.comparisonBase", max_length=256)
    _string(value["completedAt"], f"{path}.completedAt", max_length=64)
    _validate_actor(value["completedBy"], f"{path}.completedBy")
    for name in ("contract", "plan", "review"):
        _artifact_reference(value[name], f"{path}.{name}")
    verification = value["verification"]
    if not isinstance(verification, list) or not verification:
        raise ContractError(f"{path}.verification: must not be empty")
    profiles: list[str] = []
    for index, reference in enumerate(verification):
        _verification_reference(reference, f"{path}.verification[{index}]")
        profiles.append(reference["profile"])
    if len(profiles) != len(set(profiles)):
        raise ContractError(f"{path}.verification: duplicate profiles")
