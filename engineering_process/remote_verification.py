from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    ContractError,
    MAX_JSON_BYTES,
    MAX_REMOTE_ARCHIVE_BYTES,
    REMOTE_VERIFICATION_REQUEST_CONTROLS,
    Project,
    canonical_json_digest,
    validate_remote_verification_evidence,
    validate_remote_verification_request,
    validate_verification,
)


MAX_REMOTE_EVIDENCE_ARCHIVES = 256
MAX_REMOTE_EVIDENCE_TOTAL_ARCHIVE_BYTES = 64_000_000
MAX_REMOTE_BUNDLE_FILES = 17
MAX_REMOTE_BUNDLE_UNCOMPRESSED_BYTES = 1_750_000


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _stable_bytes(path: Path, *, maximum: int, label: str) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label}: must be a regular non-symlink file")
        if before.st_size > maximum:
            raise ContractError(f"{label}: exceeds {maximum} bytes")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ContractError(f"{label}: changed while opening")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        os.close(descriptor)
        descriptor = -1
        after = path.lstat()
    except ContractError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ContractError(f"{label}: cannot read {path}: {error}") from error
    data = bytes(content)
    if len(data) > maximum:
        raise ContractError(f"{label}: exceeds {maximum} bytes")
    if (
        len(data) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise ContractError(f"{label}: changed while reading")
    return data


def _json_document(content: bytes, *, label: str) -> dict[str, Any]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise ContractError(f"{label}: UTF-8 BOM is not allowed")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label}: invalid UTF-8 JSON: {error}") from error
    if not isinstance(document, dict):
        raise ContractError(f"{label}: must be a JSON object")
    return document


def _requirement_document(requirement: Any, workflow_sha: str) -> dict[str, Any]:
    execution = {
        "provider": requirement.execution.provider,
        "repository": requirement.execution.repository,
        "workflow": requirement.execution.workflow,
        "workflowRef": requirement.execution.workflow_ref,
        "workflowSha": workflow_sha,
    }
    selectors: list[dict[str, str]] = []
    for selector in requirement.selectors:
        document = {
            "id": selector.identifier,
            "runnerOs": selector.runner_os,
            "implementation": selector.implementation,
            "pythonMinor": selector.python_minor,
        }
        if selector.runner_arch is not None:
            document["runnerArch"] = selector.runner_arch
        selectors.append(document)
    return {
        "id": requirement.identifier,
        "profiles": list(requirement.profiles),
        "execution": execution,
        "selectors": selectors,
    }


def build_remote_verification_request(
    project: Project,
    contract: dict[str, Any],
    *,
    cycle: int,
    checkpoint: str,
    comparison_base: str,
    workspace_fingerprint: str,
) -> dict[str, Any]:
    required = contract.get("requiredEvidence", [])
    available = project.remote_verification or {}
    unknown = sorted(set(required) - set(available))
    if unknown:
        raise ContractError(
            "remote verification request has undefined requirements: "
            + ", ".join(unknown)
        )
    if not required:
        raise ContractError("change has no required remote verification evidence")
    request = {
        "schemaVersion": 1,
        "kind": "engineering-process-remote-verification-request",
        "changeId": contract["id"],
        "cycle": cycle,
        "project": project.identifier,
        "checkpoint": checkpoint,
        "comparisonBase": comparison_base,
        "workspaceFingerprint": workspace_fingerprint,
        "createdAt": _timestamp(),
        "requirements": [
            _requirement_document(available[identifier], comparison_base)
            for identifier in required
        ],
        "controls": dict(REMOTE_VERIFICATION_REQUEST_CONTROLS),
    }
    validate_remote_verification_request(request)
    return request


def _zip_documents(content: bytes, *, label: str) -> dict[str, dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as error:
        raise ContractError(f"{label}: invalid ZIP archive: {error}") from error
    documents: dict[str, dict[str, Any]] = {}
    total = 0
    infos = archive.infolist()
    if not 1 <= len(infos) <= MAX_REMOTE_BUNDLE_FILES:
        raise ContractError(
            f"{label}: archive must contain 1 to {MAX_REMOTE_BUNDLE_FILES} files"
        )
    for info in infos:
        name = PurePosixPath(info.filename)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or len(name.parts) != 1
            or name.name != info.filename
            or name.suffix != ".json"
            or info.file_size < 1
            or info.file_size > MAX_JSON_BYTES
        ):
            raise ContractError(f"{label}: unsafe or invalid entry {info.filename!r}")
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise ContractError(f"{label}: symlink entry is not allowed")
        if info.filename in documents:
            raise ContractError(f"{label}: duplicate entry {info.filename}")
        total += info.file_size
        if total > MAX_REMOTE_BUNDLE_UNCOMPRESSED_BYTES:
            raise ContractError(
                f"{label}: uncompressed content exceeds "
                f"{MAX_REMOTE_BUNDLE_UNCOMPRESSED_BYTES} bytes"
            )
        try:
            data = archive.read(info)
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise ContractError(
                f"{label}: cannot read entry {info.filename}: {error}"
            ) from error
        if len(data) != info.file_size:
            raise ContractError(f"{label}: entry size changed while reading")
        documents[info.filename] = _json_document(
            data, label=f"{label}:{info.filename}"
        )
    archive.close()
    return documents


def _exact_keys(value: dict[str, Any], required: set[str], *, label: str) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise ContractError(f"{label}: " + "; ".join(detail))


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    impact = report["impact"]
    impact_bytes = _json_bytes(impact)
    checks: list[dict[str, Any]] = []
    fields = (
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
    for check in report["checks"]:
        checks.append({field: check[field] for field in fields})
    return {
        "schemaVersion": report["schemaVersion"],
        "profile": report["profile"],
        "status": report["status"],
        "checkpoint": report["checkpoint"],
        "workspaceFingerprint": report["workspaceFingerprint"],
        "completedWorkspaceFingerprint": report[
            "completedWorkspaceFingerprint"
        ],
        "impact": {
            "mode": impact["mode"],
            "selectedCheckIds": impact["selectedCheckIds"],
            "skippedCheckIds": impact["skippedCheckIds"],
            "sha256": _sha256(impact_bytes),
        },
        "checks": checks,
    }


def _validated_bundle(
    documents: dict[str, dict[str, Any]],
    *,
    request: dict[str, Any],
    requirement: dict[str, Any],
    selector: dict[str, Any],
    service: dict[str, Any],
    label: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = documents.get("manifest.json")
    if manifest is None:
        raise ContractError(f"{label}: manifest.json is missing")
    expected_manifest_schema = 3 if request.get("schemaVersion") == 2 else 2
    manifest_keys = {
            "schemaVersion",
            "kind",
            "status",
            "checkpoint",
            "comparisonBase",
            "workspaceFingerprint",
            "startedAt",
            "completedAt",
            "producer",
            "execution",
            "platform",
            "runtime",
            "reports",
        }
    if expected_manifest_schema == 3:
        manifest_keys.add("authorityTransition")
    _exact_keys(
        manifest,
        manifest_keys,
        label=f"{label}:manifest",
    )
    if (
        manifest["schemaVersion"] != expected_manifest_schema
        or manifest["kind"]
        != "engineering-process-supplemental-verification"
        or manifest["status"] != "passed"
        or manifest["checkpoint"] != request["checkpoint"]
        or manifest["comparisonBase"] != request["comparisonBase"]
        or manifest["workspaceFingerprint"] != request["workspaceFingerprint"]
    ):
        raise ContractError(f"{label}: manifest source identity or status mismatch")
    if expected_manifest_schema == 3 and (
        manifest.get("authorityTransition") != request.get("authorityTransition")
    ):
        raise ContractError(f"{label}: authority transition binding mismatch")
    execution = manifest["execution"]
    expected_execution = requirement["execution"]
    for source_name, expected_name in (
        ("provider", "provider"),
        ("repository", "repository"),
        ("workflow", "workflow"),
        ("workflowRef", "workflowRef"),
        ("workflowSha", "workflowSha"),
    ):
        if execution.get(source_name) != expected_execution[expected_name]:
            raise ContractError(f"{label}: execution {source_name} mismatch")
    if (
        execution.get("runId") != service["runId"]
        or execution.get("runAttempt") != service["runAttempt"]
        or execution.get("runUrl") != service["runUrl"]
    ):
        raise ContractError(f"{label}: service run identity mismatch")
    platform = manifest["platform"]
    runtime = manifest["runtime"]
    if (
        platform.get("runnerOs") != selector["runnerOs"]
        or (
            "runnerArch" in selector
            and platform.get("runnerArch") != selector["runnerArch"]
        )
        or runtime.get("implementation") != selector["implementation"]
        or not isinstance(runtime.get("pythonVersion"), str)
        or not runtime["pythonVersion"].startswith(selector["pythonMinor"] + ".")
    ):
        raise ContractError(f"{label}: platform/runtime selector mismatch")

    entries = manifest["reports"]
    if not isinstance(entries, list):
        raise ContractError(f"{label}: manifest reports must be an array")
    entry_profiles = [entry.get("profile") for entry in entries if isinstance(entry, dict)]
    if entry_profiles != requirement["profiles"]:
        raise ContractError(f"{label}: manifest profile coverage mismatch")
    expected_names = {"manifest.json"}
    reports: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractError(f"{label}: report entry must be an object")
        report_name = entry.get("path")
        if not isinstance(report_name, str) or PurePosixPath(report_name).name != report_name:
            raise ContractError(f"{label}: invalid report path")
        expected_names.add(report_name)
        report = documents.get(report_name)
        if report is None:
            raise ContractError(f"{label}: report {report_name} is missing")
        validate_verification(report, f"{label}:{report_name}")
        report_bytes = _json_bytes(report)
        expected = {
            "path": report_name,
            "bytes": len(report_bytes),
            "sha256": _sha256(report_bytes),
            **_report_summary(report),
        }
        if entry != expected:
            raise ContractError(f"{label}: report summary mismatch for {report_name}")
        if (
            report["status"] != "passed"
            or report["checkpoint"] != request["checkpoint"]
            or report["workspaceFingerprint"] != request["workspaceFingerprint"]
            or report["completedWorkspaceFingerprint"]
            != request["workspaceFingerprint"]
            or report["workingTreeDirty"] is not False
            or report["sourceChangedDuringVerification"] is not False
            or any(
                check["status"] != "passed"
                or check["streamOutputTruncated"] is not False
                or check["diagnostics"] != {
                    "policy": "forbid-warning-error",
                    "status": "clean",
                    "count": 0,
                    "matches": [],
                    "matchesTruncated": False,
                }
                for check in report["checks"]
            )
        ):
            raise ContractError(f"{label}: report is not clean and passing")
        reports[report_name] = report
    if set(documents) != expected_names:
        raise ContractError(f"{label}: archive contains unreferenced files")
    return manifest, reports


def read_remote_evidence_document(evidence_path: Path) -> dict[str, Any]:
    evidence_bytes = _stable_bytes(
        evidence_path, maximum=MAX_JSON_BYTES, label="remote verification evidence"
    )
    evidence = _json_document(
        evidence_bytes, label="remote verification evidence"
    )
    validate_remote_verification_evidence(evidence, str(evidence_path))
    return evidence


def validate_remote_evidence_set(
    request: dict[str, Any], evidence_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_remote_verification_request(request)
    evidence = read_remote_evidence_document(evidence_path)
    if evidence["requestSha256"] != canonical_json_digest(request):
        raise ContractError("remote verification evidence request digest mismatch")
    if request.get("schemaVersion") == 2 and (
        evidence.get("schemaVersion") != 2
        or evidence.get("authorityTransition") != request.get("authorityTransition")
    ):
        raise ContractError("remote verification authority transition mismatch")
    requirements = {
        requirement["id"]: requirement for requirement in request["requirements"]
    }
    expected = {
        (requirement["id"], selector["id"])
        for requirement in request["requirements"]
        for selector in requirement["selectors"]
    }
    provided = {
        (artifact["requirementId"], artifact["selectorId"])
        for artifact in evidence["artifacts"]
    }
    if provided != expected:
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        raise ContractError(
            "remote verification selector coverage mismatch"
            + (f"; missing: {missing}" if missing else "")
            + (f"; unknown: {extra}" if extra else "")
        )
    parent = evidence_path.resolve(strict=True).parent
    total_archive_bytes = 0
    validated: list[dict[str, Any]] = []
    for artifact in evidence["artifacts"]:
        identity = (artifact["requirementId"], artifact["selectorId"])
        requirement = requirements[identity[0]]
        selector = next(
            item for item in requirement["selectors"] if item["id"] == identity[1]
        )
        archive_path = parent / artifact["archive"]["path"]
        archive_bytes = _stable_bytes(
            archive_path,
            maximum=MAX_REMOTE_ARCHIVE_BYTES,
            label=f"remote verification archive {identity[0]}/{identity[1]}",
        )
        total_archive_bytes += len(archive_bytes)
        if total_archive_bytes > MAX_REMOTE_EVIDENCE_TOTAL_ARCHIVE_BYTES:
            raise ContractError("remote verification archives exceed aggregate bound")
        if (
            len(archive_bytes) != artifact["archive"]["bytes"]
            or _sha256(archive_bytes) != artifact["archive"]["sha256"]
        ):
            raise ContractError(
                f"remote verification archive {identity[0]}/{identity[1]} digest mismatch"
            )
        documents = _zip_documents(
            archive_bytes,
            label=f"remote verification archive {identity[0]}/{identity[1]}",
        )
        manifest, reports = _validated_bundle(
            documents,
            request=request,
            requirement=requirement,
            selector=selector,
            service=artifact["service"],
            label=f"remote verification bundle {identity[0]}/{identity[1]}",
        )
        validated.append(
            {
                "requirementId": identity[0],
                "selectorId": identity[1],
                "archive": artifact["archive"],
                "service": artifact["service"],
                "manifest": manifest,
                "reports": reports,
            }
        )
    if len(validated) > MAX_REMOTE_EVIDENCE_ARCHIVES:
        raise ContractError("remote verification evidence exceeds archive count")
    return evidence, validated
