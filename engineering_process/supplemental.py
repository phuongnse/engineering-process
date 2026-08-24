from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    MAX_JSON_BYTES,
    PROFILE_PATTERN,
    ContractError,
    read_json,
    validate_project,
)
from .environment import require_environment_profile
from .runner import run_profile, source_state


MAX_SUPPLEMENTAL_PROFILES = 16
MAX_SUPPLEMENTAL_MANIFEST_BYTES = 256_000
MAX_SUPPLEMENTAL_REPORT_TOTAL_BYTES = 1_500_000
MAX_SUPPLEMENTAL_BUNDLE_BYTES = 1_750_000
GIT_OID_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
RUN_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,63}$")
OUTPUT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(
    value: str,
    label: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    if any(ord(character) < 0x20 for character in value):
        raise ContractError(f"{label} contains a control character")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _git_oid(value: str, label: str) -> str:
    return _text(value, label, maximum=64, pattern=GIT_OID_PATTERN)


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _impact_summary(report: dict[str, Any]) -> dict[str, Any]:
    impact = report.get("impact")
    if not isinstance(impact, dict):
        raise ContractError("supplemental verification requires schema 2 impact")
    serialized = _json_bytes(impact)
    return {
        "mode": impact.get("mode"),
        "selectedCheckIds": impact.get("selectedCheckIds"),
        "skippedCheckIds": impact.get("skippedCheckIds"),
        "sha256": f"sha256:{hashlib.sha256(serialized).hexdigest()}",
    }


def _check_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    checks = report.get("checks")
    if not isinstance(checks, list) or len(checks) > 256:
        raise ContractError("supplemental verification checks are invalid")
    summaries: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, dict):
            raise ContractError("supplemental verification check is invalid")
        required = (
            "id",
            "status",
            "exitCode",
            "startedAt",
            "durationMs",
            "timeoutSeconds",
            "commandSha256",
            "impactSha256",
            "impactIntegrity",
            "stdoutBytes",
            "stderrBytes",
            "stdoutSha256",
            "stderrSha256",
            "outputTruncated",
            "streamOutputTruncated",
            "diagnostics",
        )
        if any(name not in check for name in required):
            raise ContractError(
                "supplemental verification check lacks bounded provenance"
            )
        summaries.append({name: check[name] for name in required})
    return summaries


def _ready_source(project_root: Path, expected_checkpoint: str) -> dict[str, Any]:
    state = source_state(project_root)
    if state.get("checkpoint") != expected_checkpoint:
        raise ContractError(
            "supplemental verification checkpoint does not match checkout HEAD"
        )
    if state.get("dirty") is not False or state.get("fingerprint") is None:
        raise ContractError(
            "supplemental verification requires a clean fingerprinted checkout"
        )
    return state


def build_supplemental_verification(
    project_root: Path,
    *,
    expected_checkpoint: str,
    comparison_base: str,
    producer_actor: str,
    producer_context: str,
    provider: str,
    repository: str,
    event_name: str,
    workflow_name: str,
    workflow_ref: str,
    workflow_sha: str,
    run_id: str,
    run_attempt: int,
    job: str,
    run_url: str,
    runner_os: str,
    runner_arch: str,
    triggered_by: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    project_root = project_root.resolve(strict=True)
    expected_checkpoint = _git_oid(
        expected_checkpoint, "supplemental verification checkpoint"
    )
    comparison_base = _git_oid(
        comparison_base, "supplemental verification comparison base"
    )
    workflow_sha = _git_oid(
        workflow_sha, "supplemental verification workflow checkpoint"
    )
    producer_actor = _text(
        producer_actor, "supplemental verification actor", maximum=128
    )
    producer_context = _text(
        producer_context, "supplemental verification context", maximum=512
    )
    provider = _text(
        provider,
        "supplemental verification provider",
        maximum=64,
        pattern=SAFE_NAME_PATTERN,
    )
    repository = _text(
        repository,
        "supplemental verification repository",
        maximum=256,
        pattern=REPOSITORY_PATTERN,
    )
    event_name = _text(
        event_name,
        "supplemental verification event",
        maximum=64,
        pattern=SAFE_NAME_PATTERN,
    )
    workflow_name = _text(
        workflow_name, "supplemental verification workflow", maximum=256
    )
    workflow_ref = _text(
        workflow_ref, "supplemental verification workflow ref", maximum=512
    )
    run_id = _text(
        run_id,
        "supplemental verification run id",
        maximum=64,
        pattern=RUN_ID_PATTERN,
    )
    if not isinstance(run_attempt, int) or not 1 <= run_attempt <= 1_000:
        raise ContractError(
            "supplemental verification run attempt must be between 1 and 1000"
        )
    job = _text(
        job,
        "supplemental verification job",
        maximum=128,
        pattern=SAFE_NAME_PATTERN,
    )
    run_url = _text(
        run_url, "supplemental verification run URL", maximum=2_048
    )
    if not run_url.startswith("https://"):
        raise ContractError(
            "supplemental verification run URL must use HTTPS"
        )
    runner_os = _text(
        runner_os, "supplemental verification runner OS", maximum=64
    )
    runner_arch = _text(
        runner_arch, "supplemental verification runner architecture", maximum=64
    )
    triggered_by = _text(
        triggered_by, "supplemental verification trigger actor", maximum=128
    )

    project_path = project_root / ".process" / "project.json"
    project = validate_project(read_json(project_path), str(project_path))
    profiles = project.required_profiles
    if not profiles or len(profiles) > MAX_SUPPLEMENTAL_PROFILES:
        raise ContractError(
            "supplemental verification required profile count must be between "
            f"1 and {MAX_SUPPLEMENTAL_PROFILES}"
        )
    initial = _ready_source(project_root, expected_checkpoint)
    started_at = _timestamp()
    reports: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    report_total = 0
    for profile_name in profiles:
        if PROFILE_PATTERN.fullmatch(profile_name) is None:
            raise ContractError(
                f"supplemental verification profile is invalid: {profile_name}"
            )
        require_environment_profile(
            project_root, project, profile=profile_name
        )
        report = run_profile(
            project_root,
            project,
            profile_name,
            base_ref=comparison_base,
        )
        if report.get("schemaVersion") != 3:
            raise ContractError(
                "supplemental verification requires schema 3 reports"
            )
        if (
            report.get("checkpoint") != expected_checkpoint
            or report.get("workingTreeDirty") is not False
            or report.get("workspaceFingerprint") != initial["fingerprint"]
            or report.get("completedWorkspaceFingerprint")
            != initial["fingerprint"]
            or report.get("sourceChangedDuringVerification") is not False
        ):
            raise ContractError(
                f"supplemental verification profile {profile_name} lost checkpoint binding"
            )
        serialized = _json_bytes(report)
        if len(serialized) > MAX_JSON_BYTES:
            raise ContractError(
                f"supplemental verification report exceeds {MAX_JSON_BYTES} bytes"
            )
        report_total += len(serialized)
        if report_total > MAX_SUPPLEMENTAL_REPORT_TOTAL_BYTES:
            raise ContractError(
                "supplemental verification reports exceed the aggregate byte limit"
            )
        filename = f"{profile_name}.json"
        reports[filename] = report
        entries.append(
            {
                "path": filename,
                "bytes": len(serialized),
                "sha256": f"sha256:{hashlib.sha256(serialized).hexdigest()}",
                "schemaVersion": 3,
                "profile": profile_name,
                "status": report["status"],
                "checkpoint": report["checkpoint"],
                "workspaceFingerprint": report["workspaceFingerprint"],
                "completedWorkspaceFingerprint": report[
                    "completedWorkspaceFingerprint"
                ],
                "impact": _impact_summary(report),
                "checks": _check_summaries(report),
            }
        )

    final = _ready_source(project_root, expected_checkpoint)
    if final["fingerprint"] != initial["fingerprint"]:
        raise ContractError(
            "supplemental verification workspace changed between profiles"
        )
    manifest = {
        "schemaVersion": 2,
        "kind": "engineering-process-supplemental-verification",
        "status": (
            "passed"
            if all(entry["status"] == "passed" for entry in entries)
            else "failed"
        ),
        "checkpoint": expected_checkpoint,
        "comparisonBase": comparison_base,
        "workspaceFingerprint": initial["fingerprint"],
        "startedAt": started_at,
        "completedAt": _timestamp(),
        "producer": {
            "actorId": producer_actor,
            "contextId": producer_context,
            "kind": "automation",
        },
        "execution": {
            "provider": provider,
            "repository": repository,
            "event": event_name,
            "workflow": workflow_name,
            "workflowRef": workflow_ref,
            "workflowSha": workflow_sha,
            "runId": run_id,
            "runAttempt": run_attempt,
            "job": job,
            "runUrl": run_url,
            "triggeredBy": triggered_by,
        },
        "platform": {
            "runnerOs": runner_os,
            "runnerArch": runner_arch,
            "sysPlatform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "runtime": {
            "implementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
            "cacheTag": sys.implementation.cache_tag,
        },
        "reports": entries,
    }
    manifest_bytes = _json_bytes(manifest)
    if len(manifest_bytes) > MAX_SUPPLEMENTAL_MANIFEST_BYTES:
        raise ContractError(
            "supplemental verification manifest exceeds its byte limit"
        )
    if len(manifest_bytes) + report_total > MAX_SUPPLEMENTAL_BUNDLE_BYTES:
        raise ContractError(
            "supplemental verification bundle exceeds its aggregate byte limit"
        )
    return manifest, reports


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_supplemental_bundle(
    project_root: Path,
    output_root: Path,
    manifest: dict[str, Any],
    reports: dict[str, dict[str, Any]],
) -> Path:
    project_root = project_root.resolve(strict=True)
    requested = Path(os.path.abspath(os.fspath(output_root)))
    if OUTPUT_NAME_PATTERN.fullmatch(requested.name) is None:
        raise ContractError("supplemental verification output name is invalid")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as error:
        raise ContractError(
            f"cannot resolve supplemental verification output parent: {error}"
        ) from error
    resolved_output = parent / requested.name
    try:
        resolved_output.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise ContractError(
            "supplemental verification output must be outside the checkout"
        )
    if os.path.lexists(resolved_output):
        raise ContractError(
            "supplemental verification output already exists"
        )

    entries = manifest.get("reports")
    if (
        not isinstance(entries, list)
        or len(entries) != len(reports)
        or len(entries) > MAX_SUPPLEMENTAL_PROFILES
    ):
        raise ContractError("supplemental verification report manifest is invalid")
    serialized_reports: dict[str, bytes] = {}
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("path") not in reports:
            raise ContractError(
                "supplemental verification report manifest is invalid"
            )
        name = entry["path"]
        if name in seen:
            raise ContractError(
                "supplemental verification report manifest contains duplicate paths"
            )
        seen.add(name)
        if Path(name).name != name or not name.endswith(".json"):
            raise ContractError(
                "supplemental verification report path is invalid"
            )
        content = _json_bytes(reports[name])
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if entry.get("bytes") != len(content) or entry.get("sha256") != digest:
            raise ContractError(
                "supplemental verification report bytes do not match manifest"
            )
        serialized_reports[name] = content
    if seen != set(reports):
        raise ContractError("supplemental verification report manifest is invalid")
    manifest_bytes = _json_bytes(manifest)
    report_total = sum(
        len(content) for content in serialized_reports.values()
    )
    total = len(manifest_bytes) + report_total
    if (
        len(manifest_bytes) > MAX_SUPPLEMENTAL_MANIFEST_BYTES
        or report_total > MAX_SUPPLEMENTAL_REPORT_TOTAL_BYTES
        or total > MAX_SUPPLEMENTAL_BUNDLE_BYTES
    ):
        raise ContractError(
            "supplemental verification bundle exceeds its byte limit"
        )

    temporary = Path(
        tempfile.mkdtemp(prefix=".engineering-process-evidence-", dir=parent)
    )
    try:
        temporary.chmod(stat.S_IRWXU)
        _write_exclusive(temporary / "manifest.json", manifest_bytes)
        for name, content in sorted(serialized_reports.items()):
            _write_exclusive(temporary / name, content)
        os.replace(temporary, resolved_output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return resolved_output
