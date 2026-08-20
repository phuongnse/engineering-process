from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import regex as bounded_regex

from .contracts import (
    ContractError,
    EnvironmentRequirement,
    MUTATION_SCOPES,
    Project,
    ProjectEnvironment,
    SetupAction,
)
from .tooling import (
    ManagedCommandBinding,
    install_managed_tool,
    installed_command_bindings,
    managed_command_bindings,
    managed_tool_preflight,
    managed_path_entries,
    selected_artifact,
)
from .supervision import process_supervisor


OUTPUT_LIMIT = 16_384
MIRRORED_OUTPUT_LIMIT = 1_000_000
COMMAND_OUTPUT_STREAM_LIMIT = 1_000_000
COMMAND_OUTPUT_TOTAL_LIMIT = 1_500_000
OUTPUT_REGEX_TIMEOUT_SECONDS = 0.1
TERMINATION_GRACE_SECONDS = 1.0


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_regex_output(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _contained_working_directory(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    working = (resolved_root / relative).resolve()
    try:
        working.relative_to(resolved_root)
    except ValueError as error:
        raise ContractError(f"working directory escapes the project root: {relative}") from error
    if not working.is_dir():
        raise ContractError(f"working directory does not exist: {relative}")
    return working


def _command_preflight(
    root: Path,
    *,
    identifier: str,
    run: tuple[str, ...],
    working_directory: str,
    path_entries: tuple[Path, ...] = (),
    command_bindings: dict[str, ManagedCommandBinding] | None = None,
    planned_commands: set[str] | None = None,
) -> str | None:
    working = _contained_working_directory(root, working_directory)
    command_environment = os.environ.copy()
    command_environment["PATH"] = os.pathsep.join(
        [*(str(path) for path in path_entries), os.environ.get("PATH", "")]
    )
    effective_run = run
    if binding := (command_bindings or {}).get(run[0]):
        effective_run = (
            str(binding.application),
            *binding.prefix_arguments,
            *run[1:],
        )
    try:
        process_supervisor().resolve_application(
            effective_run[0],
            working_directory=working,
            environment=command_environment,
        )
    except OSError:
        if run[0] not in (planned_commands or set()):
            return f"setup action {identifier}: executable is unavailable: {run[0]}"
    return None


_OUTPUT_MIRROR_LOCK = threading.Lock()


def _drain_output(
    stream,
    capture: dict[str, Any],
    *,
    mirror: bool,
    budget: dict[str, Any],
    abort: threading.Event,
) -> None:
    try:
        while chunk := stream.read(8192):
            capture["sha256"].update(chunk)
            with budget["lock"]:
                capture["bytes"] += len(chunk)
                budget["bytes"] += len(chunk)
                if (
                    capture["bytes"] > COMMAND_OUTPUT_STREAM_LIMIT
                    or budget["bytes"] > COMMAND_OUTPUT_TOTAL_LIMIT
                ):
                    capture["truncated"] = True
                    capture["limitExceeded"] = True
                    budget["error"] = (
                        "command output exceeded the fail-closed byte budget "
                        f"({COMMAND_OUTPUT_STREAM_LIMIT} per stream, "
                        f"{COMMAND_OUTPUT_TOTAL_LIMIT} aggregate)"
                    )
                    abort.set()
                    break
            remaining = OUTPUT_LIMIT - len(capture["data"])
            if remaining > 0:
                capture["data"].extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture["truncated"] = True
            if mirror:
                with _OUTPUT_MIRROR_LOCK:
                    mirrored = capture["mirroredBytes"]
                    mirror_remaining = MIRRORED_OUTPUT_LIMIT - mirrored
                    visible = chunk[:max(0, mirror_remaining)]
                    binary = getattr(sys.stderr, "buffer", None)
                    if binary is not None and visible:
                        binary.write(visible)
                        binary.flush()
                    elif visible:
                        sys.stderr.write(visible.decode("utf-8", errors="replace"))
                        sys.stderr.flush()
                    capture["mirroredBytes"] += len(visible)
                    if len(visible) < len(chunk) and not capture["mirrorTruncated"]:
                        marker = b"\n[engineering-process: streamed output truncated]\n"
                        if binary is not None:
                            binary.write(marker)
                            binary.flush()
                        else:
                            sys.stderr.write(marker.decode("ascii"))
                            sys.stderr.flush()
                        capture["mirrorTruncated"] = True
    except (OSError, ValueError) as error:
        capture["truncated"] = True
        capture["readError"] = str(error)
        with budget["lock"]:
            if budget["error"] is None:
                budget["error"] = f"cannot read command output: {error}"
        abort.set()
    finally:
        stream.close()


def execute_command(
    root: Path,
    *,
    identifier: str,
    run: tuple[str, ...],
    timeout_seconds: int,
    working_directory: str,
    path_entries: tuple[Path, ...] = (),
    command_bindings: dict[str, ManagedCommandBinding] | None = None,
    environment_overrides: Mapping[str, str | None] | None = None,
    stream_output: bool = False,
) -> dict[str, Any]:
    working = _contained_working_directory(root, working_directory)
    started = _timestamp()
    monotonic_start = time.monotonic()
    command_digest = hashlib.sha256(
        json.dumps(run, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    command_environment = os.environ.copy()
    if path_entries:
        command_environment["PATH"] = os.pathsep.join(
            [
                *(str(path) for path in path_entries),
                command_environment.get("PATH", ""),
            ]
        )
    for name, value in (environment_overrides or {}).items():
        if value is None:
            command_environment.pop(name, None)
        else:
            command_environment[name] = value
    try:
        supervisor = process_supervisor()
        effective_run = run
        if binding := (command_bindings or {}).get(run[0]):
            effective_run = (
                str(binding.application),
                *binding.prefix_arguments,
                *run[1:],
            )
        process = supervisor.spawn(
            effective_run,
            working_directory=working,
            environment=command_environment,
        )
    except (OSError, ValueError) as error:
        return {
            "id": identifier,
            "status": "failed-to-start",
            "exitCode": None,
            "startedAt": started,
            "durationMs": round((time.monotonic() - monotonic_start) * 1000),
            "workingDirectory": working_directory,
            "command": list(run),
            "commandSha256": command_digest,
            "error": str(error),
            "stdout": "",
            "stderr": "",
            "stdoutBytes": 0,
            "stderrBytes": 0,
            "stdoutSha256": hashlib.sha256(b"").hexdigest(),
            "stderrSha256": hashlib.sha256(b"").hexdigest(),
            "outputTruncated": False,
            "streamOutputTruncated": False,
            "pathEntries": [str(path) for path in path_entries],
        }
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture: dict[str, Any] = {
        "data": bytearray(),
        "truncated": False,
        "bytes": 0,
        "sha256": hashlib.sha256(),
        "mirroredBytes": 0,
        "mirrorTruncated": False,
    }
    stderr_capture: dict[str, Any] = {
        "data": bytearray(),
        "truncated": False,
        "bytes": 0,
        "sha256": hashlib.sha256(),
        "mirroredBytes": 0,
        "mirrorTruncated": False,
    }
    for capture in (stdout_capture, stderr_capture):
        capture["limitExceeded"] = False
        capture["readError"] = None
    output_budget: dict[str, Any] = {
        "bytes": 0,
        "error": None,
        "lock": threading.Lock(),
    }
    output_abort = threading.Event()
    drain_threads = (
        threading.Thread(
            target=_drain_output,
            args=(process.stdout, stdout_capture),
            kwargs={
                "mirror": stream_output,
                "budget": output_budget,
                "abort": output_abort,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_drain_output,
            args=(process.stderr, stderr_capture),
            kwargs={
                "mirror": stream_output,
                "budget": output_budget,
                "abort": output_abort,
            },
            daemon=True,
        ),
    )
    for thread in drain_threads:
        thread.start()
    status = "passed"
    error_message: str | None = None
    command_tree_bounded = True
    try:
        deadline = monotonic_start + timeout_seconds
        while True:
            if output_abort.is_set():
                status = "failed"
                error_message = output_budget["error"] or "command output capture failed"
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_GRACE_SECONDS
                )
                command_tree_bounded = cleanup.bounded
                if cleanup.error is not None:
                    error_message = cleanup.error
                exit_code = process.returncode
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timed-out"
                error_message = f"exceeded {timeout_seconds} seconds"
                cleanup = supervisor.terminate(
                    process, grace_seconds=TERMINATION_GRACE_SECONDS
                )
                command_tree_bounded = cleanup.bounded
                if cleanup.error is not None:
                    error_message = cleanup.error
                exit_code = process.returncode
                break
            try:
                exit_code = process.wait(timeout=min(remaining, 0.05))
            except subprocess.TimeoutExpired:
                continue
            if exit_code != 0:
                status = "failed"
            break
    except KeyboardInterrupt:
        supervisor.terminate(process, grace_seconds=TERMINATION_GRACE_SECONDS)
        raise
    finally:
        if process.poll() is not None:
            cleanup = supervisor.finalize(
                process, grace_seconds=TERMINATION_GRACE_SECONDS
            )
            command_tree_bounded = command_tree_bounded and cleanup.bounded
            if cleanup.descendants_found and status == "passed":
                status = "failed"
                error_message = "command left descendant processes; they were terminated"
            if cleanup.error is not None:
                error_message = cleanup.error
        for thread in drain_threads:
            thread.join(timeout=TERMINATION_GRACE_SECONDS)
        if any(thread.is_alive() for thread in drain_threads):
            status = "failed"
            error_message = (
                "command process group retained output streams after bounded termination"
            )
        if output_abort.is_set():
            status = "failed"
            error_message = output_budget["error"] or "command output capture failed"
        if not command_tree_bounded:
            status = "failed"
            error_message = (
                "command process group could not be terminated within the bounded grace period"
            )
            for thread in drain_threads:
                thread.join(timeout=1)
    stdout = bytes(stdout_capture["data"]).decode("utf-8", errors="replace")
    stderr = bytes(stderr_capture["data"]).decode("utf-8", errors="replace")
    result: dict[str, Any] = {
        "id": identifier,
        "status": status,
        "exitCode": exit_code,
        "startedAt": started,
        "durationMs": round((time.monotonic() - monotonic_start) * 1000),
        "workingDirectory": working_directory,
        "command": list(run),
        "commandSha256": command_digest,
        "stdout": stdout,
        "stderr": stderr,
        "stdoutBytes": stdout_capture["bytes"],
        "stderrBytes": stderr_capture["bytes"],
        "stdoutSha256": stdout_capture["sha256"].hexdigest(),
        "stderrSha256": stderr_capture["sha256"].hexdigest(),
        "outputTruncated": bool(
            stdout_capture["truncated"] or stderr_capture["truncated"]
        ),
        "streamOutputTruncated": bool(
            stdout_capture["mirrorTruncated"] or stderr_capture["mirrorTruncated"]
        ),
        "pathEntries": [str(path) for path in path_entries],
    }
    if error_message is not None:
        result["error"] = error_message
    return result


def _profile_environment(
    project: Project, profile: str | None
) -> tuple[ProjectEnvironment | None, str | None, tuple[str, ...]]:
    environment = project.environment
    if environment is None:
        if profile is not None:
            raise ContractError(
                "environment profile requested for an internal Project without an "
                "environment contract"
            )
        return None, None, ()
    selected = profile or environment.default_profile
    requirements = environment.profiles.get(selected)
    if requirements is None:
        available = ", ".join(sorted(environment.profiles))
        raise ContractError(
            f"unknown environment profile {selected}; available profiles: {available}"
        )
    return environment, selected, requirements


def _required_action_ids(
    environment: ProjectEnvironment, requirement_ids: tuple[str, ...]
) -> set[str]:
    return {
        action
        for identifier in requirement_ids
        if (action := environment.requirements[identifier].setup_action) is not None
    }


def _action_dependencies(
    environment: ProjectEnvironment, identifier: str
) -> set[str]:
    dependencies: set[str] = set()

    def add(current: str) -> None:
        for dependency in environment.setup_actions[current].requires:
            if dependency not in dependencies:
                dependencies.add(dependency)
                add(dependency)

    add(identifier)
    return dependencies


def _environment_managed_tools(
    project: Project, *, profile: str
) -> tuple[Any, ...]:
    if project.environment is None:
        return ()
    environment, _, identifiers = _profile_environment(project, profile)
    if environment is None:
        return ()
    actions = _action_order(
        environment, _required_action_ids(environment, identifiers)
    )
    tool_ids = {
        action.tool
        for action in actions
        if action.kind == "managed-tool" and action.tool is not None
    }
    return tuple(environment.managed_tools[identifier] for identifier in sorted(tool_ids))


def environment_path_entries(
    project: Project, *, profile: str
) -> tuple[Path, ...]:
    entries: list[Path] = []
    for tool in _environment_managed_tools(project, profile=profile):
        try:
            tool_entries = managed_path_entries([tool])
        except ContractError:
            continue
        for entry in tool_entries:
            if entry not in entries:
                entries.append(entry)
    return tuple(entries)


def environment_command_bindings(
    project: Project, *, profile: str
) -> dict[str, ManagedCommandBinding]:
    return managed_command_bindings(
        _environment_managed_tools(project, profile=profile)
    )


def _probe_requirement(
    root: Path,
    requirement: EnvironmentRequirement,
    *,
    path_entries: tuple[Path, ...],
    command_bindings: dict[str, ManagedCommandBinding],
) -> dict[str, Any]:
    probe = requirement.probe
    deadline = time.monotonic() + probe.timeout_seconds
    execution = execute_command(
        root,
        identifier=requirement.identifier,
        run=probe.run,
        timeout_seconds=probe.timeout_seconds,
        working_directory=probe.working_directory,
        path_entries=path_entries,
        command_bindings=command_bindings,
    )
    stream = {
        "stdout": execution["stdout"],
        "stderr": execution["stderr"],
        "combined": execution["stdout"] + "\n" + execution["stderr"],
    }[probe.output_stream]
    output_matches = probe.output_regex is None
    output_match_error: str | None = None
    if probe.output_regex is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            output_matches = False
            output_match_error = "probe timeout expired before output matching"
        else:
            try:
                output_matches = (
                    bounded_regex.search(
                        probe.output_regex,
                        _canonical_regex_output(stream),
                        bounded_regex.MULTILINE,
                        timeout=min(remaining, OUTPUT_REGEX_TIMEOUT_SECONDS),
                        concurrent=True,
                    )
                    is not None
                )
            except TimeoutError:
                output_matches = False
                output_match_error = "outputRegex exceeded its bounded match timeout"
    satisfied = execution["status"] == "passed" and output_matches
    return {
        **execution,
        "status": "satisfied" if satisfied else "missing",
        "description": requirement.description,
        "outputStream": probe.output_stream,
        "outputRegex": probe.output_regex,
        "outputMatched": output_matches,
        "outputMatchError": output_match_error,
        "remediation": requirement.remediation,
        "setupAction": requirement.setup_action,
    }


def doctor_environment(
    root: Path, project: Project, *, profile: str | None = None
) -> dict[str, Any]:
    environment, selected, identifiers = _profile_environment(project, profile)
    if environment is None:
        return {
            "status": "not-declared",
            "profile": None,
            "requirements": [],
        }
    path_entries = environment_path_entries(project, profile=selected)
    command_bindings = environment_command_bindings(project, profile=selected)
    results = [
        _probe_requirement(
            root,
            environment.requirements[identifier],
            path_entries=path_entries,
            command_bindings=command_bindings,
        )
        for identifier in identifiers
    ]
    return {
        "status": (
            "passed"
            if all(result["status"] == "satisfied" for result in results)
            else "failed"
        ),
        "profile": selected,
        "foregroundOnly": environment.foreground_only,
        "requirements": results,
    }


def require_environment_profile(
    root: Path, project: Project, *, profile: str
) -> dict[str, Any]:
    if project.environment is None:
        return {
            "status": "not-declared",
            "profile": None,
            "requirements": [],
        }
    report = doctor_environment(root, project, profile=profile)
    if report["status"] in {"not-declared", "passed"}:
        return report
    missing = [
        f"{requirement['id']}: {requirement['remediation']}"
        for requirement in report["requirements"]
        if requirement["status"] != "satisfied"
    ]
    raise ContractError(
        f"environment profile {profile} is not ready: " + "; ".join(missing)
    )


def _action_order(
    environment: ProjectEnvironment, action_ids: set[str]
) -> list[SetupAction]:
    ordered: list[SetupAction] = []
    visited: set[str] = set()

    def add(identifier: str) -> None:
        if identifier in visited:
            return
        action = environment.setup_actions[identifier]
        for dependency in action.requires:
            add(dependency)
        visited.add(identifier)
        ordered.append(action)

    for identifier in sorted(action_ids):
        add(identifier)
    return ordered


def setup_environment(
    root: Path,
    project: Project,
    *,
    profile: str | None,
    apply: bool,
    allowed_mutations: set[str],
) -> dict[str, Any]:
    environment, selected, _ = _profile_environment(project, profile)
    if environment is None:
        raise ContractError(
            "setup requires a project environment contract"
        )
    if allowed_mutations and not apply:
        raise ContractError("--allow is valid only together with --apply")
    unknown_mutations = sorted(allowed_mutations - MUTATION_SCOPES)
    if unknown_mutations:
        raise ContractError(
            "unsupported allowed mutation scopes: " + ", ".join(unknown_mutations)
        )
    initial = doctor_environment(root, project, profile=selected)
    missing = [
        result for result in initial["requirements"] if result["status"] != "satisfied"
    ]
    if not missing:
        return {
            "status": "passed",
            "mode": "apply" if apply else "plan",
            "profile": selected,
            "initial": initial,
            "actions": [],
            "requiredApprovals": [],
            "blocked": [],
            "final": initial,
        }

    blocked = [
        f"{result['id']}: {result['remediation']}"
        for result in missing
        if result["setupAction"] is None
    ]
    action_ids = {
        result["setupAction"]
        for result in missing
        if result["setupAction"] is not None
    }
    actions = _action_order(environment, action_ids)
    planned_tool_ids = {
        action.tool
        for action in actions
        if action.kind == "managed-tool" and action.tool is not None
    }
    provided_commands: dict[str, str] = {}
    preflight_issues: list[str] = []
    try:
        installed_paths = managed_path_entries(
            environment.managed_tools[identifier]
            for identifier in sorted(planned_tool_ids)
        )
    except ContractError as error:
        installed_paths = ()
        preflight_issues.append(str(error))
    try:
        installed_bindings = managed_command_bindings(
            environment.managed_tools[identifier]
            for identifier in sorted(planned_tool_ids)
        )
    except ContractError as error:
        installed_bindings = {}
        preflight_issues.append(str(error))
    for action in actions:
        try:
            if action.kind == "managed-tool":
                assert action.tool is not None
                tool = environment.managed_tools[action.tool]
                artifact = selected_artifact(tool)
                issue = None
                install_issue = managed_tool_preflight(tool)
                if install_issue is not None:
                    issue = f"setup action {action.identifier}: {install_issue}"
                for command_name in artifact.commands:
                    previous = provided_commands.get(command_name)
                    if previous is not None and previous != action.identifier:
                        preflight_issues.append(
                            "setup actions provide the same managed command "
                            f"{command_name}: {previous}, {action.identifier}"
                        )
                    provided_commands[command_name] = action.identifier
            else:
                dependencies = _action_dependencies(environment, action.identifier)
                planned_commands = {
                    command_name
                    for dependency in dependencies
                    if environment.setup_actions[dependency].kind == "managed-tool"
                    for command_name in selected_artifact(
                        environment.managed_tools[
                            environment.setup_actions[dependency].tool  # type: ignore[index]
                        ]
                    ).commands
                }
                issue = _command_preflight(
                    root,
                    identifier=action.identifier,
                    run=action.run,
                    working_directory=action.working_directory,
                    path_entries=installed_paths,
                    command_bindings=installed_bindings,
                    planned_commands=planned_commands,
                )
        except Exception as error:
            issue = f"setup action {action.identifier}: {error}"
        if issue is not None:
            preflight_issues.append(issue)
    blocked.extend(preflight_issues)
    required_approvals = sorted(
        {scope for action in actions for scope in action.mutations}
    )
    unapproved = sorted(set(required_approvals) - allowed_mutations) if apply else []
    if unapproved:
        blocked.append("unapproved mutation scopes: " + ", ".join(unapproved))
    planned_actions = [
        {
            "id": action.identifier,
            "kind": action.kind,
            "status": "planned",
            "command": (
                list(action.run)
                if action.kind == "command"
                else [
                    "managed-tool",
                    action.tool,
                    environment.managed_tools[action.tool].version,  # type: ignore[index]
                ]
            ),
            "workingDirectory": action.working_directory,
            "mutations": list(action.mutations),
            "requires": list(action.requires),
        }
        for action in actions
    ]
    if not apply:
        return {
            "status": "blocked" if blocked else "planned",
            "mode": "plan",
            "profile": selected,
            "initial": initial,
            "actions": planned_actions,
            "requiredApprovals": required_approvals,
            "blocked": blocked,
            "final": None,
        }
    if blocked:
        return {
            "status": "blocked",
            "mode": "apply",
            "profile": selected,
            "initial": initial,
            "actions": planned_actions,
            "requiredApprovals": required_approvals,
            "blocked": blocked,
            "final": None,
        }

    executed: list[dict[str, Any]] = []
    for action in actions:
        try:
            if action.kind == "managed-tool":
                assert action.tool is not None
                tool = environment.managed_tools[action.tool]
                artifact = selected_artifact(tool)
                started = _timestamp()
                monotonic_start = time.monotonic()
                logical_command = (
                    "managed-tool",
                    tool.identifier,
                    tool.version,
                    artifact.platform,
                )
                install_managed_tool(
                    tool,
                    timeout_seconds=action.timeout_seconds,
                )
                commands = installed_command_bindings(tool)
                result = {
                    "id": action.identifier,
                    "kind": action.kind,
                    "status": "passed",
                    "exitCode": 0,
                    "startedAt": started,
                    "durationMs": round(
                        (time.monotonic() - monotonic_start) * 1000
                    ),
                    "workingDirectory": ".",
                    "command": list(logical_command),
                    "commandSha256": hashlib.sha256(
                        json.dumps(
                            logical_command,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "installedCommands": {
                        name: {
                            "application": str(binding.application),
                            "prefixArguments": list(binding.prefix_arguments),
                        }
                        for name, binding in commands.items()
                    },
                }
            else:
                result = execute_command(
                    root,
                    identifier=action.identifier,
                    run=action.run,
                    timeout_seconds=action.timeout_seconds,
                    working_directory=action.working_directory,
                    path_entries=managed_path_entries(
                        environment.managed_tools[identifier]
                        for identifier in sorted(planned_tool_ids)
                    ),
                    command_bindings=managed_command_bindings(
                        environment.managed_tools[identifier]
                        for identifier in sorted(planned_tool_ids)
                    ),
                )
        except Exception as error:
            result = {
                "id": action.identifier,
                "kind": action.kind,
                "status": "failed-to-start",
                "command": (
                    list(action.run)
                    if action.kind == "command"
                    else ["managed-tool", action.tool]
                ),
                "workingDirectory": action.working_directory,
                "error": str(error),
                "errorType": type(error).__name__,
            }
        result["mutations"] = list(action.mutations)
        result["requires"] = list(action.requires)
        executed.append(result)
        if result["status"] != "passed":
            return {
                "status": "failed",
                "mode": "apply",
                "profile": selected,
                "initial": initial,
                "actions": executed,
                "requiredApprovals": required_approvals,
                "blocked": [],
                "final": None,
            }
    try:
        final = doctor_environment(root, project, profile=selected)
    except Exception as error:
        final = {
            "status": "failed-to-start",
            "error": str(error),
            "errorType": type(error).__name__,
            "requirements": [],
        }
    return {
        "status": "passed" if final["status"] == "passed" else "failed",
        "mode": "apply",
        "profile": selected,
        "initial": initial,
        "actions": executed,
        "requiredApprovals": required_approvals,
        "blocked": [],
        "final": final,
    }
