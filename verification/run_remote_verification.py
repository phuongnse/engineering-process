from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (  # noqa: E402
    ContractError,
    MAX_JSON_BYTES,
    canonical_json_digest,
    read_json,
    validate_remote_verification_request,
)
from engineering_process.remote_verification import (  # noqa: E402
    MAX_REMOTE_BUNDLE_FILES,
    _validated_bundle,
    _zip_documents,
    read_remote_evidence_document,
)


API_VERSION = "2026-03-10"
MAX_COMMAND_OUTPUT = 64_000
MAX_ARTIFACTS = 64
MAX_DOWNLOAD_BYTES = 4_000_000
MAX_TOTAL_DOWNLOAD_BYTES = 64_000_000
TAG_PREFIX = "epv"
SAFE_WORKFLOW = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
BOOTSTRAP_CHANGE = "evidence-valid-remote-verification"
BOOTSTRAP_BASE = "842627fe8d6cc4e7cb58112d63a32c2e7df467c3"
BOOTSTRAP_AUTHORIZATION = (
    "sha256:d11be9e012dc98983b53949d2b7a5b191044e393281f46d72566794f46d78eac"
)


class AdapterError(RuntimeError):
    pass


def _bounded(content: bytes) -> str:
    return content[:MAX_COMMAND_OUTPUT].decode("utf-8", errors="replace").strip()


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    timeout: int,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise AdapterError(f"required executable is unavailable: {arguments[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise AdapterError(
            f"bounded command timed out after {timeout}s: {arguments[0]}"
        ) from error
    if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
        raise AdapterError(
            f"command output exceeds {MAX_COMMAND_OUTPUT} bytes: {arguments[0]}"
        )
    if check and result.returncode != 0:
        diagnostic = _bounded(result.stderr) or _bounded(result.stdout)
        raise AdapterError(
            f"command failed with exit {result.returncode}: {arguments[0]}"
            + (f"; {diagnostic}" if diagnostic else "")
        )
    return result


def _run_bounded_download(
    arguments: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                arguments,
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            deadline = time.monotonic() + timeout
            exceeded = False
            while process.poll() is None:
                if (
                    os.fstat(stdout_file.fileno()).st_size > MAX_DOWNLOAD_BYTES
                    or os.fstat(stderr_file.fileno()).st_size > MAX_COMMAND_OUTPUT
                ):
                    exceeded = True
                    process.kill()
                    break
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait(timeout=5)
                    raise AdapterError(
                        f"bounded download timed out after {timeout}s: {arguments[0]}"
                    )
                time.sleep(0.1)
            process.wait(timeout=5)
            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            if exceeded or stdout_size > MAX_DOWNLOAD_BYTES:
                raise AdapterError(
                    f"download exceeds {MAX_DOWNLOAD_BYTES} bytes: {arguments[0]}"
                )
            if stderr_size > MAX_COMMAND_OUTPUT:
                raise AdapterError(
                    f"download diagnostics exceed {MAX_COMMAND_OUTPUT} bytes: {arguments[0]}"
                )
            stdout_file.seek(0)
            stderr_file.seek(0)
            result = subprocess.CompletedProcess(
                arguments,
                process.returncode,
                stdout=stdout_file.read(),
                stderr=stderr_file.read(),
            )
    except FileNotFoundError as error:
        raise AdapterError(f"required executable is unavailable: {arguments[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise AdapterError(f"download process did not terminate: {arguments[0]}") from error
    if result.returncode != 0:
        diagnostic = _bounded(result.stderr) or _bounded(result.stdout)
        raise AdapterError(
            f"download failed with exit {result.returncode}: {arguments[0]}"
            + (f"; {diagnostic}" if diagnostic else "")
        )
    return result


def _json_result(result: subprocess.CompletedProcess[bytes], *, label: str) -> dict[str, Any]:
    try:
        document = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"{label} did not return bounded JSON") from error
    if not isinstance(document, dict):
        raise AdapterError(f"{label} did not return a JSON object")
    return document


def verification_tag(request: dict[str, Any]) -> str:
    validate_remote_verification_request(request)
    request_digest = canonical_json_digest(request).removeprefix("sha256:")
    change_digest = hashlib.sha256(request["changeId"].encode("utf-8")).hexdigest()[:16]
    return (
        f"{TAG_PREFIX}/{change_digest}/c{request['cycle']}/"
        f"{request['comparisonBase'][:16]}/{request['checkpoint']}/{request_digest}"
    )


def _request(
    project_root: Path,
    *,
    processctl: str,
    change_id: str,
    actor: str,
    context: str,
    actor_kind: str,
    candidate_root: Path | None,
) -> tuple[dict[str, Any], Path]:
    command = [
            processctl,
            "change",
            "remote",
            "request",
            "--project-root",
            str(project_root),
            "--change-id",
            change_id,
            "--actor",
            actor,
            "--context",
            context,
            "--actor-kind",
            actor_kind,
        ]
    if candidate_root is not None:
        command.extend(["--candidate-root", str(candidate_root)])
    command.append("--json")
    result = _run(
        command,
        cwd=project_root,
        timeout=60,
    )
    response = _json_result(result, label="remote verification request")
    if response.get("status") != "passed" or response.get("phase") != "implementing":
        raise AdapterError("processctl did not create an implementing remote request")
    relative = response.get("request")
    if not isinstance(relative, str):
        raise AdapterError("processctl omitted the remote request path")
    path = (project_root / relative).resolve(strict=True)
    try:
        path.relative_to(project_root)
    except ValueError as error:
        raise AdapterError("remote request path escapes the project") from error
    request = read_json(path)
    validate_remote_verification_request(request, str(path))
    if response.get("requestSha256") != canonical_json_digest(request):
        raise AdapterError("processctl request digest does not match its artifact")
    return request, path


def _publish_tag(project_root: Path, remote: str, ref: str, checkpoint: str) -> bool:
    existing = _run(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=project_root,
        timeout=30,
    )
    lines = [line for line in existing.stdout.decode("utf-8").splitlines() if line]
    if lines:
        if lines != [f"{checkpoint}\t{ref}"]:
            raise AdapterError("verification tag already exists with another identity")
        return False
    _run(
        ["git", "push", "--porcelain", remote, f"{checkpoint}:{ref}"],
        cwd=project_root,
        timeout=60,
    )
    confirmed = _run(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=project_root,
        timeout=30,
    )
    if confirmed.stdout.decode("utf-8").strip() != f"{checkpoint}\t{ref}":
        raise AdapterError("published verification tag identity did not reconcile")
    return True


def _delete_tag(project_root: Path, remote: str, ref: str) -> None:
    current = _run(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=project_root,
        timeout=30,
    )
    if not current.stdout.strip():
        return
    _run(
        ["git", "push", "--porcelain", remote, f":{ref}"],
        cwd=project_root,
        timeout=60,
    )
    remaining = _run(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=project_root,
        timeout=30,
    )
    if remaining.stdout.strip():
        raise AdapterError("verification tag still exists after cleanup")


def _dispatch(
    project_root: Path,
    *,
    repository: str,
    workflow: str,
    dispatch_ref: str,
    source_ref: str,
    request: dict[str, Any],
    bootstrap_authorization_sha256: str | None = None,
) -> dict[str, Any]:
    workflow_shas = {
        requirement["execution"]["workflowSha"]
        for requirement in request["requirements"]
    }
    if len(workflow_shas) != 1:
        raise AdapterError("request requirements do not share one workflow checkpoint")
    inputs = {
            "remote_source_ref": source_ref,
            "remote_change_id": request["changeId"],
            "remote_checkpoint": request["checkpoint"],
            "remote_comparison_base": request["comparisonBase"],
            "remote_request_sha256": canonical_json_digest(request),
            "remote_workflow_sha": next(iter(workflow_shas)),
            "remote_bootstrap_authorization_sha256": (
                bootstrap_authorization_sha256 or ""
            ),
        }
    if request.get("authorityTransition") is not None:
        inputs["remote_authority_transition"] = json.dumps(
            request["authorityTransition"],
            separators=(",", ":"),
            sort_keys=True,
        )
    payload = {
        "ref": dispatch_ref,
        "inputs": inputs,
    }
    result = _run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            f"repos/{repository}/actions/workflows/{workflow}/dispatches",
            "--input",
            "-",
        ],
        cwd=project_root,
        timeout=60,
        input_bytes=(json.dumps(payload, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    response = _json_result(result, label="workflow dispatch")
    run_id = response.get("workflow_run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise AdapterError(
            "workflow dispatch response lacks workflow_run_id; the required API capability is unavailable"
        )
    return response


def _wait_run(
    project_root: Path,
    *,
    repository: str,
    run_id: int,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        attempts += 1
        result = _run(
            [
                "gh",
                "api",
                "-H",
                f"X-GitHub-Api-Version: {API_VERSION}",
                f"repos/{repository}/actions/runs/{run_id}",
            ],
            cwd=project_root,
            timeout=30,
        )
        last = _json_result(result, label="workflow run")
        if last.get("status") == "completed":
            last["pollAttempts"] = attempts
            return last
        time.sleep(poll_seconds)
    status = last.get("status") if last is not None else "unobserved"
    raise AdapterError(
        f"workflow run {run_id} did not complete within {timeout_seconds}s; status={status}"
    )


def _artifact_manifest(content: bytes, *, artifact_id: int) -> dict[str, Any]:
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise AdapterError(f"artifact {artifact_id} exceeds {MAX_DOWNLOAD_BYTES} bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= MAX_REMOTE_BUNDLE_FILES:
                raise AdapterError(
                    f"artifact {artifact_id} exceeds the archive entry bound"
                )
            manifests = [info for info in infos if info.filename == "manifest.json"]
            if len(manifests) != 1:
                raise AdapterError(f"artifact {artifact_id} lacks manifest.json")
            info = manifests[0]
            mode = info.external_attr >> 16
            if (
                info.is_dir()
                or info.flag_bits & 0x1
                or (mode and stat.S_ISLNK(mode))
                or info.file_size < 1
                or info.file_size > MAX_JSON_BYTES
                or info.compress_size > MAX_DOWNLOAD_BYTES
            ):
                raise AdapterError(
                    f"artifact {artifact_id} manifest exceeds its safe expansion boundary"
                )
            data = archive.read(info)
            if len(data) != info.file_size:
                raise AdapterError(f"artifact {artifact_id} manifest size changed")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise AdapterError(f"artifact {artifact_id} is not a valid evidence ZIP") from error
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"artifact {artifact_id} manifest is invalid JSON") from error
    if not isinstance(manifest, dict):
        raise AdapterError(f"artifact {artifact_id} manifest is not an object")
    return manifest


def _write_json_exclusive(path: Path, document: dict[str, Any]) -> None:
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_bootstrap_evidence(
    request: dict[str, Any],
    evidence_path: Path,
    *,
    repository: str,
    workflow: str,
    source_ref: str,
) -> dict[str, Any]:
    if not source_ref.startswith("refs/tags/"):
        raise AdapterError("bootstrap source ref is not a tag")
    evidence = read_remote_evidence_document(evidence_path)
    request_digest = canonical_json_digest(request)
    if evidence["requestSha256"] != request_digest:
        raise AdapterError("bootstrap evidence request digest mismatch")
    tag = source_ref.removeprefix("refs/tags/")
    expected_workflow_ref = (
        f"{repository}/.github/workflows/{workflow}@refs/tags/{tag}"
    )
    requirements = {
        requirement["id"]: requirement
        for requirement in request["requirements"]
    }
    expected = {
        (requirement["id"], selector["id"])
        for requirement in request["requirements"]
        for selector in requirement["selectors"]
    }
    observed: set[tuple[str, str]] = set()
    artifacts: list[dict[str, Any]] = []
    for item in evidence["artifacts"]:
        identity = (item["requirementId"], item["selectorId"])
        if identity in observed:
            raise AdapterError("bootstrap evidence contains a duplicate selector")
        observed.add(identity)
        archive_path = evidence_path.parent / item["archive"]["path"]
        content = archive_path.read_bytes()
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if (
            len(content) != item["archive"]["bytes"]
            or len(content) != item["service"]["sizeInBytes"]
            or digest != item["archive"]["sha256"]
            or digest != item["service"]["digest"]
        ):
            raise AdapterError("bootstrap archive service identity mismatch")
        documents = _zip_documents(
            content, label=f"bootstrap evidence {identity[0]}/{identity[1]}"
        )
        requirement = copy.deepcopy(requirements.get(identity[0]))
        if not isinstance(requirement, dict):
            raise AdapterError("bootstrap evidence uses an unknown requirement")
        requirement["execution"]["workflowRef"] = expected_workflow_ref
        selector = next(
            (
                selector
                for selector in requirement["selectors"]
                if selector["id"] == identity[1]
            ),
            None,
        )
        if selector is None:
            raise AdapterError("bootstrap evidence uses an unknown selector")
        manifest, reports = _validated_bundle(
            documents,
            request=request,
            requirement=requirement,
            selector=selector,
            service=item["service"],
            label=f"bootstrap evidence {identity[0]}/{identity[1]}",
        )
        if manifest["execution"]["workflowRef"] != expected_workflow_ref:
            raise AdapterError("bootstrap manifest workflow ref mismatch")
        artifacts.append(
            {
                "requirementId": identity[0],
                "selectorId": identity[1],
                "artifactId": item["service"]["artifactId"],
                "archiveSha256": digest,
                "manifestSha256": canonical_json_digest(manifest),
                "reportSha256": {
                    name: canonical_json_digest(report)
                    for name, report in sorted(reports.items())
                },
            }
        )
    if observed != expected:
        raise AdapterError("bootstrap evidence selector coverage mismatch")
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-bootstrap-remote-evidence-audit",
        "status": "passed",
        "changeId": request["changeId"],
        "cycle": request["cycle"],
        "checkpoint": request["checkpoint"],
        "comparisonBase": request["comparisonBase"],
        "workspaceFingerprint": request["workspaceFingerprint"],
        "requestSha256": request_digest,
        "workflowRef": expected_workflow_ref,
        "workflowSha": request["checkpoint"],
        "artifacts": sorted(
            artifacts,
            key=lambda item: (item["requirementId"], item["selectorId"]),
        ),
        "controls": {
            "grantsLifecycleCompletion": False,
            "grantsMerge": False,
            "grantsRelease": False,
            "grantsReview": False,
        },
    }


def _selector_identity(request: dict[str, Any], manifest: dict[str, Any]) -> tuple[str, str]:
    matches: list[tuple[str, str]] = []
    platform_data = manifest.get("platform", {})
    runtime = manifest.get("runtime", {})
    version = runtime.get("pythonVersion")
    for requirement in request["requirements"]:
        for selector in requirement["selectors"]:
            if (
                platform_data.get("runnerOs") == selector["runnerOs"]
                and (
                    "runnerArch" not in selector
                    or platform_data.get("runnerArch") == selector["runnerArch"]
                )
                and runtime.get("implementation") == selector["implementation"]
                and isinstance(version, str)
                and version.startswith(selector["pythonMinor"] + ".")
            ):
                matches.append((requirement["id"], selector["id"]))
    if len(matches) != 1:
        raise AdapterError("remote artifact does not match exactly one request selector")
    return matches[0]


def _download_evidence(
    project_root: Path,
    *,
    repository: str,
    run: dict[str, Any],
    request: dict[str, Any],
    output_root: Path,
) -> Path:
    run_id = run["id"]
    result = _run(
        [
            "gh",
            "api",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
        ],
        cwd=project_root,
        timeout=30,
    )
    response = _json_result(result, label="workflow artifacts")
    raw_artifacts = response.get("artifacts")
    if (
        not isinstance(raw_artifacts, list)
        or len(raw_artifacts) > MAX_ARTIFACTS
        or response.get("total_count") != len(raw_artifacts)
    ):
        raise AdapterError("workflow artifact set is incomplete or exceeds its bound")
    expected_count = sum(
        len(requirement["selectors"]) for requirement in request["requirements"]
    )
    candidates = [
        artifact
        for artifact in raw_artifacts
        if isinstance(artifact, dict)
        and isinstance(artifact.get("name"), str)
        and artifact["name"].startswith("engineering-process-ci-evidence-")
        and artifact.get("expired") is False
    ]
    if len(candidates) != expected_count:
        raise AdapterError("workflow did not publish the exact remote evidence count")
    evidence_artifacts: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    total_download_bytes = 0
    for artifact in candidates:
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
            raise AdapterError("workflow artifact id is invalid")
        download = _run_bounded_download(
            [
                "gh",
                "api",
                "-H",
                f"X-GitHub-Api-Version: {API_VERSION}",
                f"repos/{repository}/actions/artifacts/{artifact_id}/zip",
            ],
            cwd=project_root,
            timeout=120,
        )
        content = download.stdout
        total_download_bytes += len(content)
        if total_download_bytes > MAX_TOTAL_DOWNLOAD_BYTES:
            raise AdapterError(
                f"downloaded artifacts exceed {MAX_TOTAL_DOWNLOAD_BYTES} aggregate bytes"
            )
        manifest = _artifact_manifest(content, artifact_id=artifact_id)
        identity = _selector_identity(request, manifest)
        if identity in identities:
            raise AdapterError("workflow published duplicate selector evidence")
        identities.add(identity)
        archive_name = f"artifact-{artifact_id}.zip"
        archive_path = output_root / archive_name
        with archive_path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        service_digest = artifact.get("digest")
        service_bytes = artifact.get("size_in_bytes")
        if service_digest != digest or service_bytes != len(content):
            raise AdapterError(
                f"artifact {artifact_id} service identity does not match downloaded bytes"
            )
        execution = manifest.get("execution", {})
        evidence_artifacts.append(
            {
                "requirementId": identity[0],
                "selectorId": identity[1],
                "archive": {
                    "path": archive_name,
                    "bytes": len(content),
                    "sha256": digest,
                },
                "service": {
                    "artifactId": str(artifact_id),
                    "name": artifact["name"],
                    "sizeInBytes": service_bytes,
                    "digest": service_digest,
                    "runId": str(run_id),
                    "runAttempt": run["run_attempt"],
                    "runUrl": execution.get("runUrl"),
                },
            }
        )
    evidence_artifacts.sort(
        key=lambda item: (item["requirementId"], item["selectorId"])
    )
    transition = request.get("authorityTransition")
    evidence = {
        "schemaVersion": 2 if transition is not None else 1,
        "kind": "engineering-process-remote-verification-evidence",
        "requestSha256": canonical_json_digest(request),
        "capturedAt": run["updated_at"],
        "artifacts": evidence_artifacts,
        **(
            {"authorityTransition": transition}
            if transition is not None
            else {}
        ),
    }
    evidence_path = output_root / "remote-evidence.json"
    with evidence_path.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return evidence_path


def _ingest(
    project_root: Path,
    *,
    processctl: str,
    change_id: str,
    actor: str,
    context: str,
    actor_kind: str,
    evidence_path: Path,
    candidate_root: Path | None,
) -> dict[str, Any]:
    command = [
            processctl,
            "change",
            "remote",
            "ingest",
            "--project-root",
            str(project_root),
            "--change-id",
            change_id,
            "--evidence",
            str(evidence_path),
            "--actor",
            actor,
            "--context",
            context,
            "--actor-kind",
            actor_kind,
        ]
    if candidate_root is not None:
        command.extend(["--candidate-root", str(candidate_root)])
    command.append("--json")
    result = _run(
        command,
        cwd=project_root,
        timeout=120,
    )
    response = _json_result(result, label="remote evidence ingestion")
    if response.get("status") != "passed":
        raise AdapterError("processctl rejected remote evidence")
    return response


def _write_failure(
    path: Path,
    document: dict[str, Any],
    *,
    temporary_root: Path | None,
) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        raise AdapterError(f"failure output already exists: {path}")
    artifacts_path: Path | None = None
    if temporary_root is not None and temporary_root.exists():
        artifacts_path = path.with_name(f"{path.stem}-artifacts")
        if os.path.lexists(artifacts_path):
            raise AdapterError(f"failure artifact path already exists: {artifacts_path}")
        shutil.copytree(temporary_root, artifacts_path)
    payload = {
        **document,
        "preservedArtifacts": str(artifacts_path) if artifacts_path is not None else None,
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except OSError:
        if artifacts_path is not None and artifacts_path.exists():
            shutil.rmtree(artifacts_path)
        raise
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run_adapter(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve(strict=True)
    failure_output = args.failure_output.resolve()
    try:
        failure_output.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise AdapterError("failure output must stay outside the project checkout")
    if SAFE_WORKFLOW.fullmatch(args.workflow) is None:
        raise AdapterError("workflow path is invalid")
    bootstrap = args.bootstrap_request is not None
    if bootstrap != (args.bootstrap_authorization_sha256 is not None):
        raise AdapterError(
            "bootstrap request and bootstrap authorization must be provided together"
        )
    if bootstrap != (args.evidence_output is not None):
        raise AdapterError(
            "bootstrap request and durable evidence output must be provided together"
        )
    candidate_root = getattr(args, "candidate_root", None)
    evidence_output = (
        args.evidence_output.resolve() if args.evidence_output is not None else None
    )
    if evidence_output is not None:
        try:
            evidence_output.relative_to(project_root)
        except ValueError:
            pass
        else:
            raise AdapterError("bootstrap evidence output must stay outside the checkout")
    request: dict[str, Any] | None = None
    source_ref: str | None = None
    tag_present = False
    run: dict[str, Any] | None = None
    temporary_path: Path | None = None
    outcome: dict[str, Any] | None = None
    failure: Exception | None = None
    cleanup_error: Exception | None = None
    with tempfile.TemporaryDirectory(prefix="engineering-process-remote-") as directory:
        temporary_path = Path(directory)
        try:
            if bootstrap:
                request_path = args.bootstrap_request.resolve(strict=True)
                if request_path.is_symlink() or not request_path.is_file():
                    raise AdapterError(
                        "bootstrap request must be a regular non-symlink file"
                    )
                request = read_json(request_path)
                validate_remote_verification_request(request, str(request_path))
                workflow_shas = {
                    item["execution"]["workflowSha"]
                    for item in request["requirements"]
                }
                if not (
                    request["changeId"] == BOOTSTRAP_CHANGE == args.change_id
                    and request["comparisonBase"] == BOOTSTRAP_BASE
                    and workflow_shas == {request["checkpoint"]}
                    and args.bootstrap_authorization_sha256
                    == BOOTSTRAP_AUTHORIZATION
                ):
                    raise AdapterError(
                        "bootstrap request is outside the one-time owner authorization"
                    )
            else:
                request, _ = _request(
                    project_root,
                    processctl=args.processctl,
                    change_id=args.change_id,
                    actor=args.actor,
                    context=args.context,
                    actor_kind=args.actor_kind,
                    candidate_root=candidate_root,
                )
            executions = {json.dumps(item["execution"], sort_keys=True) for item in request["requirements"]}
            if len(executions) != 1:
                raise AdapterError("adapter requires one exact workflow authority per request")
            execution = request["requirements"][0]["execution"]
            if execution["repository"] != args.repository:
                raise AdapterError("request repository does not match adapter target")
            tag = verification_tag(request)
            source_ref = f"refs/tags/{tag}"
            tag_present = True
            _publish_tag(
                project_root, args.remote, source_ref, request["checkpoint"]
            )
            dispatch = _dispatch(
                project_root,
                repository=args.repository,
                workflow=args.workflow,
                dispatch_ref=(tag if bootstrap else args.dispatch_ref),
                source_ref=source_ref,
                request=request,
                bootstrap_authorization_sha256=args.bootstrap_authorization_sha256,
            )
            run = _wait_run(
                project_root,
                repository=args.repository,
                run_id=dispatch["workflow_run_id"],
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
            evidence_path = _download_evidence(
                project_root,
                repository=args.repository,
                run=run,
                request=request,
                output_root=temporary_path,
            )
            if run.get("conclusion") != "success":
                raise AdapterError(
                    f"remote verification run concluded {run.get('conclusion')}"
                )
            audit_digest: str | None = None
            if bootstrap:
                audit = _validate_bootstrap_evidence(
                    request,
                    evidence_path,
                    repository=args.repository,
                    workflow=args.workflow,
                    source_ref=source_ref,
                )
                audit["runId"] = str(run["id"])
                audit["runUrl"] = run["html_url"]
                audit["ownerDecisionSha256"] = BOOTSTRAP_AUTHORIZATION
                audit_path = temporary_path / "bootstrap-audit.json"
                _write_json_exclusive(audit_path, audit)
                audit_digest = canonical_json_digest(audit)
                assert evidence_output is not None
                if os.path.lexists(evidence_output):
                    raise AdapterError(
                        f"bootstrap evidence output already exists: {evidence_output}"
                    )
                evidence_output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(temporary_path, evidence_output)
                ingestion = {
                    "artifactCount": sum(
                        len(item["selectors"])
                        for item in request["requirements"]
                    ),
                    "phase": "bootstrap-evidence",
                }
            else:
                ingestion = _ingest(
                    project_root,
                    processctl=args.processctl,
                    change_id=args.change_id,
                    actor=args.actor,
                    context=args.context,
                    actor_kind=args.actor_kind,
                    evidence_path=evidence_path,
                    candidate_root=candidate_root,
                )
            outcome = {
                "schemaVersion": 1,
                "kind": "engineering-process-remote-verification-operation",
                "status": "passed",
                "changeId": args.change_id,
                "requestSha256": canonical_json_digest(request),
                "sourceRef": source_ref,
                "runId": str(run["id"]),
                "runUrl": run["html_url"],
                "artifactCount": ingestion["artifactCount"],
                "lifecyclePhase": ingestion["phase"],
                "evidenceOutput": str(evidence_output) if bootstrap else None,
                "bootstrapAuditSha256": audit_digest,
            }
        except (AdapterError, ContractError, OSError, ValueError) as error:
            failure = error
        except KeyboardInterrupt:
            failure = AdapterError("remote verification was interrupted")
        finally:
            if tag_present and source_ref is not None:
                try:
                    _delete_tag(project_root, args.remote, source_ref)
                    tag_present = False
                except (AdapterError, OSError) as error:
                    cleanup_error = error
            if failure is not None or cleanup_error is not None:
                failure_document = {
                    "schemaVersion": 1,
                    "kind": "engineering-process-remote-verification-failure",
                    "status": "failed",
                    "changeId": args.change_id,
                    "requestSha256": (
                        canonical_json_digest(request) if request is not None else None
                    ),
                    "sourceRef": source_ref,
                    "runId": str(run["id"]) if run is not None else None,
                    "runUrl": run.get("html_url") if run is not None else None,
                    "failureClass": type(failure or cleanup_error).__name__,
                    "failureSha256": f"sha256:{hashlib.sha256(str(failure or cleanup_error).encode('utf-8')).hexdigest()}",
                    "tagCleaned": not tag_present,
                    "cleanupFailureSha256": (
                        f"sha256:{hashlib.sha256(str(cleanup_error).encode('utf-8')).hexdigest()}"
                        if cleanup_error is not None
                        else None
                    ),
                }
                _write_failure(
                    failure_output,
                    failure_document,
                    temporary_root=temporary_path,
                )
    if failure is not None:
        raise AdapterError(str(failure))
    if cleanup_error is not None:
        raise AdapterError(str(cleanup_error))
    if outcome is None:
        raise AdapterError("remote verification ended without a terminal outcome")
    outcome["tagCleaned"] = True
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded exact-checkpoint remote verification and ingest evidence"
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--processctl", default="processctl")
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--actor-kind", choices=("agent", "human"), required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--dispatch-ref", default="main")
    parser.add_argument("--bootstrap-request", type=Path)
    parser.add_argument("--bootstrap-authorization-sha256")
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--failure-output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 60 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 60 and 3600")
    if not 2 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 2 and 60")
    try:
        result = run_adapter(args)
    except AdapterError as error:
        print(f"remote verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
