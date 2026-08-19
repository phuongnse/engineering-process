from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
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


OUTPUT_LIMIT = 16_384


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


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait()


def _stop_remaining_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        pass


def _drain_output(stream, capture: dict[str, Any]) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = OUTPUT_LIMIT - len(capture["data"])
            if remaining > 0:
                capture["data"].extend(chunk[:remaining])
            if len(chunk) > remaining:
                capture["truncated"] = True
    except (OSError, ValueError):
        capture["truncated"] = True
    finally:
        stream.close()


def _execute(
    root: Path,
    *,
    identifier: str,
    run: tuple[str, ...],
    timeout_seconds: int,
    working_directory: str,
) -> dict[str, Any]:
    working = _contained_working_directory(root, working_directory)
    started = _timestamp()
    monotonic_start = time.monotonic()
    command_digest = hashlib.sha256(
        json.dumps(run, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            run,
            cwd=working,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
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
        }
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
    stderr_capture: dict[str, Any] = {"data": bytearray(), "truncated": False}
    drain_threads = (
        threading.Thread(
            target=_drain_output,
            args=(process.stdout, stdout_capture),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_output,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for thread in drain_threads:
        thread.start()
    status = "passed"
    error_message: str | None = None
    try:
        exit_code = process.wait(timeout=timeout_seconds)
        if exit_code != 0:
            status = "failed"
    except subprocess.TimeoutExpired:
        status = "timed-out"
        error_message = f"exceeded {timeout_seconds} seconds"
        _stop_process(process)
        exit_code = process.returncode
    except KeyboardInterrupt:
        _stop_process(process)
        raise
    finally:
        for thread in drain_threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in drain_threads):
            status = "failed"
            error_message = "command left background processes holding output streams"
            _stop_remaining_process_group(process)
            process.stdout.close()
            process.stderr.close()
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


def _probe_requirement(root: Path, requirement: EnvironmentRequirement) -> dict[str, Any]:
    probe = requirement.probe
    execution = _execute(
        root,
        identifier=requirement.identifier,
        run=probe.run,
        timeout_seconds=probe.timeout_seconds,
        working_directory=probe.working_directory,
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
    results = [
        _probe_requirement(root, environment.requirements[identifier])
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
    required_approvals = sorted(
        {scope for action in actions for scope in action.mutations}
    )
    unapproved = sorted(set(required_approvals) - allowed_mutations) if apply else []
    if unapproved:
        blocked.append("unapproved mutation scopes: " + ", ".join(unapproved))
    planned_actions = [
        {
            "id": action.identifier,
            "status": "planned",
            "command": list(action.run),
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
        result = _execute(
            root,
            identifier=action.identifier,
            run=action.run,
            timeout_seconds=action.timeout_seconds,
            working_directory=action.working_directory,
        )
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
