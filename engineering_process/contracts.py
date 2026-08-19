from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import regex as bounded_regex


NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PROFILE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
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


class ContractError(ValueError):
    """Raised when a process contract is invalid."""


@dataclass(frozen=True)
class Check:
    identifier: str
    run: tuple[str, ...]
    timeout_seconds: int
    working_directory: str


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
class ManagedToolArtifact:
    platform: str
    url: str
    checksum: str
    archive_format: str
    strip_components: int
    max_download_bytes: int
    max_extracted_bytes: int
    max_files: int
    commands: dict[str, str]


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


@dataclass(frozen=True)
class ProcessLock:
    version: str
    digest: str
    skills: tuple[str, ...]


def read_json(path: Path) -> Any:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"{path}: cannot read: {error}") from error
    if len(data) > 1_000_000:
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
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"{path}: must contain at least {minimum} item(s)")
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


def _relative_tool_path(value: Any, path: str) -> str:
    text = _string(value, path, max_length=512)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts or text in {".", ".."}:
        raise ContractError(f"{path}: must be a contained relative file path")
    return candidate.as_posix()


def _https_url(value: Any, path: str) -> str:
    text = _string(value, path, max_length=2048)
    if any(ord(character) < 0x21 or ord(character) > 0x7e for character in text):
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


def _validate_environment(document: Any, path: str) -> ProjectEnvironment:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
            "defaultProfile",
            "foregroundOnly",
            "managedTools",
            "profiles",
            "requirements",
            "setupActions",
        },
        path=path,
    )
    default_profile = _string(
        value["defaultProfile"], f"{path}.defaultProfile", max_length=64
    )
    if PROFILE_PATTERN.fullmatch(default_profile) is None:
        raise ContractError(f"{path}.defaultProfile: invalid profile name")
    if value["foregroundOnly"] is not True:
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
            commands: dict[str, str] = {}
            for command_name, relative_path in raw_commands.items():
                if COMMAND_PATTERN.fullmatch(command_name) is None:
                    raise ContractError(
                        f"{artifact_path}.commands.{command_name}: invalid command name"
                    )
                commands[command_name] = _relative_tool_path(
                    relative_path, f"{artifact_path}.commands.{command_name}"
                )
                basename = Path(commands[command_name]).name
                allowed_basenames = (
                    {
                        command_name.casefold(),
                        f"{command_name}.bat".casefold(),
                        f"{command_name}.cmd".casefold(),
                        f"{command_name}.exe".casefold(),
                    }
                    if platform_name.startswith("windows-")
                    else {command_name.casefold()}
                )
                if basename.casefold() not in allowed_basenames:
                    raise ContractError(
                        f"{artifact_path}.commands.{command_name}: executable basename "
                        "must match the command name"
                    )
            if list(commands) != sorted(commands):
                raise ContractError(f"{artifact_path}.commands: must be sorted")
            if archive_format == "file" and (
                strip_components != 0 or len(commands) != 1
            ):
                raise ContractError(
                    f"{artifact_path}: file artifacts require stripComponents 0 and one command"
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
            run = tuple(_string_list(action["run"], f"{action_path}.run"))
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
                    _string_list(raw_probe["run"], f"{requirement_path}.probe.run")
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
        foreground_only=True,
        profiles=profiles,
        requirements=requirements,
        managed_tools=managed_tools,
        setup_actions=actions,
    )


def validate_project(document: Any, path: str = "project") -> Project:
    value = _object(document, path)
    if value.get("schemaVersion") != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
    _exact_keys(
        value,
        required={
            "schemaVersion",
            "project",
            "lifecycle",
            "profiles",
            "environment",
        },
        optional={"$schema"},
        path=path,
    )
    identifier = _string(value["project"], f"{path}.project", max_length=128)
    if NAME_PATTERN.fullmatch(identifier) is None:
        raise ContractError(f"{path}.project: must use lowercase project-id format")

    lifecycle = _object(value["lifecycle"], f"{path}.lifecycle")
    _exact_keys(
        lifecycle,
        required={"requiredProfiles"},
        path=f"{path}.lifecycle",
    )
    required_profiles = _string_list(
        lifecycle["requiredProfiles"],
        f"{path}.lifecycle.requiredProfiles",
        pattern=PROFILE_PATTERN,
    )
    if required_profiles != sorted(required_profiles):
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: must be sorted"
        )

    raw_profiles = _object(value["profiles"], f"{path}.profiles")
    if not raw_profiles:
        raise ContractError(f"{path}.profiles: must define at least one profile")
    profiles: dict[str, tuple[Check, ...]] = {}
    for profile_name, raw_checks in raw_profiles.items():
        if PROFILE_PATTERN.fullmatch(profile_name) is None:
            raise ContractError(
                f"{path}.profiles.{profile_name}: invalid profile name"
            )
        if not isinstance(raw_checks, list) or not raw_checks:
            raise ContractError(
                f"{path}.profiles.{profile_name}: must contain at least one check"
            )
        checks: list[Check] = []
        identifiers: set[str] = set()
        for index, raw_check in enumerate(raw_checks):
            check_path = f"{path}.profiles.{profile_name}[{index}]"
            check = _object(raw_check, check_path)
            _exact_keys(
                check,
                required={"id", "run", "timeoutSeconds"},
                optional={"workingDirectory"},
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
            argv = _string_list(check["run"], f"{check_path}.run")
            timeout = _timeout(check["timeoutSeconds"], f"{check_path}.timeoutSeconds")
            working_directory = _working_directory(
                check.get("workingDirectory", "."),
                f"{check_path}.workingDirectory",
            )
            checks.append(
                Check(
                    identifier=check_id,
                    run=tuple(argv),
                    timeout_seconds=timeout,
                    working_directory=working_directory,
                )
            )
        profiles[profile_name] = tuple(checks)
    missing_required = sorted(set(required_profiles) - set(profiles))
    if missing_required:
        raise ContractError(
            f"{path}.lifecycle.requiredProfiles: undefined profiles: "
            f"{', '.join(missing_required)}"
        )
    environment = _validate_environment(
        value["environment"], f"{path}.environment"
    )
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


def validate_change(document: Any, path: str = "change") -> None:
    value = _object(document, path)
    _exact_keys(
        value,
        required={
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
        },
        optional={"$schema"},
        path=path,
    )
    if value.get("schemaVersion") != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
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
    _string_list(
        value["affectedProjects"],
        f"{path}.affectedProjects",
        pattern=NAME_PATTERN,
    )
    _string_list(
        value["requiredProfiles"],
        f"{path}.requiredProfiles",
        pattern=PROFILE_PATTERN,
    )

    criteria = value["acceptanceCriteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ContractError(f"{path}.acceptanceCriteria: must not be empty")
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
    _schema_version(value, path)
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
        _string_list(item["affectedPaths"], f"{item_path}.affectedPaths")
        _string_list(
            item["verificationProfiles"],
            f"{item_path}.verificationProfiles",
            pattern=PROFILE_PATTERN,
        )

    acceptance_plan = value["acceptancePlan"]
    if not isinstance(acceptance_plan, list) or not acceptance_plan:
        raise ContractError(f"{path}.acceptancePlan: must not be empty")
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
            mapping["workItems"], f"{mapping_path}.workItems", pattern=PROFILE_PATTERN
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
        )

    risks = value["risks"]
    if not isinstance(risks, list):
        raise ContractError(f"{path}.risks: must be an array")
    for index, raw_risk in enumerate(risks):
        risk_path = f"{path}.risks[{index}]"
        risk = _object(raw_risk, risk_path)
        _exact_keys(risk, required={"risk", "mitigation"}, path=risk_path)
        _string(risk["risk"], f"{risk_path}.risk", max_length=1000)
        _string(risk["mitigation"], f"{risk_path}.mitigation", max_length=1000)

    decisions = value["openDecisions"]
    if not isinstance(decisions, list):
        raise ContractError(f"{path}.openDecisions: must be an array")
    if decisions:
        _string_list(decisions, f"{path}.openDecisions")


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
) -> None:
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
            "reviewer",
            "independence",
            "verdict",
            "findings",
        },
        optional={"$schema"},
        path=path,
    )
    if value.get("schemaVersion") != 1:
        raise ContractError(f"{path}.schemaVersion: must be 1")
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
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ContractError(f"{path}.findings: must be an array")
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
    ):
        raise ContractError(
            f"{path}: approved review cannot contain open or deferred findings"
        )
    if verdict == "changes-requested" and not unresolved_findings:
        raise ContractError(
            f"{path}: changes-requested review must contain an open or deferred finding"
        )


def validate_review(document: Any, path: str = "review") -> None:
    _validate_review(document, path)
