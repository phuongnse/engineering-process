from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    ContractError,
    canonical_json_digest,
    DIGEST_PATTERN,
    IMPROVEMENT_OWNER_BOUNDARIES,
    IMPROVEMENT_REUSABLE_CLASSES,
    MAX_JSON_BYTES,
    PROFILE_PATTERN,
    REPOSITORY_PATTERN,
    Project,
    _validate_legacy_review,
    read_json,
    validate_change,
    validate_completion,
    validate_improvement_catalog,
    validate_improvement_disposition,
    validate_plan,
    validate_remote_verification_request,
    validate_review,
    validate_verification,
)
from .environment import require_environment_profile
from .git import run_git
from .lifecycle_routes import INTERNAL_PHASES, lifecycle_next_state
from .runner import run_profile, source_state
from .remote_verification import (
    build_remote_verification_request,
    read_remote_evidence_document,
    validate_remote_evidence_set,
)


PHASES = set(INTERNAL_PHASES)

FINDING_IDENTITY_FIELDS = (
    "id",
    "severity",
    "path",
    "line",
    "summary",
    "evidence",
)
UNRESOLVED_FINDING_STATUSES = {"open", "deferred"}
IMPROVEMENT_CASE_PHASES = {
    "classification-required",
    "input-required",
    "local-resolution-required",
    "producer-change",
    "producer-completed",
    "producer-disposition-required",
    "producer-resolution-required",
    "consumer-reproduction-required",
    "closed",
}
IMPROVEMENT_DISPOSITIONS = {
    "external-recovery",
    "input-required",
    "local-fix",
    "producer-improvement",
    "shared-escalation",
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _digest_file(path: Path) -> str:
    try:
        return _digest_bytes(path.read_bytes())
    except OSError as error:
        raise ContractError(f"{path}: cannot read lifecycle artifact: {error}") from error


def _resolve_commit(project_root: Path, reference: str) -> str:
    result = run_git(
        project_root,
        ["rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"],
        label=f"resolve comparison base {reference}",
        timeout_seconds=10,
        max_stdout_bytes=128,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(
            f"comparison base {reference} does not resolve to a commit"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.decode("ascii").strip()


def _actor(actor_id: str, context_id: str, kind: str) -> dict[str, str]:
    if not actor_id or actor_id != actor_id.strip() or len(actor_id) > 256:
        raise ContractError("actor id must be a non-empty trimmed value up to 256 characters")
    if not context_id or context_id != context_id.strip() or len(context_id) > 256:
        raise ContractError(
            "context id must be a non-empty trimmed value up to 256 characters"
        )
    if kind not in {"agent", "human"}:
        raise ContractError("actor kind must be agent or human")
    return {"actorId": actor_id, "contextId": context_id, "kind": kind}


def _runs_root(project_root: Path) -> Path:
    return project_root / ".process" / "runs"


def lifecycle_environment_issues(project_root: Path) -> list[str]:
    issues: list[str] = []
    try:
        top = run_git(
            project_root,
            ["rev-parse", "--show-toplevel"],
            label="inspect Git lifecycle boundary",
            timeout_seconds=10,
            max_stdout_bytes=4096,
        )
    except ContractError as error:
        return [f"cannot inspect Git lifecycle boundary: {error}"]
    if top.returncode != 0:
        return ["canonical lifecycle requires a Git repository"]
    try:
        repository_root = Path(top.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError:
        return ["Git repository root is not valid UTF-8"]
    if repository_root != project_root.resolve():
        issues.append(
            f"project root {project_root.resolve()} must equal Git root {repository_root}"
        )
    try:
        ignore = run_git(
            project_root,
            ["check-ignore", "-q", ".process/runs/__process_probe__"],
            label="inspect lifecycle evidence ignore rule",
            timeout_seconds=10,
            max_stdout_bytes=128,
        )
    except ContractError as error:
        issues.append(f"cannot inspect lifecycle evidence ignore rule: {error}")
        return issues
    if ignore.returncode != 0:
        issues.append(
            ".process/runs/ must be ignored so lifecycle evidence cannot dirty source"
        )
    return issues


def _run_root(project_root: Path, change_id: str) -> Path:
    if PROFILE_PATTERN.fullmatch(change_id) is None or len(change_id) > 64:
        raise ContractError(f"invalid change id: {change_id}")
    return _runs_root(project_root) / change_id


@contextmanager
def _change_lock(project_root: Path, change_id: str):
    lock_root = _runs_root(project_root) / ".locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    path = lock_root / f"{change_id}.lock"
    handle = path.open("a+b")
    try:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError(
                    f"change {change_id} is being mutated by another process"
                ) from error
        else:
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ContractError(
                    f"change {change_id} is being mutated by another process"
                ) from error
        yield
    finally:
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


def _state_path(project_root: Path, change_id: str) -> Path:
    return _run_root(project_root, change_id) / "state.json"


def _relative(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ContractError(f"{path}: lifecycle artifacts must stay within the project") from error


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(content) > MAX_JSON_BYTES:
        raise ContractError(
            f"{path}: lifecycle artifact exceeds the {MAX_JSON_BYTES} byte limit"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def reserve_review_context(
    project_root: Path,
    state: dict[str, Any],
    reviewer: dict[str, str],
) -> dict[str, Any]:
    context_digest = hashlib.sha256(
        reviewer["contextId"].encode("utf-8")
    ).hexdigest()
    registry = _runs_root(project_root) / ".review-contexts"
    registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        registry.lstat()
    except OSError as error:
        raise ContractError(
            f"{registry}: cannot inspect review context registry: {error}"
        ) from error
    if registry.is_symlink() or not registry.is_dir():
        raise ContractError(
            f"{registry}: review context registry must be a regular directory"
        )
    record = {
        "schemaVersion": 1,
        "contextDigest": f"sha256:{context_digest}",
        "actorId": reviewer["actorId"],
        "kind": reviewer["kind"],
        "changeId": state["changeId"],
        "cycle": state["cycle"],
        "reservedAt": _timestamp(),
    }
    content = (
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(content) > MAX_JSON_BYTES:
        raise ContractError("review context reservation exceeds its byte limit")
    path = registry / f"{context_digest}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ContractError(
            "independent review requires a fresh context id unused by any "
            "review assignment in this project"
        ) from error
    except OSError as error:
        raise ContractError(
            f"{path}: cannot reserve review context: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return record


def review_context_reservation(
    project_root: Path,
    context_id: str,
) -> dict[str, Any]:
    if not context_id or context_id != context_id.strip() or len(context_id) > 256:
        raise ContractError("review context id must be a bounded non-empty value")
    context_digest = hashlib.sha256(context_id.encode("utf-8")).hexdigest()
    path = _runs_root(project_root) / ".review-contexts" / f"{context_digest}.json"
    try:
        before = path.lstat()
    except OSError as error:
        raise ContractError(
            f"{path}: cannot inspect review context reservation: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ContractError("review context reservation must be a regular file")
    document = read_json(path)
    try:
        after = path.lstat()
    except OSError as error:
        raise ContractError(
            f"{path}: cannot recheck review context reservation: {error}"
        ) from error
    if (
        after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError("review context reservation changed while reading")
    if not isinstance(document, dict):
        raise ContractError("review context reservation must be an object")
    expected_keys = {
        "schemaVersion",
        "contextDigest",
        "actorId",
        "kind",
        "changeId",
        "cycle",
        "reservedAt",
    }
    if set(document) != expected_keys:
        raise ContractError("review context reservation has an unexpected contract")
    if document["schemaVersion"] != 1:
        raise ContractError("review context reservation schemaVersion must be 1")
    if document["contextDigest"] != f"sha256:{context_digest}":
        raise ContractError("review context reservation digest does not match context")
    return document


def _copy_document(
    project_root: Path,
    source: Path,
    destination: Path,
) -> dict[str, str]:
    document = read_json(source)
    content = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_bytes(content)
    except OSError as error:
        raise ContractError(f"{destination}: cannot write lifecycle artifact: {error}") from error
    return {
        "path": _relative(project_root, destination),
        "digest": _digest_bytes(content),
    }


def _artifact_path(project_root: Path, artifact: dict[str, str]) -> Path:
    relative = artifact.get("path")
    digest = artifact.get("digest")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise ContractError("lifecycle state contains an invalid artifact reference")
    path = (project_root / relative).resolve()
    _relative(project_root, path)
    if _digest_file(path) != digest:
        raise ContractError(f"{path}: lifecycle artifact digest is stale")
    return path


def _event(
    state: dict[str, Any],
    event: str,
    actor: dict[str, str] | None,
    **details: Any,
) -> None:
    state["revision"] += 1
    record: dict[str, Any] = {
        "revision": state["revision"],
        "event": event,
        "at": _timestamp(),
    }
    if actor is not None:
        record["actor"] = actor
    record.update(details)
    state["history"].append(record)


def _validate_state(state: Any, path: Path) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ContractError(f"{path}: lifecycle state must be an object")
    required = {
        "schemaVersion",
        "changeId",
        "project",
        "phase",
        "cycle",
        "revision",
        "comparisonBase",
        "contract",
        "plan",
        "implementationActors",
        "verification",
        "pendingFindings",
        "reviewAssignment",
        "review",
        "completion",
        "history",
    }
    missing = sorted(required - set(state))
    extra = sorted(
        set(state) - required - {"improvements", "remoteVerification"}
    )
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        if extra:
            detail.append(f"unknown: {', '.join(extra)}")
        raise ContractError(f"{path}: invalid lifecycle state ({'; '.join(detail)})")
    if state["schemaVersion"] != 2:
        raise ContractError(f"{path}.schemaVersion: must be 2")
    if state["phase"] not in PHASES:
        raise ContractError(f"{path}.phase: invalid phase")
    if not isinstance(state["cycle"], int) or state["cycle"] < 1:
        raise ContractError(f"{path}.cycle: must be a positive integer")
    if not isinstance(state["revision"], int) or state["revision"] < 1:
        raise ContractError(f"{path}.revision: must be a positive integer")
    if not isinstance(state["history"], list) or not state["history"]:
        raise ContractError(f"{path}.history: must not be empty")
    if not isinstance(state["pendingFindings"], list):
        raise ContractError(f"{path}.pendingFindings: must be an array")
    remote = state.get("remoteVerification")
    if remote is not None:
        if not isinstance(remote, dict) or set(remote) != {
            "requiredEvidence",
            "request",
            "evidence",
        }:
            raise ContractError(f"{path}.remoteVerification: invalid fields")
        required_evidence = remote["requiredEvidence"]
        if (
            not isinstance(required_evidence, list)
            or not required_evidence
            or required_evidence != sorted(set(required_evidence))
            or any(
                not isinstance(identifier, str)
                or PROFILE_PATTERN.fullmatch(identifier) is None
                for identifier in required_evidence
            )
        ):
            raise ContractError(
                f"{path}.remoteVerification.requiredEvidence: invalid requirements"
            )
        for field in ("request", "evidence"):
            reference = remote[field]
            if reference is None:
                continue
            if (
                not isinstance(reference, dict)
                or set(reference) != {"path", "digest"}
                or not isinstance(reference["path"], str)
                or not isinstance(reference["digest"], str)
                or DIGEST_PATTERN.fullmatch(reference["digest"]) is None
            ):
                raise ContractError(
                    f"{path}.remoteVerification.{field}: invalid artifact reference"
                )
    improvements = state.get("improvements", [])
    if not isinstance(improvements, list) or len(improvements) > 256:
        raise ContractError(f"{path}.improvements: must contain at most 256 cases")
    identifiers: list[str] = []
    for index, case in enumerate(improvements):
        case_path = f"{path}.improvements[{index}]"
        if not isinstance(case, dict):
            raise ContractError(f"{case_path}: must be an object")
        required_case = {
            "id",
            "role",
            "trigger",
            "phase",
            "sourceCycle",
            "findingId",
            "evidence",
            "classification",
            "signal",
            "catalog",
            "disposition",
            "resolution",
            "reproduction",
        }
        if set(case) != required_case:
            raise ContractError(f"{case_path}: invalid fields")
        identifier = case["id"]
        if (
            not isinstance(identifier, str)
            or len(identifier) > 64
            or PROFILE_PATTERN.fullmatch(identifier) is None
        ):
            raise ContractError(f"{case_path}.id: invalid case id")
        identifiers.append(identifier)
        if case["role"] not in {"consumer", "local", "producer"}:
            raise ContractError(f"{case_path}.role: invalid role")
        if case["trigger"] not in {
            "external-integration",
            "repeated-friction",
            "review-finding",
            "verification-failure",
        }:
            raise ContractError(f"{case_path}.trigger: invalid trigger")
        if case["phase"] not in IMPROVEMENT_CASE_PHASES:
            raise ContractError(f"{case_path}.phase: invalid phase")
        if (
            isinstance(case["sourceCycle"], bool)
            or not isinstance(case["sourceCycle"], int)
            or case["sourceCycle"] < 1
        ):
            raise ContractError(f"{case_path}.sourceCycle: invalid cycle")
        if case["findingId"] is not None and (
            not isinstance(case["findingId"], str)
            or PROFILE_PATTERN.fullmatch(case["findingId"]) is None
        ):
            raise ContractError(f"{case_path}.findingId: invalid finding id")
        for field in (
            "evidence",
            "signal",
            "catalog",
            "disposition",
            "resolution",
            "reproduction",
        ):
            reference = case[field]
            if field == "evidence" and reference is None:
                raise ContractError(f"{case_path}.evidence: required")
            if reference is None:
                continue
            if not isinstance(reference, dict) or set(reference) != {"path", "digest"}:
                raise ContractError(f"{case_path}.{field}: invalid artifact reference")
            if (
                not isinstance(reference["path"], str)
                or not isinstance(reference["digest"], str)
                or DIGEST_PATTERN.fullmatch(reference["digest"]) is None
            ):
                raise ContractError(f"{case_path}.{field}: invalid artifact reference")
            relative = reference["path"]
            candidate = PurePosixPath(relative)
            if (
                "\\" in relative
                or candidate.is_absolute()
                or ".." in candidate.parts
                or candidate.as_posix() != relative
            ):
                raise ContractError(
                    f"{case_path}.{field}.path: must be a portable contained path"
                )
        classification = case["classification"]
        if classification is None:
            if case["phase"] != "classification-required":
                raise ContractError(
                    f"{case_path}.classification: required outside classification phase"
                )
            continue
        if not isinstance(classification, dict) or set(classification) != {
            "ownerBoundary",
            "reusableClass",
            "invariantId",
            "disposition",
            "rationaleSha256",
            "target",
        }:
            raise ContractError(f"{case_path}.classification: invalid fields")
        if classification["ownerBoundary"] not in IMPROVEMENT_OWNER_BOUNDARIES:
            raise ContractError(f"{case_path}.classification.ownerBoundary: invalid")
        if classification["reusableClass"] not in IMPROVEMENT_REUSABLE_CLASSES:
            raise ContractError(f"{case_path}.classification.reusableClass: invalid")
        if (
            not isinstance(classification["invariantId"], str)
            or PROFILE_PATTERN.fullmatch(classification["invariantId"]) is None
        ):
            raise ContractError(f"{case_path}.classification.invariantId: invalid")
        if classification["disposition"] not in IMPROVEMENT_DISPOSITIONS:
            raise ContractError(f"{case_path}.classification.disposition: invalid")
        if (
            not isinstance(classification["rationaleSha256"], str)
            or DIGEST_PATTERN.fullmatch(classification["rationaleSha256"]) is None
        ):
            raise ContractError(f"{case_path}.classification.rationaleSha256: invalid")
        target = classification["target"]
        if target is not None:
            if not isinstance(target, dict) or set(target) != {"project", "repository"}:
                raise ContractError(f"{case_path}.classification.target: invalid")
            if (
                not isinstance(target["project"], str)
                or PROFILE_PATTERN.fullmatch(target["project"]) is None
                or not isinstance(target["repository"], str)
                or REPOSITORY_PATTERN.fullmatch(target["repository"]) is None
            ):
                raise ContractError(f"{case_path}.classification.target: invalid")
    if identifiers != sorted(set(identifiers)):
        raise ContractError(f"{path}.improvements: must be sorted by id and unique")
    return state


def _same_finding_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in FINDING_IDENTITY_FIELDS)


def _replay_pending_findings(
    project_root: Path, state: dict[str, Any], path: Path
) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    review_events = 0
    for event in state.get("history", []):
        if not isinstance(event, dict) or event.get("event") != "review-submitted":
            continue
        review_events += 1
        artifact = event.get("report")
        if not isinstance(artifact, dict):
            raise ContractError(
                f"{path}: cannot safely migrate review history without report artifacts"
            )
        report_path = _artifact_path(project_root, artifact)
        report = read_json(report_path)
        _validate_legacy_review(report, str(report_path))
        for finding in report["findings"]:
            identifier = finding["id"]
            previous = pending.get(identifier)
            if previous is not None and not _same_finding_identity(previous, finding):
                raise ContractError(
                    f"{path}: cannot safely migrate finding {identifier} because its "
                    "identity changed"
                )
            if finding["status"] in UNRESOLVED_FINDING_STATUSES:
                pending[identifier] = dict(finding)
            elif previous is not None:
                del pending[identifier]
    existing = state.get("pendingFindings")
    if review_events == 0 and isinstance(existing, list):
        return [dict(finding) for finding in existing]
    return list(pending.values())


def _migrate_state(project_root: Path, state: Any, path: Path) -> dict[str, Any]:
    if not isinstance(state, dict):
        return state
    migrated = dict(state)
    if migrated.get("schemaVersion") == 1:
        migrated["pendingFindings"] = _replay_pending_findings(
            project_root, state, path
        )
        if migrated["pendingFindings"] and migrated.get("phase") in {
            "approved",
            "completed",
        }:
            migrated["phase"] = "changes-requested"
            migrated["completion"] = None
        migrated["schemaVersion"] = 2
    if migrated.get("schemaVersion") == 2 and "improvements" not in migrated:
        migrated["improvements"] = []
    return migrated


def load_state(project_root: Path, change_id: str) -> dict[str, Any]:
    path = _state_path(project_root, change_id)
    state = _migrate_state(project_root, read_json(path), path)
    return _validate_state(state, path)


def _save_state(project_root: Path, state: dict[str, Any]) -> None:
    _validate_state(state, _state_path(project_root, state["changeId"]))
    _write_atomic(_state_path(project_root, state["changeId"]), state)


def _require_phase(state: dict[str, Any], *allowed: str) -> None:
    if state["phase"] not in allowed:
        expected = ", ".join(allowed)
        raise ContractError(
            f"change {state['changeId']} is {state['phase']}; expected {expected}"
        )


def _transition_phase(state: dict[str, Any], result: str) -> None:
    next_state = lifecycle_next_state(state["phase"], result)
    if next_state not in PHASES:
        raise ContractError(
            f"lifecycle result {result!r} leaves the internal change lifecycle"
        )
    state["phase"] = next_state


def _contract(project_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    document = read_json(_artifact_path(project_root, state["contract"]))
    validate_change(document, "registered change")
    return document


def _plan(project_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state["plan"] is None:
        raise ContractError(f"change {state['changeId']} has no registered plan")
    document = read_json(_artifact_path(project_root, state["plan"]))
    validate_plan(document, "registered plan")
    return document


def _improvement_case(
    state: dict[str, Any], case_id: str
) -> dict[str, Any]:
    for case in state["improvements"]:
        if case["id"] == case_id:
            return case
    raise ContractError(
        f"change {state['changeId']} has no improvement case {case_id}"
    )


def _improvement_case_id(
    prefix: str, cycle: int, identity: str
) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{cycle}-{digest}"


def _observe_improvement_case(
    state: dict[str, Any],
    *,
    trigger: str,
    evidence: dict[str, str],
    identity: str,
    finding_id: str | None = None,
) -> dict[str, Any]:
    case_id = _improvement_case_id(
        "review" if trigger == "review-finding" else "verify",
        state["cycle"],
        identity,
    )
    existing = next(
        (case for case in state["improvements"] if case["id"] == case_id),
        None,
    )
    if existing is not None:
        return existing
    case: dict[str, Any] = {
        "id": case_id,
        "role": "local",
        "trigger": trigger,
        "phase": "classification-required",
        "sourceCycle": state["cycle"],
        "findingId": finding_id,
        "evidence": evidence,
        "classification": None,
        "signal": None,
        "catalog": None,
        "disposition": None,
        "resolution": None,
        "reproduction": None,
    }
    state["improvements"].append(case)
    state["improvements"].sort(key=lambda item: item["id"])
    return case


def _improvement_verification_blockers(
    state: dict[str, Any]
) -> list[str]:
    blocked_phases = {
        "classification-required",
        "consumer-reproduction-required",
        "input-required",
        "producer-disposition-required",
        "producer-resolution-required",
    }
    return [
        case["id"]
        for case in state["improvements"]
        if case["phase"] in blocked_phases
    ]


def _resolve_reviewed_improvements(state: dict[str, Any]) -> None:
    for case in state["improvements"]:
        if case["phase"] == "local-resolution-required":
            case["phase"] = "closed"
        elif case["phase"] == "producer-change":
            case["phase"] = "producer-completed"


def _case_artifact_canonical_digest(
    project_root: Path,
    case: dict[str, Any],
    name: str,
) -> str | None:
    artifact = case[name]
    if artifact is None:
        return None
    document = read_json(_artifact_path(project_root, artifact))
    return canonical_json_digest(document)


def _is_self_discovered_producer_case(case: dict[str, Any]) -> bool:
    classification = case["classification"]
    return (
        case["role"] == "local"
        and classification is not None
        and classification["disposition"] == "producer-improvement"
    )


def _case_catalog_canonical_digest(
    project_root: Path,
    case: dict[str, Any],
) -> str | None:
    digest = _case_artifact_canonical_digest(project_root, case, "catalog")
    if digest is not None or not _is_self_discovered_producer_case(case):
        return digest
    catalog_path = project_root / "improvement-catalog.json"
    document = read_json(catalog_path)
    validate_improvement_catalog(document, str(catalog_path))
    return canonical_json_digest(document)


def _require_producer_catalog_activation(
    project_root: Path,
    state: dict[str, Any],
    case: dict[str, Any],
) -> None:
    classification = case["classification"]
    self_discovered = _is_self_discovered_producer_case(case)
    if case["role"] != "producer" and not self_discovered:
        return
    disposition_reference = case["disposition"]
    if case["role"] == "producer" and disposition_reference is None:
        raise ContractError(
            f"producer improvement case {case['id']} lacks a disposition"
        )
    disposition = None
    if disposition_reference is not None:
        disposition = read_json(
            _artifact_path(project_root, disposition_reference)
        )
        validate_improvement_disposition(
            disposition, f"producer improvement case {case['id']} disposition"
        )
    catalog_path = project_root / "improvement-catalog.json"
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ContractError(
            "producer improvement completion requires the reviewed "
            "improvement-catalog.json"
        )
    catalog = read_json(catalog_path)
    validate_improvement_catalog(catalog, str(catalog_path))
    if disposition is not None:
        producer = disposition["producer"]
        if catalog["producer"] != {
            "project": producer["project"],
            "repository": producer["repository"],
        }:
            raise ContractError(
                "reviewed improvement catalog producer does not match the disposition"
            )
        invariant_id = disposition["canonicalInvariantId"]
        reusable_class = disposition["reusableClass"]
        linked_change_id = disposition["linkedChangeId"]
    else:
        if classification is None:
            raise ContractError(
                f"producer improvement case {case['id']} lacks classification"
            )
        if catalog["producer"]["project"] != state["project"]:
            raise ContractError(
                "reviewed improvement catalog producer does not match the "
                "lifecycle project"
            )
        invariant_id = classification["invariantId"]
        reusable_class = classification["reusableClass"]
        linked_change_id = state["changeId"]
    entry = next(
        (
            item
            for item in catalog["entries"]
            if item["id"] == invariant_id
        ),
        None,
    )
    if entry is None:
        raise ContractError(
            "producer improvement completion requires the canonical invariant "
            "in the reviewed catalog"
        )
    if (
        entry["status"] != "active"
        or entry["activeChangeId"] != state["changeId"]
        or linked_change_id != state["changeId"]
    ):
        raise ContractError(
            "producer improvement completion requires reviewed catalog activation "
            "for the selected lifecycle change"
        )
    if entry["reusableClass"] != reusable_class:
        raise ContractError(
            "reviewed catalog reusable class does not match the disposition"
        )


def _improvement_next_owner(
    project: str,
    case: dict[str, Any],
) -> str | None:
    classification = case["classification"]
    return {
        "classification-required": project,
        "consumer-reproduction-required": project,
        "input-required": "owner-input",
        "local-resolution-required": project,
        "producer-change": project,
        "producer-completed": "release-owner",
        "producer-disposition-required": (
            classification["target"]["project"]
            if classification is not None and classification["target"] is not None
            else "producer"
        ),
        "producer-resolution-required": "producer-release-owner",
        "closed": None,
    }[case["phase"]]


def _improvement_case_status(
    project_root: Path,
    state: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    from .improvement import _stable_json_document, validate_improvement_chain

    classification = case["classification"]
    self_discovered = _is_self_discovered_producer_case(case)
    artifact_names = (
        "evidence",
        "signal",
        "catalog",
        "disposition",
        "resolution",
        "reproduction",
    )
    paths: dict[str, Path] = {}
    documents: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, str]] = {}
    for name in artifact_names:
        reference = case[name]
        if reference is None:
            continue
        path = _artifact_path(project_root, reference)
        document, data = _stable_json_document(
            path, label=f"improvement case {case['id']} {name}"
        )
        if _digest_bytes(data) != reference["digest"]:
            raise ContractError(
                f"improvement case {case['id']} {name} changed while status was read"
            )
        paths[name] = path
        documents[name] = document
        artifacts[name] = {
            "sourceSha256": reference["digest"],
            "canonicalSha256": canonical_json_digest(document),
        }
    if self_discovered and "catalog" not in documents:
        catalog_path = project_root / "improvement-catalog.json"
        if catalog_path.is_symlink():
            raise ContractError(
                "self-discovered producer catalog must not be a symlink"
            )
        if catalog_path.is_file():
            document, data = _stable_json_document(
                catalog_path,
                label=f"improvement case {case['id']} producer catalog",
            )
            validate_improvement_catalog(document, str(catalog_path))
            documents["catalog"] = document
            artifacts["catalog"] = {
                "sourceSha256": _digest_bytes(data),
                "canonicalSha256": canonical_json_digest(document),
            }

    chain: dict[str, Any] | None = None
    if "signal" in paths:
        chain = validate_improvement_chain(
            paths["signal"],
            paths.get("disposition"),
            paths.get("resolution"),
            paths.get("reproduction"),
            paths.get("catalog"),
        )
        allowed_case_phases = {
            "consumer": {
                "signal-exported": "producer-disposition-required",
                "producer-disposition": "producer-resolution-required",
                "producer-rejected": "input-required",
                "producer-released": "consumer-reproduction-required",
                "closed": "closed",
            },
            "producer": {
                "producer-disposition": {"producer-change", "producer-completed"},
            },
        }
        expected = allowed_case_phases.get(case["role"], {}).get(chain["phase"])
        phase_matches = (
            case["phase"] in expected
            if isinstance(expected, set)
            else case["phase"] == expected
        )
        if expected is None or not phase_matches:
            raise ContractError(
                f"improvement case {case['id']} phase contradicts its artifact chain"
            )

    signal = documents.get("signal")
    catalog = documents.get("catalog")
    disposition = documents.get("disposition")
    resolution = documents.get("resolution")
    reproduction = documents.get("reproduction")
    proposed_invariant = (
        signal["claim"]["proposedInvariantId"]
        if signal is not None
        else classification["invariantId"]
        if classification is not None
        else None
    )
    canonical_invariant = (
        disposition["canonicalInvariantId"]
        if disposition is not None
        else classification["invariantId"]
        if classification is not None and case["role"] == "local"
        else None
    )

    catalog_entry = None
    if catalog is not None and canonical_invariant is not None:
        catalog_entry = next(
            (
                item
                for item in catalog["entries"]
                if item["id"] == canonical_invariant
            ),
            None,
        )
    producer_target = (
        disposition["producer"]
        if disposition is not None
        else signal["target"]
        if signal is not None
        else catalog["producer"]
        if self_discovered and catalog is not None
        else classification["target"]
        if classification is not None
        else None
    )
    producer = None
    if producer_target is not None:
        producer = {
            "project": producer_target["project"],
            "repository": producer_target["repository"],
            "checkpoint": producer_target.get("checkpoint"),
            "process": producer_target.get("process"),
            "linkedChangeId": (
                disposition["linkedChangeId"]
                if disposition is not None
                else state["changeId"]
                if self_discovered
                else None
            ),
        }
    consumer_source = (
        reproduction["consumer"]
        if reproduction is not None
        else signal["source"]
        if signal is not None
        else None
    )
    consumer = None
    if consumer_source is not None:
        consumer = {
            "project": consumer_source["project"],
            "repository": consumer_source["repository"],
            "checkpoint": consumer_source["checkpoint"],
            "workspaceFingerprint": consumer_source["workspaceFingerprint"],
            "process": consumer_source["process"],
        }
    catalog_status = (
        disposition["catalogStatus"]
        if disposition is not None
        else catalog_entry["status"]
        if catalog_entry is not None
        else "missing"
        if self_discovered
        else None
    )
    recurrence = (
        disposition["recurrence"]
        if disposition is not None
        else "new"
        if self_discovered
        and catalog_entry is not None
        and catalog_entry["lastResolution"] is None
        else "recurrence"
        if self_discovered and catalog_entry is not None
        else "unassessed"
    )
    return {
        "id": case["id"],
        "phase": case["phase"],
        "role": case["role"],
        "trigger": case["trigger"],
        "sourceCycle": case["sourceCycle"],
        "invariantId": (
            canonical_invariant if canonical_invariant is not None else proposed_invariant
        ),
        "proposedInvariantId": proposed_invariant,
        "canonicalInvariantId": canonical_invariant,
        "ownerBoundary": (
            classification["ownerBoundary"] if classification is not None else None
        ),
        "reusableClass": (
            disposition["reusableClass"]
            if disposition is not None
            else classification["reusableClass"]
            if classification is not None
            else None
        ),
        "recurrence": recurrence,
        "catalog": {
            "status": catalog_status,
            "canonicalSha256": (
                artifacts.get("catalog", {}).get("canonicalSha256")
            ),
            "activeChangeId": (
                catalog_entry["activeChangeId"] if catalog_entry is not None else None
            ),
            "lastResolution": (
                catalog_entry["lastResolution"] if catalog_entry is not None else None
            ),
        },
        "producer": producer,
        "release": resolution["release"] if resolution is not None else None,
        "consumer": consumer,
        "artifacts": artifacts,
        "chainPhase": chain["phase"] if chain is not None else None,
        "closed": case["phase"] == "closed",
        "nextOwner": _improvement_next_owner(state["project"], case),
    }


def _start_change_unlocked(
    project_root: Path,
    project: Project,
    contract_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    document = read_json(contract_path)
    validate_change(document, str(contract_path))
    if document["schemaVersion"] != 3:
        raise ContractError(
            f"{contract_path}: new lifecycle runs require change schemaVersion 3; "
            "schemaVersion 2 remains readable only for historical runs"
        )
    change_id = document["id"]
    if project.identifier not in document["affectedProjects"]:
        raise ContractError(
            f"change {change_id} does not include project {project.identifier}"
        )
    missing_profiles = sorted(set(document["requiredProfiles"]) - set(project.profiles))
    if missing_profiles:
        raise ContractError(
            f"change {change_id} requires undefined profiles: {', '.join(missing_profiles)}"
        )
    missing_baseline = sorted(
        set(project.required_profiles) - set(document["requiredProfiles"])
    )
    if missing_baseline:
        raise ContractError(
            f"change {change_id} omits project lifecycle profiles: "
            f"{', '.join(missing_baseline)}"
        )
    required_evidence = document.get("requiredEvidence", [])
    available_remote = project.remote_verification or {}
    unknown_evidence = sorted(set(required_evidence) - set(available_remote))
    if unknown_evidence:
        raise ContractError(
            f"change {change_id} requires undefined remote evidence: "
            + ", ".join(unknown_evidence)
        )
    remote_profile_gaps = sorted(
        {
            profile
            for requirement_id in required_evidence
            for profile in available_remote[requirement_id].profiles
            if profile not in document["requiredProfiles"]
        }
    )
    if remote_profile_gaps:
        raise ContractError(
            f"change {change_id} remote evidence uses omitted profiles: "
            + ", ".join(remote_profile_gaps)
        )
    assessed_dimensions = {
        assessment["dimension"] for assessment in document["quality"]["assessments"]
    }
    missing_quality_extensions = sorted(
        set(project.quality_extensions) - assessed_dimensions
    )
    if missing_quality_extensions:
        raise ContractError(
            f"change {change_id} omits project quality dimensions: "
            f"{', '.join(missing_quality_extensions)}"
        )
    run_root = _run_root(project_root, change_id)
    if run_root.exists():
        raise ContractError(f"change {change_id} already exists")
    actor = _actor(actor_id, context_id, kind)
    comparison_base = _resolve_commit(project_root, document["comparisonBase"])
    contract = _copy_document(project_root, contract_path, run_root / "contract.json")
    now = _timestamp()
    state: dict[str, Any] = {
        "schemaVersion": 2,
        "changeId": change_id,
        "project": project.identifier,
        "phase": lifecycle_next_state("unregistered", "success"),
        "cycle": 1,
        "revision": 1,
        "comparisonBase": comparison_base,
        "contract": contract,
        "plan": None,
        "implementationActors": [],
        "verification": [],
        "remoteVerification": (
            {
                "requiredEvidence": list(required_evidence),
                "request": None,
                "evidence": None,
            }
            if required_evidence
            else None
        ),
        "pendingFindings": [],
        "reviewAssignment": None,
        "review": None,
        "completion": None,
        "improvements": [],
        "history": [
            {
                "revision": 1,
                "event": "specified",
                "at": now,
                "actor": actor,
                "comparisonBaseRef": document["comparisonBase"],
                "comparisonBase": comparison_base,
            }
        ],
    }
    _save_state(project_root, state)
    return state


def _register_plan_unlocked(
    project_root: Path,
    project: Project,
    change_id: str,
    plan_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "specified")
    contract = _contract(project_root, state)
    if contract["signOff"]["required"] and contract["signOff"]["status"] != "approved":
        raise ContractError(f"change {change_id} requires sign-off before planning")
    document = read_json(plan_path)
    validate_plan(document, str(plan_path))
    if contract["schemaVersion"] == 3 and document["schemaVersion"] != 2:
        raise ContractError("new schema-3 changes require a bounded schema-2 plan")
    if document["changeId"] != change_id:
        raise ContractError(f"plan changeId does not match {change_id}")
    if document["contractDigest"] != state["contract"]["digest"]:
        raise ContractError("plan contractDigest does not match the registered contract")
    if document["openDecisions"]:
        raise ContractError("implementation plan has unresolved open decisions")
    contract_criteria = {item["id"] for item in contract["acceptanceCriteria"]}
    planned_criteria = {item["criterionId"] for item in document["acceptancePlan"]}
    if contract_criteria != planned_criteria:
        missing = sorted(contract_criteria - planned_criteria)
        extra = sorted(planned_criteria - contract_criteria)
        raise ContractError(
            "acceptance plan must map the exact contract criteria"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unknown: {', '.join(extra)}" if extra else "")
        )
    used_profiles = {
        profile
        for item in document["workItems"]
        for profile in item["verificationProfiles"]
    } | {
        profile
        for item in document["acceptancePlan"]
        for profile in item["verificationProfiles"]
    }
    unknown_profiles = sorted(used_profiles - set(project.profiles))
    if unknown_profiles:
        raise ContractError(
            f"plan references undefined profiles: {', '.join(unknown_profiles)}"
        )
    missing_required = sorted(set(contract["requiredProfiles"]) - used_profiles)
    if missing_required:
        raise ContractError(
            f"plan does not use required profiles: {', '.join(missing_required)}"
        )
    actor = _actor(actor_id, context_id, kind)
    artifact = _copy_document(
        project_root, plan_path, _run_root(project_root, change_id) / "plan.json"
    )
    state["plan"] = artifact
    _transition_phase(state, "success")
    _event(state, "planned", actor)
    _save_state(project_root, state)
    return state


def _begin_implementation_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _contract(project_root, state)
    _plan(project_root, state)
    classification_required = [
        case["id"]
        for case in state["improvements"]
        if case["phase"] == "classification-required"
    ]
    if classification_required:
        raise ContractError(
            "implementation cannot continue before improvement classification: "
            + ", ".join(classification_required)
        )
    actor = _actor(actor_id, context_id, kind)
    starting_phase = state["phase"]
    if starting_phase == "verified":
        source = source_state(project_root)
        checkpoints = {item["checkpoint"] for item in state["verification"]}
        fingerprints = {
            item["workspaceFingerprint"] for item in state["verification"]
        }
        current = (
            source["dirty"] is False
            and source["checkpoint"] is not None
            and source["fingerprint"] is not None
            and checkpoints == {source["checkpoint"]}
            and fingerprints == {source["fingerprint"]}
        )
        if current:
            raise ContractError(
                "current verified change must enter independent review before "
                "another implementation cycle"
            )
        previous_cycle = state["cycle"]
        previous_verification = list(state["verification"])
        state["cycle"] += 1
        state["implementationActors"] = []
        state["verification"] = []
        if state.get("remoteVerification") is not None:
            state["remoteVerification"]["request"] = None
            state["remoteVerification"]["evidence"] = None
        state["reviewAssignment"] = None
        state["review"] = None
        _event(
            state,
            "verification-invalidated",
            actor,
            cycle=state["cycle"],
            previousCycle=previous_cycle,
            previousVerification=previous_verification,
            reason="source-changed-after-verification",
        )
    else:
        _require_phase(state, "planned", "implementing", "changes-requested")
    if starting_phase == "changes-requested":
        state["cycle"] += 1
        state["implementationActors"] = []
        state["verification"] = []
        if state.get("remoteVerification") is not None:
            state["remoteVerification"]["request"] = None
            state["remoteVerification"]["evidence"] = None
        state["reviewAssignment"] = None
        state["review"] = None
    if actor not in state["implementationActors"]:
        state["implementationActors"].append(actor)
    transition_result = {
        "changes-requested": "success",
        "implementing": "implementation-continued",
        "planned": "success",
        "verified": "source-changed",
    }[starting_phase]
    _transition_phase(state, transition_result)
    _event(state, "implementation-started", actor, cycle=state["cycle"])
    _save_state(project_root, state)
    return state


def _verification_eligibility_issues(report: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if report["status"] != "passed":
        issues.append("profile-status-not-passed")
    if report["checkpoint"] is None:
        issues.append("checkpoint-missing")
    if report["workingTreeDirty"] is not False:
        issues.append("working-tree-dirty")
    if report["workspaceFingerprint"] is None:
        issues.append("workspace-fingerprint-missing")
    if report["sourceChangedDuringVerification"]:
        issues.append("source-changed-during-verification")
    if report["workspaceFingerprint"] != report["completedWorkspaceFingerprint"]:
        issues.append("workspace-fingerprint-changed-during-verification")
    return issues


def _required_profiles_current(
    state: dict[str, Any], contract: dict[str, Any]
) -> bool:
    required = set(contract["requiredProfiles"])
    if {item["profile"] for item in state["verification"]} != required:
        return False
    checkpoints = {item["checkpoint"] for item in state["verification"]}
    fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
    return len(checkpoints) == 1 and len(fingerprints) == 1


def _remote_evidence_ready(
    project_root: Path,
    state: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
) -> bool:
    remote = state.get("remoteVerification")
    if remote is None:
        return True
    request_reference = remote.get("request")
    evidence_reference = remote.get("evidence")
    if not isinstance(request_reference, dict) or not isinstance(
        evidence_reference, dict
    ):
        return False
    request = read_json(_artifact_path(project_root, request_reference))
    validate_remote_verification_request(request, "registered remote request")
    evidence = read_json(_artifact_path(project_root, evidence_reference))
    required_keys = {
        "schemaVersion",
        "kind",
        "requestSha256",
        "checkpoint",
        "comparisonBase",
        "workspaceFingerprint",
        "sourceEvidence",
        "artifacts",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != required_keys
        or evidence["schemaVersion"] != 1
        or evidence["kind"]
        != "engineering-process-ingested-remote-verification"
        or evidence["requestSha256"] != canonical_json_digest(request)
        or evidence["checkpoint"] != request["checkpoint"]
        or evidence["comparisonBase"] != request["comparisonBase"]
        or evidence["workspaceFingerprint"] != request["workspaceFingerprint"]
    ):
        raise ContractError("registered remote verification evidence is stale")
    _artifact_path(project_root, evidence["sourceEvidence"])
    expected = {
        (requirement["id"], selector["id"])
        for requirement in request["requirements"]
        for selector in requirement["selectors"]
    }
    artifacts = evidence["artifacts"]
    if not isinstance(artifacts, list):
        raise ContractError("registered remote verification artifacts are invalid")
    provided: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "requirementId",
            "selectorId",
            "archive",
            "service",
            "manifest",
            "verification",
        }:
            raise ContractError("registered remote verification artifact is invalid")
        provided.add((artifact["requirementId"], artifact["selectorId"]))
        _artifact_path(project_root, artifact["manifest"])
        verification = artifact["verification"]
        if not isinstance(verification, list):
            raise ContractError(
                "registered remote verification report references are invalid"
            )
        for reference in verification:
            report = read_json(_artifact_path(project_root, reference))
            validate_verification(report, "registered remote verification report")
            if (
                report.get("status") != "passed"
                or report.get("checkpoint") != request["checkpoint"]
                or report.get("workspaceFingerprint")
                != request["workspaceFingerprint"]
            ):
                raise ContractError(
                    "registered remote verification report is stale or failed"
                )
    if provided != expected:
        raise ContractError("registered remote verification coverage is incomplete")
    if source is None:
        source = source_state(project_root)
    if (
        source.get("dirty") is not False
        or source.get("checkpoint") != request["checkpoint"]
        or source.get("fingerprint") != request["workspaceFingerprint"]
    ):
        raise ContractError("registered remote verification source is stale")
    return True


def _request_remote_verification_unlocked(
    project_root: Path,
    project: Project,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "implementing")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    actor = _actor(actor_id, context_id, kind)
    if actor not in state["implementationActors"]:
        raise ContractError(
            "only a registered implementation actor may request remote verification"
        )
    remote = state.get("remoteVerification")
    if remote is None:
        raise ContractError(f"change {change_id} requires no remote evidence")
    source = source_state(project_root)
    if (
        source.get("dirty") is not False
        or source.get("checkpoint") is None
        or source.get("fingerprint") is None
    ):
        raise ContractError(
            "remote verification requires a clean immutable source checkpoint"
        )
    request = build_remote_verification_request(
        project,
        contract,
        cycle=state["cycle"],
        checkpoint=source["checkpoint"],
        comparison_base=state["comparisonBase"],
        workspace_fingerprint=source["fingerprint"],
    )
    existing = remote.get("request")
    if isinstance(existing, dict):
        current = read_json(_artifact_path(project_root, existing))
        validate_remote_verification_request(current, "registered remote request")
        current_identity = {
            name: current[name]
            for name in (
                "changeId",
                "cycle",
                "project",
                "checkpoint",
                "comparisonBase",
                "workspaceFingerprint",
                "requirements",
                "controls",
            )
        }
        request_identity = {
            name: request[name]
            for name in current_identity
        }
        if current_identity == request_identity:
            return state, current
    destination = (
        _run_root(project_root, change_id)
        / "remote-verification"
        / f"cycle-{state['cycle']}-request.json"
    )
    _write_atomic(destination, request)
    reference = {
        "path": _relative(project_root, destination),
        "digest": _digest_file(destination),
    }
    remote["request"] = reference
    remote["evidence"] = None
    _event(
        state,
        "remote-verification-requested",
        actor,
        cycle=state["cycle"],
        request=reference,
        checkpoint=request["checkpoint"],
        requiredEvidence=remote["requiredEvidence"],
    )
    _save_state(project_root, state)
    return state, request


def _ingest_remote_verification_unlocked(
    project_root: Path,
    change_id: str,
    evidence_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "implementing")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    actor = _actor(actor_id, context_id, kind)
    if actor not in state["implementationActors"]:
        raise ContractError(
            "only a registered implementation actor may ingest remote verification"
        )
    remote = state.get("remoteVerification")
    if remote is None or not isinstance(remote.get("request"), dict):
        raise ContractError("remote verification request is missing")
    request = read_json(_artifact_path(project_root, remote["request"]))
    validate_remote_verification_request(request, "registered remote request")
    source = source_state(project_root)
    if (
        source.get("dirty") is not False
        or source.get("checkpoint") != request["checkpoint"]
        or source.get("fingerprint") != request["workspaceFingerprint"]
    ):
        raise ContractError("remote verification request is stale for current source")
    observed_evidence = read_remote_evidence_document(evidence_path)
    try:
        source_evidence, bundles = validate_remote_evidence_set(
            request, evidence_path
        )
    except ContractError as error:
        rejection = {
            "schemaVersion": 1,
            "kind": "engineering-process-remote-verification-rejection",
            "observedAt": _timestamp(),
            "requestSha256": canonical_json_digest(request),
            "sourceEvidenceSha256": canonical_json_digest(observed_evidence),
            "failureSha256": _digest_bytes(str(error).encode("utf-8")),
            "controls": {
                "grantsLifecycleCompletion": False,
                "grantsMerge": False,
                "grantsRelease": False,
                "grantsReview": False,
            },
        }
        rejection_path = (
            _run_root(project_root, change_id)
            / "improvements"
            / "evidence"
            / (
                f"remote-cycle-{state['cycle']}-"
                f"{rejection['failureSha256'].removeprefix('sha256:')[:16]}.json"
            )
        )
        _write_atomic(rejection_path, rejection)
        evidence_reference = {
            "path": _relative(project_root, rejection_path),
            "digest": _digest_file(rejection_path),
        }
        case = _observe_improvement_case(
            state,
            trigger="external-integration",
            evidence=evidence_reference,
            identity=(
                f"remote:{canonical_json_digest(request)}:"
                f"{rejection['failureSha256']}"
            ),
        )
        _event(
            state,
            "remote-verification-rejected",
            actor,
            cycle=state["cycle"],
            evidence=evidence_reference,
            improvementCase=case["id"],
        )
        _transition_phase(state, "failure")
        _save_state(project_root, state)
        raise
    root = (
        _run_root(project_root, change_id)
        / "remote-verification"
        / f"cycle-{state['cycle']}"
    )
    source_path = root / "source-evidence.json"
    _write_atomic(source_path, source_evidence)
    source_reference = {
        "path": _relative(project_root, source_path),
        "digest": _digest_file(source_path),
    }
    stored_artifacts: list[dict[str, Any]] = []
    for bundle in bundles:
        bundle_root = root / bundle["requirementId"] / bundle["selectorId"]
        manifest_path = bundle_root / "manifest.json"
        _write_atomic(manifest_path, bundle["manifest"])
        manifest_reference = {
            "path": _relative(project_root, manifest_path),
            "digest": _digest_file(manifest_path),
        }
        reports: list[dict[str, Any]] = []
        for name, report in sorted(bundle["reports"].items()):
            report_path = bundle_root / name
            _write_atomic(report_path, report)
            reports.append(
                {
                    "profile": report["profile"],
                    "path": _relative(project_root, report_path),
                    "digest": _digest_file(report_path),
                }
            )
        stored_artifacts.append(
            {
                "requirementId": bundle["requirementId"],
                "selectorId": bundle["selectorId"],
                "archive": bundle["archive"],
                "service": bundle["service"],
                "manifest": manifest_reference,
                "verification": reports,
            }
        )
    index = {
        "schemaVersion": 1,
        "kind": "engineering-process-ingested-remote-verification",
        "requestSha256": canonical_json_digest(request),
        "checkpoint": request["checkpoint"],
        "comparisonBase": request["comparisonBase"],
        "workspaceFingerprint": request["workspaceFingerprint"],
        "sourceEvidence": source_reference,
        "artifacts": stored_artifacts,
    }
    index_path = root / "index.json"
    _write_atomic(index_path, index)
    reference = {
        "path": _relative(project_root, index_path),
        "digest": _digest_file(index_path),
    }
    remote["evidence"] = reference
    if _required_profiles_current(state, contract):
        _transition_phase(state, "all-required-passed")
    _event(
        state,
        "remote-verification-ingested",
        actor,
        cycle=state["cycle"],
        evidence=reference,
        artifactCount=len(stored_artifacts),
        phase=state["phase"],
    )
    _save_state(project_root, state)
    return state, index


def _verify_change_unlocked(
    project_root: Path,
    project: Project,
    change_id: str,
    profile: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    if state["phase"] in {"improvement-required", "improvement-pending"}:
        blockers = [
            case["id"]
            for case in state["improvements"]
            if case["phase"] != "closed"
        ]
        raise ContractError(
            f"verification is blocked in {state['phase']} by improvement cases: "
            + ", ".join(blockers)
        )
    _require_phase(state, "implementing")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    actor = _actor(actor_id, context_id, kind)
    if actor not in state["implementationActors"]:
        raise ContractError("only a registered implementation actor may record verification")
    if profile not in contract["requiredProfiles"]:
        raise ContractError(f"profile {profile} is not required by change {change_id}")
    improvement_blockers = _improvement_verification_blockers(state)
    if improvement_blockers:
        raise ContractError(
            "verification is blocked by unresolved improvement cases: "
            + ", ".join(improvement_blockers)
        )
    require_environment_profile(project_root, project, profile=profile)
    report = run_profile(
        project_root,
        project,
        profile,
        base_ref=contract["comparisonBase"],
    )
    validate_verification(report, f"verification profile {profile}")
    report_path = (
        _run_root(project_root, change_id)
        / "verification"
        / f"cycle-{state['cycle']}-{profile}.json"
    )
    _write_atomic(report_path, report)
    eligibility_issues = _verification_eligibility_issues(report)
    if eligibility_issues:
        report_digest = _digest_file(report_path)
        evidence_reference = _copy_document(
            project_root,
            report_path,
            _run_root(project_root, change_id)
            / "improvements"
            / "evidence"
            / (
                f"verification-cycle-{state['cycle']}-{profile}-"
                f"{report_digest.removeprefix('sha256:')[:16]}.json"
            ),
        )
        case = _observe_improvement_case(
            state,
            trigger="verification-failure",
            evidence=evidence_reference,
            identity=(
                f"{profile}:{evidence_reference['digest']}:"
                + ",".join(eligibility_issues)
            ),
        )
        _event(
            state,
            "verification-rejected",
            actor,
            cycle=state["cycle"],
            profile=profile,
            report=_relative(project_root, report_path),
            reportDigest=report_digest,
            eligibilityIssues=eligibility_issues,
            improvementCase=case["id"],
        )
        _transition_phase(state, "failure")
        _save_state(project_root, state)
        raise ContractError(
            "lifecycle verification requires passing checks on a clean immutable "
            "checkpoint: " + ", ".join(eligibility_issues)
        )
    evidence = {
        "profile": profile,
        "path": _relative(project_root, report_path),
        "digest": _digest_file(report_path),
        "checkpoint": report["checkpoint"],
        "workspaceFingerprint": report["workspaceFingerprint"],
    }
    state["verification"] = [
        item for item in state["verification"] if item["profile"] != profile
    ]
    state["verification"].append(evidence)
    state["verification"].sort(key=lambda item: item["profile"])
    current_profiles = {item["profile"] for item in state["verification"]}
    required_profiles = set(contract["requiredProfiles"])
    checkpoints = {item["checkpoint"] for item in state["verification"]}
    fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
    if (
        required_profiles <= current_profiles
        and len(checkpoints) == 1
        and len(fingerprints) == 1
        and _remote_evidence_ready(project_root, state)
    ):
        _transition_phase(state, "all-required-passed")
    else:
        _transition_phase(state, "profile-passed")
    _event(
        state,
        "verification-recorded",
        actor,
        cycle=state["cycle"],
        profile=profile,
        phase=state["phase"],
        evidence=evidence,
    )
    _save_state(project_root, state)
    return state, report


def _start_review_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    method: str,
    attested_by: str,
    evidence: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "verified")
    _contract(project_root, state)
    _plan(project_root, state)
    reviewer = _actor(actor_id, context_id, kind)
    if method not in {"isolated-context", "separate-person"}:
        raise ContractError("review independence method is invalid")
    if (kind == "agent" and method != "isolated-context") or (
        kind == "human" and method != "separate-person"
    ):
        raise ContractError("review independence method does not match reviewer kind")
    if not attested_by or attested_by != attested_by.strip() or len(attested_by) > 256:
        raise ContractError("review attester must be a non-empty trimmed value")
    if attested_by in {reviewer["actorId"], reviewer["contextId"]}:
        raise ContractError("review independence cannot be self-attested")
    if not evidence or evidence != evidence.strip() or len(evidence) > 2000:
        raise ContractError("review independence evidence must be a non-empty trimmed value")
    actor_ids = {item["actorId"] for item in state["implementationActors"]}
    context_ids = {item["contextId"] for item in state["implementationActors"]}
    if reviewer["actorId"] in actor_ids or reviewer["contextId"] in context_ids:
        raise ContractError(
            "independent review requires an actor id and context id unused by implementation"
        )
    previous_review_contexts = {
        event["actor"]["contextId"]
        for event in state["history"]
        if event.get("event") == "review-started"
        and isinstance(event.get("actor"), dict)
        and isinstance(event["actor"].get("contextId"), str)
    }
    if reviewer["contextId"] in previous_review_contexts:
        raise ContractError(
            "independent review requires a fresh context id unused by earlier "
            "review assignments"
        )
    source = source_state(project_root)
    checkpoints = {item["checkpoint"] for item in state["verification"]}
    fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
    if (
        source["dirty"] is not False
        or source["checkpoint"] is None
        or source["fingerprint"] is None
        or checkpoints != {source["checkpoint"]}
        or fingerprints != {source["fingerprint"]}
    ):
        raise ContractError("review cannot start because verification evidence is stale")
    if not _remote_evidence_ready(project_root, state, source=source):
        raise ContractError(
            "review cannot start before required remote verification evidence passes"
        )
    assignment = {
        "changeId": change_id,
        "cycle": state["cycle"],
        "checkpoint": source["checkpoint"],
        "workspaceFingerprint": source["fingerprint"],
        "comparisonBase": state["comparisonBase"],
        "reviewer": reviewer,
        "independence": {
            "method": method,
            "attestedBy": attested_by,
            "evidence": evidence,
        },
        "contract": state["contract"],
        "plan": state["plan"],
        "verification": state["verification"],
        "remoteVerification": state.get("remoteVerification"),
        "pendingFindings": state["pendingFindings"],
    }
    reserve_review_context(project_root, state, reviewer)
    assignment_path = _run_root(project_root, change_id) / f"review-request-{state['cycle']}.json"
    _write_atomic(assignment_path, assignment)
    assignment["path"] = _relative(project_root, assignment_path)
    state["reviewAssignment"] = assignment
    _transition_phase(state, "reviewer-assigned")
    _event(
        state,
        "review-started",
        reviewer,
        cycle=state["cycle"],
        request=assignment["path"],
    )
    _save_state(project_root, state)
    return state, assignment


def _submit_review_unlocked(
    project_root: Path,
    change_id: str,
    report_path: Path,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "review-pending")
    assignment = state["reviewAssignment"]
    if not isinstance(assignment, dict):
        raise ContractError("review assignment is missing")
    document = read_json(report_path)
    validate_review(document, str(report_path))
    contract = _contract(project_root, state)
    required_review_schema = 3 if contract["schemaVersion"] == 3 else 2
    if document["schemaVersion"] != required_review_schema:
        raise ContractError(
            f"review schemaVersion {required_review_schema} is required for this change"
        )
    if required_review_schema == 3:
        contract_quality = {
            item["dimension"]: item for item in contract["quality"]["assessments"]
        }
        review_quality = {
            item["dimension"]: item for item in document["quality"]["assessments"]
        }
        if set(contract_quality) != set(review_quality):
            raise ContractError("review quality dimensions do not match the change contract")
        for dimension, accepted in contract_quality.items():
            reviewed = review_quality[dimension]
            expected_status = (
                "verified"
                if accepted["status"] == "applicable"
                else "not-applicable-confirmed"
            )
            if (
                (
                    reviewed["status"] not in {"verified", "failed"}
                    if expected_status == "verified"
                    else reviewed["status"] != expected_status
                )
                or reviewed["criteria"] != accepted["criteria"]
            ):
                raise ContractError(
                    f"review quality assessment for {dimension} does not match the contract"
                )
    for field in (
        "changeId",
        "cycle",
        "checkpoint",
        "workspaceFingerprint",
        "comparisonBase",
        "reviewer",
        "independence",
    ):
        if document[field] != assignment[field]:
            raise ContractError(f"review report {field} does not match its assignment")
    findings_by_id = {finding["id"]: finding for finding in document["findings"]}
    for pending in state["pendingFindings"]:
        current = findings_by_id.get(pending["id"])
        if current is None:
            raise ContractError(
                f"review report must carry forward pending finding {pending['id']}"
            )
        changed = [
            field
            for field in FINDING_IDENTITY_FIELDS
            if current[field] != pending[field]
        ]
        if changed:
            raise ContractError(
                f"review finding {pending['id']} changed immutable fields: "
                + ", ".join(changed)
            )
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] != document["checkpoint"]
        or source["fingerprint"] != document["workspaceFingerprint"]
    ):
        raise ContractError("review report is stale for the current source")
    destination = (
        _run_root(project_root, change_id) / f"review-{state['cycle']}.json"
    )
    artifact = _copy_document(project_root, report_path, destination)
    state["review"] = artifact
    state["pendingFindings"] = [
        dict(finding)
        for finding in document["findings"]
        if finding["status"] in UNRESOLVED_FINDING_STATUSES
    ]
    for finding in state["pendingFindings"]:
        _observe_improvement_case(
            state,
            trigger="review-finding",
            evidence=artifact,
            identity=f"{artifact['digest']}:{finding['id']}",
            finding_id=finding["id"],
        )
    if document["verdict"] == "approved":
        _resolve_reviewed_improvements(state)
    _transition_phase(state, document["verdict"])
    _event(
        state,
        "review-submitted",
        document["reviewer"],
        cycle=state["cycle"],
        verdict=document["verdict"],
        report=artifact,
    )
    _save_state(project_root, state)
    return state


def _finish_change_unlocked(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(project_root, change_id)
    _require_phase(state, "approved")
    contract = _contract(project_root, state)
    _plan(project_root, state)
    if state["review"] is None:
        raise ContractError("approved change has no review artifact")
    if state["pendingFindings"]:
        raise ContractError("completion requires every pending finding to be resolved")
    blocking_improvements = [
        case["id"]
        for case in state["improvements"]
        if case["phase"] not in {"closed", "producer-completed"}
    ]
    if blocking_improvements:
        raise ContractError(
            "completion requires every improvement case to reach a reviewed local "
            "resolution or producer completion: "
            + ", ".join(blocking_improvements)
        )
    review = read_json(_artifact_path(project_root, state["review"]))
    validate_review(review, "registered review")
    source = source_state(project_root)
    if (
        source["dirty"] is not False
        or source["checkpoint"] != review["checkpoint"]
        or source["fingerprint"] != review["workspaceFingerprint"]
    ):
        raise ContractError("approved review is stale for the current source")
    if not _remote_evidence_ready(project_root, state, source=source):
        raise ContractError(
            "completion requires current passing remote verification evidence"
        )
    for case in state["improvements"]:
        _require_producer_catalog_activation(project_root, state, case)
    required = set(contract["requiredProfiles"])
    if {item["profile"] for item in state["verification"]} != required:
        raise ContractError("completion requires the exact required verification profiles")
    for item in state["verification"]:
        report = read_json(_artifact_path(project_root, item))
        validate_verification(report, f"verification profile {item['profile']}")
        if report.get("status") != "passed":
            raise ContractError(f"verification profile {item['profile']} is not passing")
        if (
            report.get("checkpoint") != review["checkpoint"]
            or report.get("workspaceFingerprint") != review["workspaceFingerprint"]
        ):
            raise ContractError(f"verification profile {item['profile']} is stale")
    actor = _actor(actor_id, context_id, kind)
    completion = {
        "schemaVersion": 1,
        "changeId": change_id,
        "cycle": state["cycle"],
        "checkpoint": review["checkpoint"],
        "workspaceFingerprint": review["workspaceFingerprint"],
        "comparisonBase": state["comparisonBase"],
        "completedAt": _timestamp(),
        "completedBy": actor,
        "contract": state["contract"],
        "plan": state["plan"],
        "verification": state["verification"],
        "remoteVerification": (
            state["remoteVerification"]["evidence"]
            if state.get("remoteVerification") is not None
            else None
        ),
        "review": state["review"],
        "improvements": [
            {
                "id": case["id"],
                "role": case["role"],
                "phase": case["phase"],
                "invariantId": case["classification"]["invariantId"],
                "signal": case["signal"],
                "catalog": case["catalog"],
                "disposition": case["disposition"],
                "resolution": case["resolution"],
                "reproduction": case["reproduction"],
                "signalCanonicalSha256": _case_artifact_canonical_digest(
                    project_root, case, "signal"
                ),
                "catalogCanonicalSha256": _case_catalog_canonical_digest(
                    project_root, case
                ),
                "dispositionCanonicalSha256": _case_artifact_canonical_digest(
                    project_root, case, "disposition"
                ),
            }
            for case in state["improvements"]
        ],
    }
    validate_completion(completion, "completion")
    completion_path = _run_root(project_root, change_id) / "completion.json"
    _write_atomic(completion_path, completion)
    state["completion"] = {
        "path": _relative(project_root, completion_path),
        "digest": _digest_file(completion_path),
    }
    _transition_phase(state, "success")
    _event(state, "completed", actor, cycle=state["cycle"])
    _save_state(project_root, state)
    return state, completion


def lifecycle_status(project_root: Path, change_id: str) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    issues: list[str] = []
    for name in ("contract", "plan", "review", "completion"):
        artifact = state[name]
        if artifact is None:
            continue
        try:
            _artifact_path(project_root, artifact)
        except ContractError as error:
            issues.append(str(error))
    for evidence in state["verification"]:
        try:
            _artifact_path(project_root, evidence)
        except ContractError as error:
            issues.append(str(error))
    remote = state.get("remoteVerification")
    if remote is not None:
        for name in ("request", "evidence"):
            artifact = remote.get(name)
            if artifact is None:
                continue
            try:
                _artifact_path(project_root, artifact)
            except ContractError as error:
                issues.append(str(error))
    improvement_cases: list[dict[str, Any]] = []
    for case in state["improvements"]:
        try:
            improvement_cases.append(
                _improvement_case_status(project_root, state, case)
            )
        except ContractError as error:
            issues.append(str(error))
            classification = case["classification"]
            improvement_cases.append(
                {
                    "id": case["id"],
                    "phase": case["phase"],
                    "role": case["role"],
                    "trigger": case["trigger"],
                    "sourceCycle": case["sourceCycle"],
                    "invariantId": (
                        classification["invariantId"]
                        if classification is not None
                        else None
                    ),
                    "proposedInvariantId": (
                        classification["invariantId"]
                        if classification is not None
                        else None
                    ),
                    "canonicalInvariantId": None,
                    "ownerBoundary": (
                        classification["ownerBoundary"]
                        if classification is not None
                        else None
                    ),
                    "reusableClass": (
                        classification["reusableClass"]
                        if classification is not None
                        else None
                    ),
                    "recurrence": "unassessed",
                    "catalog": None,
                    "producer": None,
                    "release": None,
                    "consumer": None,
                    "artifacts": {},
                    "chainPhase": None,
                    "closed": case["phase"] == "closed",
                    "nextOwner": _improvement_next_owner(state["project"], case),
                    "status": "invalid-artifact-chain",
                }
            )
    improvement_blockers = [
        {
            "id": case["id"],
            "phase": case["phase"],
            "role": case["role"],
            "invariantId": case["invariantId"],
            "nextOwner": case["nextOwner"],
        }
        for case in improvement_cases
        if not case["closed"]
    ]
    if state["phase"] == "completed" and any(
        case["phase"] not in {"closed", "producer-completed"}
        for case in state["improvements"]
    ):
        issues.append("completed lifecycle has unresolved improvement cases")
    if state["phase"] in {"verified", "review-pending", "approved", "completed"}:
        source = source_state(project_root)
        checkpoints = {item["checkpoint"] for item in state["verification"]}
        fingerprints = {item["workspaceFingerprint"] for item in state["verification"]}
        if (
            source["dirty"] is not False
            or source["checkpoint"] is None
            or source["fingerprint"] is None
            or checkpoints != {source["checkpoint"]}
            or fingerprints != {source["fingerprint"]}
        ):
            issues.append("current source no longer matches lifecycle verification")
        try:
            if not _remote_evidence_ready(project_root, state, source=source):
                issues.append("required remote verification evidence is missing")
        except ContractError as error:
            issues.append(str(error))
    return {
        **state,
        "current": not issues,
        "issues": issues,
        "improvementStatus": {
            "count": len(state["improvements"]),
            "openCount": len(improvement_blockers),
            "blockers": improvement_blockers,
            "cases": improvement_cases,
        },
    }


def _classify_improvement_case_unlocked(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    owner_boundary: str,
    reusable_class: str,
    invariant_id: str,
    disposition: str,
    rationale_sha256: str,
    target_project: str | None,
    target_repository: str | None,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    _require_phase(state, "improvement-required")
    case = _improvement_case(state, case_id)
    if case["phase"] != "classification-required":
        raise ContractError(
            f"improvement case {case_id} is {case['phase']}; expected classification-required"
        )
    if owner_boundary not in IMPROVEMENT_OWNER_BOUNDARIES:
        raise ContractError("invalid improvement owner boundary")
    if reusable_class not in IMPROVEMENT_REUSABLE_CLASSES:
        raise ContractError("invalid reusable improvement class")
    if (
        not invariant_id
        or len(invariant_id) > 64
        or PROFILE_PATTERN.fullmatch(invariant_id) is None
    ):
        raise ContractError("invalid improvement invariant id")
    if disposition not in IMPROVEMENT_DISPOSITIONS:
        raise ContractError("invalid improvement disposition")
    if disposition == "producer-improvement" and owner_boundary != "shared-process":
        raise ContractError(
            "producer improvement requires the shared-process owner boundary"
        )
    if DIGEST_PATTERN.fullmatch(rationale_sha256) is None:
        raise ContractError("improvement rationale must be a SHA-256 digest")
    if disposition == "shared-escalation":
        if owner_boundary != "shared-process":
            raise ContractError(
                "shared escalation requires the shared-process owner boundary"
            )
        if (
            target_project is None
            or PROFILE_PATTERN.fullmatch(target_project) is None
            or target_repository is None
            or REPOSITORY_PATTERN.fullmatch(target_repository) is None
        ):
            raise ContractError(
                "shared escalation requires a valid target project and repository"
            )
        target: dict[str, str] | None = {
            "project": target_project,
            "repository": target_repository,
        }
        case["role"] = "consumer"
        case["phase"] = "producer-disposition-required"
    else:
        if target_project is not None or target_repository is not None:
            raise ContractError(
                "only shared escalation may select a remote improvement target"
            )
        target = None
        case["role"] = "local"
        case["phase"] = {
            "external-recovery": "local-resolution-required",
            "input-required": "input-required",
            "local-fix": "local-resolution-required",
            "producer-improvement": "local-resolution-required",
        }[disposition]
    case["classification"] = {
        "ownerBoundary": owner_boundary,
        "reusableClass": reusable_class,
        "invariantId": invariant_id,
        "disposition": disposition,
        "rationaleSha256": rationale_sha256,
        "target": target,
    }
    actor = _actor(actor_id, context_id, kind)
    _event(
        state,
        "improvement-classified",
        actor,
        caseId=case_id,
        ownerBoundary=owner_boundary,
        reusableClass=reusable_class,
        invariantId=invariant_id,
        disposition=disposition,
        phase=case["phase"],
    )
    if any(
        item["phase"] in {"classification-required", "input-required"}
        for item in state["improvements"]
    ):
        transition_result = "blocked-classified"
    elif any(
        item["role"] == "consumer" and item["phase"] != "closed"
        for item in state["improvements"]
    ):
        transition_result = "shared-classified"
    elif any(
        item["trigger"] == "review-finding" and item["phase"] != "closed"
        for item in state["improvements"]
    ):
        transition_result = "review-classified"
    else:
        transition_result = "local-classified"
    _transition_phase(state, transition_result)
    _save_state(project_root, state)
    return state


def _bind_improvement_chain_unlocked(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    signal_path: Path,
    catalog_path: Path | None,
    disposition_path: Path | None,
    resolution_path: Path | None,
    reproduction_path: Path | None,
    expected_canonical_digests: dict[str, str],
    chain_phase: str,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    case = _improvement_case(state, case_id)
    if case["classification"] is None:
        raise ContractError(
            f"improvement case {case_id} must be classified before artifact binding"
        )
    if case["role"] != "consumer":
        raise ContractError(
            "only a consumer improvement case can attach a transported chain"
        )
    expected_stage = {
        ("producer-disposition-required", False): "signal-exported",
        ("producer-disposition-required", True): "producer-disposition",
        ("producer-resolution-required", True): "producer-released",
        ("consumer-reproduction-required", True): "closed",
    }.get((case["phase"], case["signal"] is not None))
    if chain_phase == "producer-rejected" and (
        case["phase"] == "producer-disposition-required"
        and case["signal"] is not None
    ):
        expected_stage = "producer-rejected"
    if chain_phase != expected_stage:
        raise ContractError(
            f"improvement chain phase {chain_phase} cannot advance consumer case "
            f"from {case['phase']}"
        )
    required_fields = {
        "signal-exported": {"signal"},
        "producer-disposition": {"signal", "catalog", "disposition"},
        "producer-rejected": {"signal", "catalog", "disposition"},
        "producer-released": {"signal", "catalog", "disposition", "resolution"},
        "closed": {
            "signal",
            "catalog",
            "disposition",
            "resolution",
            "reproduction",
        },
    }[chain_phase]
    supplied = {
        "signal": signal_path,
        "catalog": catalog_path,
        "disposition": disposition_path,
        "resolution": resolution_path,
        "reproduction": reproduction_path,
    }
    if {name for name, path in supplied.items() if path is not None} != required_fields:
        raise ContractError(
            f"improvement chain phase {chain_phase} requires exactly: "
            + ", ".join(sorted(required_fields))
        )
    from .improvement import _stable_json_document

    snapshots: dict[str, dict[str, Any]] = {}
    for field in sorted(required_fields):
        source_path = supplied[field]
        assert source_path is not None
        document, _data = _stable_json_document(
            source_path,
            label=f"improvement {field} artifact",
        )
        expected_digest = expected_canonical_digests.get(field)
        if expected_digest is None or canonical_json_digest(document) != expected_digest:
            raise ContractError(
                f"improvement {field} changed after chain validation"
            )
        snapshots[field] = document
    signal_document = snapshots["signal"]
    classification = case["classification"]
    assert classification is not None
    if (
        signal_document.get("signalId") != case_id
        or signal_document.get("source", {}).get("project") != state["project"]
        or signal_document.get("source", {}).get("changeId") != change_id
        or signal_document.get("source", {}).get("cycle") != case["sourceCycle"]
        or signal_document.get("target") != classification["target"]
        or signal_document.get("trigger", {}).get("kind") != case["trigger"]
        or signal_document.get("claim", {}).get("ownerBoundary")
        != classification["ownerBoundary"]
        or signal_document.get("claim", {}).get("reusableClass")
        != classification["reusableClass"]
        or signal_document.get("claim", {}).get("proposedInvariantId")
        != classification["invariantId"]
        or signal_document.get("claim", {}).get("rationaleSha256")
        != classification["rationaleSha256"]
    ):
        raise ContractError(
            f"improvement signal does not belong to lifecycle case {case_id}"
        )
    destination_root = _run_root(project_root, change_id) / "improvements" / case_id
    for field in sorted(required_fields):
        current = case[field]
        if current is not None:
            current_document = read_json(_artifact_path(project_root, current))
            if canonical_json_digest(current_document) != expected_canonical_digests[field]:
                raise ContractError(
                    f"improvement case {case_id} {field} artifact is immutable"
                )
            continue
        destination = destination_root / f"{field}.json"
        _write_atomic(destination, snapshots[field])
        case[field] = {
            "path": _relative(project_root, destination),
            "digest": _digest_file(destination),
        }
    case["phase"] = {
        "signal-exported": "producer-disposition-required",
        "producer-disposition": "producer-resolution-required",
        "producer-rejected": "input-required",
        "producer-released": "consumer-reproduction-required",
        "closed": "closed",
    }[chain_phase]
    actor = _actor(actor_id, context_id, kind)
    _event(
        state,
        "improvement-chain-bound",
        actor,
        caseId=case_id,
        chainPhase=chain_phase,
        phase=case["phase"],
    )
    if state["phase"] == "improvement-pending":
        if chain_phase == "producer-rejected":
            _transition_phase(state, "producer-rejected")
        elif chain_phase == "closed" and not any(
            item["role"] == "consumer" and item["phase"] != "closed"
            for item in state["improvements"]
        ):
            _transition_phase(
                state,
                (
                    "review-chain-closed"
                    if any(
                        item["trigger"] == "review-finding"
                        for item in state["improvements"]
                    )
                    else "chain-closed"
                ),
            )
    _save_state(project_root, state)
    return state


def _register_producer_improvement_case_unlocked(
    project_root: Path,
    change_id: str,
    *,
    signal_path: Path,
    catalog_path: Path,
    disposition_path: Path,
    expected_canonical_digests: dict[str, str],
    signal_id: str,
    canonical_invariant_id: str,
    owner_boundary: str,
    reusable_class: str,
    rationale_sha256: str,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    state = load_state(project_root, change_id)
    if state["phase"] not in {"planned", "implementing", "changes-requested"}:
        raise ContractError(
            "producer improvement intake requires a planned or implementing change"
        )
    case_id = _improvement_case_id("signal", state["cycle"], signal_id)
    if any(case["id"] == case_id for case in state["improvements"]):
        raise ContractError(f"improvement signal {signal_id} is already registered")
    destination_root = _run_root(project_root, change_id) / "improvements" / case_id
    from .improvement import _stable_json_document

    artifacts: dict[str, dict[str, str]] = {}
    for field, source_path in (
        ("signal", signal_path),
        ("catalog", catalog_path),
        ("disposition", disposition_path),
    ):
        document, _data = _stable_json_document(
            source_path, label=f"producer improvement {field}"
        )
        if canonical_json_digest(document) != expected_canonical_digests[field]:
            raise ContractError(
                f"producer improvement {field} changed after validation"
            )
        destination = destination_root / f"{field}.json"
        _write_atomic(destination, document)
        artifacts[field] = {
            "path": _relative(project_root, destination),
            "digest": _digest_file(destination),
        }
    signal = artifacts["signal"]
    catalog = artifacts["catalog"]
    disposition = artifacts["disposition"]
    case = {
        "id": case_id,
        "role": "producer",
        "trigger": "external-integration",
        "phase": "producer-change",
        "sourceCycle": state["cycle"],
        "findingId": None,
        "evidence": signal,
        "classification": {
            "ownerBoundary": owner_boundary,
            "reusableClass": reusable_class,
            "invariantId": canonical_invariant_id,
            "disposition": "producer-improvement",
            "rationaleSha256": rationale_sha256,
            "target": None,
        },
        "signal": signal,
        "catalog": catalog,
        "disposition": disposition,
        "resolution": None,
        "reproduction": None,
    }
    state["improvements"].append(case)
    state["improvements"].sort(key=lambda item: item["id"])
    actor = _actor(actor_id, context_id, kind)
    _event(
        state,
        "improvement-signal-ingested",
        actor,
        caseId=case_id,
        signalId=signal_id,
        invariantId=canonical_invariant_id,
        phase=case["phase"],
    )
    _save_state(project_root, state)
    return state


def start_change(
    project_root: Path,
    project: Project,
    contract_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    document = read_json(contract_path)
    validate_change(document, str(contract_path))
    change_id = document["id"]
    with _change_lock(project_root, change_id):
        return _start_change_unlocked(
            project_root,
            project,
            contract_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def classify_improvement_case(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    owner_boundary: str,
    reusable_class: str,
    invariant_id: str,
    disposition: str,
    rationale_sha256: str,
    target_project: str | None,
    target_repository: str | None,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _classify_improvement_case_unlocked(
            project_root,
            change_id,
            case_id,
            owner_boundary=owner_boundary,
            reusable_class=reusable_class,
            invariant_id=invariant_id,
            disposition=disposition,
            rationale_sha256=rationale_sha256,
            target_project=target_project,
            target_repository=target_repository,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def bind_improvement_chain(
    project_root: Path,
    change_id: str,
    case_id: str,
    *,
    signal_path: Path,
    catalog_path: Path | None,
    disposition_path: Path | None,
    resolution_path: Path | None,
    reproduction_path: Path | None,
    expected_canonical_digests: dict[str, str],
    chain_phase: str,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _bind_improvement_chain_unlocked(
            project_root,
            change_id,
            case_id,
            signal_path=signal_path,
            catalog_path=catalog_path,
            disposition_path=disposition_path,
            resolution_path=resolution_path,
            reproduction_path=reproduction_path,
            expected_canonical_digests=expected_canonical_digests,
            chain_phase=chain_phase,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def register_producer_improvement_case(
    project_root: Path,
    change_id: str,
    *,
    signal_path: Path,
    catalog_path: Path,
    disposition_path: Path,
    expected_canonical_digests: dict[str, str],
    signal_id: str,
    canonical_invariant_id: str,
    owner_boundary: str,
    reusable_class: str,
    rationale_sha256: str,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _register_producer_improvement_case_unlocked(
            project_root,
            change_id,
            signal_path=signal_path,
            catalog_path=catalog_path,
            disposition_path=disposition_path,
            expected_canonical_digests=expected_canonical_digests,
            signal_id=signal_id,
            canonical_invariant_id=canonical_invariant_id,
            owner_boundary=owner_boundary,
            reusable_class=reusable_class,
            rationale_sha256=rationale_sha256,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def register_plan(
    project_root: Path,
    project: Project,
    change_id: str,
    plan_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _register_plan_unlocked(
            project_root,
            project,
            change_id,
            plan_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def begin_implementation(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _begin_implementation_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def verify_change(
    project_root: Path,
    project: Project,
    change_id: str,
    profile: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _verify_change_unlocked(
            project_root,
            project,
            change_id,
            profile,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def request_remote_verification(
    project_root: Path,
    project: Project,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _request_remote_verification_unlocked(
            project_root,
            project,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def ingest_remote_verification(
    project_root: Path,
    change_id: str,
    evidence_path: Path,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _ingest_remote_verification_unlocked(
            project_root,
            change_id,
            evidence_path,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )


def start_review(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
    method: str,
    attested_by: str,
    evidence: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _start_review_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
            method=method,
            attested_by=attested_by,
            evidence=evidence,
        )


def submit_review(
    project_root: Path,
    change_id: str,
    report_path: Path,
) -> dict[str, Any]:
    with _change_lock(project_root, change_id):
        return _submit_review_unlocked(project_root, change_id, report_path)


def finish_change(
    project_root: Path,
    change_id: str,
    *,
    actor_id: str,
    context_id: str,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _change_lock(project_root, change_id):
        return _finish_change_unlocked(
            project_root,
            change_id,
            actor_id=actor_id,
            context_id=context_id,
            kind=kind,
        )
