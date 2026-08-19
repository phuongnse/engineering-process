from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    EnvironmentRequirement,
    MUTATION_SCOPES,
    Project,
    ProjectEnvironment,
    SetupAction,
)
from .tooling import (
    install_managed_tool,
    managed_path_entries,
    selected_artifact,
)


OUTPUT_LIMIT = 16_384
TERMINATION_GRACE_SECONDS = 1.0


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _posix_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_posix_process_group(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _posix_process_group_exists(process_group):
            return True
        time.sleep(0.02)
    return not _posix_process_group_exists(process_group)


def _terminate_posix_process_group(process_group: int) -> tuple[bool, bool]:
    if not _posix_process_group_exists(process_group):
        return False, True
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True, True
    except OSError:
        pass
    if _wait_for_posix_process_group(process_group, TERMINATION_GRACE_SECONDS):
        return True, True
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True, True
    except OSError:
        pass
    return True, _wait_for_posix_process_group(
        process_group, TERMINATION_GRACE_SECONDS
    )


def _stop_command_tree(process: subprocess.Popen[bytes]) -> bool:
    """Stop the complete command tree and return whether cleanup was bounded."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return False
        _, stopped = _terminate_posix_process_group(process.pid)
        return stopped

    # Windows commands run inside the kill-on-close Job Object created by
    # engineering_process._windows_job. Terminating the wrapper closes that job
    # handle and the kernel terminates every assigned descendant.
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return False
    return True


def _command_for_platform(run: tuple[str, ...]) -> tuple[str, ...]:
    if os.name != "nt":
        return run
    return (
        sys.executable,
        "-m",
        "engineering_process._windows_job",
        "--",
        *run,
    )


def _command_preflight(
    root: Path,
    *,
    identifier: str,
    run: tuple[str, ...],
    working_directory: str,
    path_entries: tuple[Path, ...] = (),
    planned_commands: set[str] | None = None,
) -> str | None:
    working = _contained_working_directory(root, working_directory)
    executable = run[0]
    executable_path = Path(executable)
    if executable_path.is_absolute() or executable_path.parent != Path("."):
        candidate = (
            executable_path
            if executable_path.is_absolute()
            else working / executable_path
        )
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return f"setup action {identifier}: executable is unavailable: {executable}"
        return None
    search_path = os.pathsep.join(
        [*(str(path) for path in path_entries), os.environ.get("PATH", "")]
    )
    if shutil.which(executable, path=search_path) is None and executable not in (
        planned_commands or set()
    ):
        return f"setup action {identifier}: executable is unavailable: {executable}"
    return None


_OUTPUT_MIRROR_LOCK = threading.Lock()


def _drain_output(stream, capture: dict[str, Any], *, mirror: bool) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = OUTPUT_LIMIT - len(capture["data"])
            if remaining > 0:
                capture["data"].extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture["truncated"] = True
            if mirror:
                with _OUTPUT_MIRROR_LOCK:
                    binary = getattr(sys.stderr, "buffer", None)
                    if binary is not None:
                        binary.write(chunk)
                        binary.flush()
                    else:
                        sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                        sys.stderr.flush()
    except (OSError, ValueError):
        capture["truncated"] = True
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
    stream_output: bool = False,
) -> dict[str, Any]:
    working = _contained_working_directory(root, working_directory)
    started = _timestamp()
    monotonic_start = time.monotonic()
    command_digest = hashlib.sha256(
        json.dumps(run, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    command_environment = os.environ.copy()
    if path_entries:
        command_environment["PATH"] = os.pathsep.join(
            [
                *(str(path) for path in path_entries),
                command_environment.get("PATH", ""),
            ]
        )
    try:
        process = subprocess.Popen(
            _command_for_platform(run),
            cwd=working,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=command_environment,
            start_new_session=os.name == "posix",
            creationflags=creation_flags,
        )
    except OSError as error:
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
            "outputTruncated": False,
            "pathEntries": [str(path) for path in path_entries],
        }
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
    stderr_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
    drain_threads = (
        threading.Thread(
            target=_drain_output,
            args=(process.stdout, stdout_capture),
            kwargs={"mirror": stream_output},
            daemon=True,
        ),
        threading.Thread(
            target=_drain_output,
            args=(process.stderr, stderr_capture),
            kwargs={"mirror": stream_output},
            daemon=True,
        ),
    )
    for thread in drain_threads:
        thread.start()
    status = "passed"
    error_message: str | None = None
    command_tree_bounded = True
    try:
        exit_code = process.wait(timeout=timeout_seconds)
        if exit_code != 0:
            status = "failed"
    except subprocess.TimeoutExpired:
        status = "timed-out"
        error_message = f"exceeded {timeout_seconds} seconds"
        command_tree_bounded = _stop_command_tree(process)
        exit_code = process.returncode
    except KeyboardInterrupt:
        _stop_command_tree(process)
        raise
    finally:
        if process.poll() is not None and os.name == "posix":
            descendants_found, descendants_stopped = _terminate_posix_process_group(
                process.pid
            )
            command_tree_bounded = command_tree_bounded and descendants_stopped
            if descendants_found and status == "passed":
                status = "failed"
                error_message = "command left descendant processes; they were terminated"
        for thread in drain_threads:
            thread.join(timeout=TERMINATION_GRACE_SECONDS)
        if any(thread.is_alive() for thread in drain_threads):
            status = "failed"
            error_message = "command tree retained output streams after bounded termination"
        if not command_tree_bounded:
            status = "failed"
            error_message = "command tree could not be terminated within the bounded grace period"
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
        "outputTruncated": bool(
            stdout_capture["truncated"] or stderr_capture["truncated"]
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
                "environment profile requested, but project schema version 1 has no "
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


def environment_path_entries(
    project: Project, *, profile: str
) -> tuple[Path, ...]:
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
    entries: list[Path] = []
    for identifier in sorted(tool_ids):
        try:
            tool_entries = managed_path_entries(
                [environment.managed_tools[identifier]]
            )
        except ContractError:
            continue
        for entry in tool_entries:
            if entry not in entries:
                entries.append(entry)
    return tuple(entries)


def _probe_requirement(
    root: Path,
    requirement: EnvironmentRequirement,
    *,
    path_entries: tuple[Path, ...],
) -> dict[str, Any]:
    probe = requirement.probe
    execution = execute_command(
        root,
        identifier=requirement.identifier,
        run=probe.run,
        timeout_seconds=probe.timeout_seconds,
        working_directory=probe.working_directory,
        path_entries=path_entries,
    )
    stream = {
        "stdout": execution["stdout"],
        "stderr": execution["stderr"],
        "combined": execution["stdout"] + "\n" + execution["stderr"],
    }[probe.output_stream]
    output_matches = (
        probe.output_regex is None
        or re.search(probe.output_regex, stream, re.MULTILINE) is not None
    )
    satisfied = execution["status"] == "passed" and output_matches
    return {
        **execution,
        "status": "satisfied" if satisfied else "missing",
        "description": requirement.description,
        "outputStream": probe.output_stream,
        "outputRegex": probe.output_regex,
        "outputMatched": output_matches,
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
    results = [
        _probe_requirement(
            root,
            environment.requirements[identifier],
            path_entries=path_entries,
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
            "setup requires a schema-version-2 project environment contract"
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
    for action in actions:
        try:
            if action.kind == "managed-tool":
                assert action.tool is not None
                artifact = selected_artifact(environment.managed_tools[action.tool])
                issue = None
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
                    planned_commands=planned_commands,
                )
        except (ContractError, OSError) as error:
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
                commands = install_managed_tool(
                    tool,
                    timeout_seconds=action.timeout_seconds,
                )
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
                        name: str(path) for name, path in commands.items()
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
                )
        except (ContractError, OSError) as error:
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
    final = doctor_environment(root, project, profile=selected)
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
