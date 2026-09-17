"""Automated bounded process-improvement incident intake for change finish."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable

from .contracts import ProcessError
from .evidence import child_environment
from . import VERSION
from .project import load_project
from .repository import _git
from .supervision import process_supervisor

DEFAULT_MAX_ISSUES_PER_FINISH = 3
DEFAULT_MAX_ISSUES_PER_KEY = 1
MAX_TRACKER_RESULTS = 32
MAX_TRACKER_OUTPUT_BYTES = 64_000
MAX_TRACKER_URL_BYTES = 512
MAX_TRACKER_TITLE_BYTES = 512

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)$"
)
_SAFE_REASON = {
    "input-digest-mismatch",
    "checkpoint-mismatch",
    "policy-changed",
    "runtime-changed",
    "comparison-base-changed",
    "verification-repetition",
}

CLOSED_TAXONOMY = (
    "evidence-integrity",
    "execution-boundary",
    "governance-thrashing",
    "invariant-violation",
    "publication-boundary",
    "explicit-review-signal",
)


@dataclass(frozen=True)
class Incident:
    kind: str
    invariant: str
    summary: str
    details: dict[str, Any]
    severity: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _policy_allows_external_intake(project: dict[str, Any]) -> bool:
    """Use the existing consumer process policy as the external-I/O boundary."""
    policy = project.get("lifecycle", {}).get("processChanges", {})
    return bool(
        isinstance(policy, dict)
        and policy.get("requireConsumerEvidence") is True
        and isinstance(policy.get("acceptedIssueUrlPrefix"), str)
        and policy["acceptedIssueUrlPrefix"]
    )


def _valid_issue_url(repo: str, value: Any) -> bool:
    if (
        not isinstance(repo, str)
        or not isinstance(value, str)
        or len(value.encode("utf-8")) > MAX_TRACKER_URL_BYTES
    ):
        return False
    match = _ISSUE_URL.fullmatch(value)
    return bool(match and match.group(1).casefold() == repo.casefold())


def _validate_tracker_matches(matches: Any) -> list[dict[str, Any]]:
    """Validate the small provider result shape before making a decision."""
    if not isinstance(matches, list) or len(matches) > MAX_TRACKER_RESULTS:
        raise ProcessError("tracker search returned an invalid result set")
    try:
        encoded = json.dumps(matches, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProcessError("tracker search returned invalid data") from error
    if len(encoded) > MAX_TRACKER_OUTPUT_BYTES:
        raise ProcessError("tracker search exceeded its output limit")
    for item in matches:
        if not isinstance(item, dict):
            raise ProcessError("tracker search returned an invalid result set")
        if not isinstance(item.get("title"), str):
            raise ProcessError("tracker search returned an invalid result set")
        if len(item["title"].encode("utf-8")) > MAX_TRACKER_TITLE_BYTES:
            raise ProcessError("tracker search returned an invalid result set")
        if not isinstance(item.get("url"), str):
            raise ProcessError("tracker search returned an invalid result set")
        if item.get("state") not in {"OPEN", "CLOSED"}:
            raise ProcessError("tracker search returned an invalid result set")
    return matches


def _safe_identifier(value: Any, fallback: str = "unknown") -> str:
    text = value if isinstance(value, str) else ""
    return text if _SAFE_IDENTIFIER.fullmatch(text) else fallback


def _safe_consumer(value: Any) -> str:
    raw = value if isinstance(value, str) else ""
    if not raw:
        return "unknown"
    encoded: list[str] = []
    for byte in raw.encode("utf-8"):
        character = chr(byte)
        if character.isascii() and (character.isalnum() or character in ".-"):
            encoded.append(character)
        else:
            encoded.append(f"_{byte:02x}")
    return "".join(encoded)


def _public_details(incident: Incident) -> dict[str, Any]:
    """Project internal incident state into a small, typed public shape."""
    allowed = {
        "evidence-integrity": {
            "profile", "reason", "cycle", "verificationCount",
            "invalidationCount",
            "recordedInputDigest", "currentInputDigest", "recordedCheckpointFingerprint",
            "currentCheckpointFingerprint", "digest",
        },
        "execution-boundary": {"profile", "checkId"},
        "governance-thrashing": {"cycleCount"},
        "invariant-violation": {"invariant"},
        "publication-boundary": {"reason"},
        "explicit-review-signal": set(),
    }.get(incident.kind, set())
    public: dict[str, Any] = {}
    for key in sorted(allowed):
        value = incident.details.get(key)
        if key in {"profile", "checkId", "invariant"}:
            if _SAFE_IDENTIFIER.fullmatch(value or ""):
                public[key] = value
        elif key == "reason":
            if value in _SAFE_REASON:
                public[key] = value
        elif key in {
            "recordedInputDigest", "currentInputDigest",
            "recordedCheckpointFingerprint", "currentCheckpointFingerprint", "digest",
        }:
            if _SAFE_DIGEST.fullmatch(value or ""):
                public[key] = value
        elif key in {"cycle", "verificationCount", "invalidationCount", "cycleCount"}:
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2048:
                public[key] = value
    return public


def _public_summary(incident: Incident) -> str:
    """Build a title/body summary without copying reviewer or runtime prose."""
    invariant = _safe_identifier(incident.invariant)
    details = _public_details(incident)
    if incident.kind == "evidence-integrity":
        return f"Verification evidence crossed the '{invariant}' freshness boundary"
    if incident.kind == "execution-boundary":
        profile = _safe_identifier(details.get("profile"))
        check = _safe_identifier(details.get("checkId"), "check")
        return f"Bounded execution reported '{check}' in profile '{profile}'"
    if incident.kind == "governance-thrashing":
        return "The change crossed the correction/reviewer recovery boundary"
    if incident.kind == "invariant-violation":
        return f"Production invariant '{invariant}' was violated during review"
    if incident.kind == "publication-boundary":
        return "Publication preflight reported a bounded source boundary failure"
    return "Independent review reported a shared-process signal"


def _record_intake_event(
    state: dict[str, Any],
    event: str,
    actor: dict[str, str],
    incident: Incident,
    stable_key: str,
    **details: Any,
) -> None:
    state.setdefault("history", []).append(
        {
            "event": event,
            "at": _now_iso(),
            "actor": actor,
            "details": {
                "stableKey": stable_key,
                "kind": incident.kind,
                "invariant": incident.invariant,
                **details,
            },
        }
    )


def _prior_intake_result(
    state: dict[str, Any], stable_key: str, process_repo: str
) -> dict[str, Any] | None:
    """Return the first recorded result so a repeated finish cannot retry a side effect."""
    for event in reversed(state.get("history", [])):
        if event.get("details", {}).get("stableKey") != stable_key:
            continue
        details = event.get("details", {})
        if event.get("event") in {"process-improvement-created", "process-improvement-reused"}:
            record_url = details.get("recordUrl")
            if _valid_issue_url(process_repo, record_url):
                return {
                    "status": "reused",
                    "stableKey": stable_key,
                    "recordUrl": record_url,
                }
            return {
                "status": "failed",
                "stableKey": stable_key,
                "errorCode": "invalid-record-url",
            }
        if event.get("event") == "process-improvement-suppressed":
            return {
                "status": "suppressed",
                "stableKey": stable_key,
                "reason": details.get("reason", "policy-disabled"),
            }
        if event.get("event") == "process-improvement-failed":
            return {
                "status": "failed",
                "stableKey": stable_key,
                "errorCode": details.get("errorCode", "tracker-failed"),
            }
    return None


def stable_title_key(consumer: str, process_version: str, invariant: str, kind: str) -> str:
    """Format the stable non-sensitive deduplication title key."""
    clean_consumer = _safe_consumer(consumer)
    clean_invariant = _safe_identifier(invariant)
    clean_kind = _safe_identifier(kind)
    return f"[consumer-process][{clean_consumer}][{process_version}][{clean_invariant}][{clean_kind}]"


def resolve_consumer_identity(project_root: Path, project: dict[str, Any]) -> str:
    """Derive the canonical consumer repository identity."""
    try:
        remote = _git(project_root, ["config", "--get", "remote.origin.url"]).decode("utf-8").strip()
        match = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+?)(?:\.git)?$", remote)
        if match:
            return match.group(1)
    except Exception:
        pass
    return project.get("project", "unknown-consumer")


def resolve_process_repo(project: dict[str, Any]) -> str | None:
    """Resolve a supported configured GitHub destination, with no fallback."""
    prefix = (
        project.get("lifecycle", {})
        .get("processChanges", {})
        .get("acceptedIssueUrlPrefix", "")
    )
    match = re.fullmatch(
        r"https://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/issues/",
        prefix,
    )
    if match:
        return match.group(1)
    return None


def collect_incidents(
    project_root: Path,
    process_root: Path,
    state: dict[str, Any],
) -> list[Incident]:
    """Observe exact current lifecycle state and emit structured incidents for closed taxonomy."""
    incidents: list[Incident] = []
    seen: set[tuple[str, str]] = set()

    def _add(incident: Incident) -> None:
        key = (incident.kind, incident.invariant)
        if key not in seen:
            seen.add(key)
            incidents.append(incident)

    history = state.get("history", [])

    # Only the current implementation cycle can attribute a new signal to the
    # operation being finished.  Older history may belong to an already
    # completed change or to a prior correction cycle.
    cycle_start = -1
    current_cycle = state.get("cycle", 1)
    for index, event in enumerate(history):
        if (
            event.get("event") == "implementation-started"
            and event.get("details", {}).get("cycle") == current_cycle
        ):
            cycle_start = index
    current_history = history[cycle_start + 1:]
    lineage_start = -1
    for index in range(cycle_start - 1, -1, -1):
        if history[index].get("event") == "finished":
            lineage_start = index
            break
    active_lineage = history[lineage_start + 1:]

    # 1. Evidence-integrity: repeated invalidation before a successful rerun.
    # One invalidation is expected when a candidate, policy, runtime, or input
    # changes; it is not evidence of a shared-process defect by itself.
    pending_invalidations: dict[str, dict[str, Any]] = {}
    for event in current_history:
        if event.get("event") == "evidence-invalidated":
            details = event.get("details", {})
            profile = details.get("profile", "unknown-profile")
            # A legacy/minimal state without an implementation boundary cannot
            # prove whether the invalidation was expected, so retain it as an
            # actionable evidence-integrity signal instead of guessing.
            repeated = dict(details) if cycle_start < 0 else None
            if profile in pending_invalidations or repeated is not None:
                repeated = dict(details) if repeated is None else repeated
                repeated["invalidationCount"] = 2 if profile in pending_invalidations else 1
                _add(
                    Incident(
                        kind="evidence-integrity",
                        invariant=profile,
                        summary=(
                            f"Verification report for '{profile}' was invalidated "
                            + (
                                "repeatedly before a successful rerun"
                                if profile in pending_invalidations
                                else "without a current implementation boundary"
                            )
                        ),
                        details=repeated,
                        severity="high",
                    )
                )
            else:
                pending_invalidations[profile] = details
        elif event.get("event") == "profile-verified":
            profile = event.get("details", {}).get("profile")
            if profile:
                pending_invalidations.pop(profile, None)

    # 1c. Publication-boundary: callers may persist a structured preflight failure
    # before returning the lifecycle operation's error. Never infer one from text.
    for event in current_history:
        if event.get("event") == "publication-failed":
            details = event.get("details", {})
            _add(
                Incident(
                    kind="publication-boundary",
                    invariant="publication",
                    summary="Publication preflight reported a bounded source boundary failure",
                    details=details,
                    severity="high",
                )
            )

    # 2. Execution-boundary: check timeouts, overflows, terminated descendants, stream failures
    for profile, report in state.get("verification", {}).items():
        for check in report.get("checks", []):
            check_id = check.get("id", "check")
            if check.get("timedOut"):
                _add(
                    Incident(
                        kind="execution-boundary",
                        invariant="check-timeout",
                        summary=f"Check '{check_id}' in profile '{profile}' timed out",
                        details={"profile": profile, "checkId": check_id},
                        severity="high",
                    )
                )
            if check.get("outputExceeded"):
                _add(
                    Incident(
                        kind="execution-boundary",
                        invariant="output-overflow",
                        summary=f"Check '{check_id}' in profile '{profile}' exceeded maximum captured output",
                        details={"profile": profile, "checkId": check_id},
                        severity="medium",
                    )
                )
            if check.get("descendantsTerminated"):
                _add(
                    Incident(
                        kind="execution-boundary",
                        invariant="descendants-terminated",
                        summary=f"Check '{check_id}' in profile '{profile}' leaked child processes requiring termination",
                        details={"profile": profile, "checkId": check_id},
                        severity="medium",
                    )
                )
            if check.get("streamFailed"):
                _add(
                    Incident(
                        kind="execution-boundary",
                        invariant="stream-failed",
                        summary=f"Check '{check_id}' in profile '{profile}' encountered stream capture failure",
                        details={"profile": profile, "checkId": check_id},
                        severity="high",
                    )
                )

    # 3. Governance-thrashing: repeated corrections in the active change lineage
    # or reviewer replacement. Review corrections advance implementation cycles,
    # so cycle equality cannot identify the repeated sequence.
    correction_count = sum(
        event.get("event") == "review-submitted"
        and event.get("details", {}).get("verdict") == "changes-requested"
        for event in active_lineage
    )
    if correction_count >= 2:
        _add(
            Incident(
                kind="governance-thrashing",
                invariant="excessive-review-cycles",
                summary=(
                    f"Change received {correction_count} changes-requested reviews "
                    "in the active implementation lineage"
                ),
                details={"cycleCount": correction_count},
                severity="medium",
            )
        )

    for event in active_lineage:
        if event.get("event") == "review-assignment-replaced":
            _add(
                Incident(
                    kind="governance-thrashing",
                    invariant="reviewer-context-replaced",
                    summary="Reviewer assignment had to be replaced due to context reuse conflict",
                    details=event.get("details", {}),
                    severity="high",
                )
            )

    # 4. Invariant-violation & Explicit-review-signal
    review_doc = state.get("review", {}).get("document") if state.get("review") else None
    if review_doc:
        for inv in review_doc.get("productionEngineering", []):
            if inv.get("status") == "violated":
                inv_id = inv.get("id", "unknown-invariant")
                _add(
                    Incident(
                        kind="invariant-violation",
                        invariant=inv_id,
                        summary=f"Production invariant '{inv_id}' was violated during review",
                        details={"invariant": inv_id, "rationale": inv.get("rationale")},
                        severity="high",
                    )
                )
        pi = review_doc.get("processImprovement", {})
        if pi.get("status") == "shared-process":
            _add(
                Incident(
                    kind="explicit-review-signal",
                    invariant="shared-process-finding",
                    summary=pi.get("rationale", "Reviewer identified shared process improvement requirement"),
                    details=pi,
                    severity="medium",
                )
            )

    return incidents


def _run_tracker_command(
    command: list[str],
    *,
    output_limit: int,
    failure_message: str,
) -> bytes:
    """Run a tracker command with the shared bounded process supervisor."""
    supervisor = process_supervisor()
    process: Any | None = None
    output = bytearray()
    overflow = threading.Event()
    stream_error = threading.Event()
    timed_out = False
    cleanup_error: str | None = None
    cleanup_bounded = True
    termination_requested = False
    reader: threading.Thread | None = None
    error_reader: threading.Thread | None = None
    reader_started = False
    error_reader_started = False

    def apply_cleanup(outcome: Any) -> None:
        nonlocal cleanup_error, cleanup_bounded
        cleanup_bounded = cleanup_bounded and bool(outcome.bounded)
        cleanup_error = cleanup_error or outcome.error

    def request_termination() -> None:
        nonlocal termination_requested
        if termination_requested or process is None:
            return
        termination_requested = True
        apply_cleanup(supervisor.terminate(process, grace_seconds=2))

    def finalize_process() -> None:
        nonlocal cleanup_error, cleanup_bounded
        if process is None:
            return
        try:
            if process.poll() is None:
                request_termination()
        except BaseException:
            cleanup_bounded = False
            cleanup_error = cleanup_error or "tracker process termination could not be requested"
        try:
            apply_cleanup(supervisor.finalize(process, grace_seconds=2))
        except BaseException:
            cleanup_bounded = False
            cleanup_error = cleanup_error or "tracker process finalization failed"

        if reader_started and reader is not None:
            reader.join(timeout=2)
        if error_reader_started and error_reader is not None:
            error_reader.join(timeout=2)
        if (
            (reader_started and reader is not None and reader.is_alive())
            or (error_reader_started and error_reader is not None and error_reader.is_alive())
        ):
            cleanup_bounded = False
            cleanup_error = cleanup_error or "tracker output drain did not finish"
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            cleanup_bounded = False
            cleanup_error = cleanup_error or "tracker process was not reaped"

    try:
        process = supervisor.spawn(
            tuple(command),
            working_directory=Path.cwd(),
            environment=child_environment(managed_only=True),
        )
        if process.stdout is None or process.stderr is None:
            raise ProcessError("tracker process did not expose output streams")

        def read_output() -> None:
            try:
                while True:
                    remaining = output_limit + 1 - len(output)
                    if remaining <= 0:
                        overflow.set()
                        return
                    chunk = process.stdout.read(min(64 * 1024, remaining))
                    if not chunk:
                        return
                    output.extend(chunk)
                    if len(output) > output_limit:
                        overflow.set()
                        return
            except (OSError, ValueError):
                stream_error.set()
            finally:
                process.stdout.close()

        def discard_error() -> None:
            try:
                while process.stderr.read(64 * 1024):
                    pass
            except (OSError, ValueError):
                stream_error.set()
            finally:
                process.stderr.close()

        reader = threading.Thread(target=read_output, daemon=True)
        error_reader = threading.Thread(target=discard_error, daemon=True)
        reader.start()
        reader_started = True
        error_reader.start()
        error_reader_started = True
        deadline = time.monotonic() + 30

        while reader.is_alive():
            try:
                supervisor.observe(process)
            except OSError:
                cleanup_bounded = False
                cleanup_error = cleanup_error or "tracker process observation failed"
                break
            if overflow.is_set():
                request_termination()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                request_termination()
                break
            time.sleep(0.01)

        if overflow.is_set() or timed_out:
            request_termination()

        if (
            not reader.is_alive()
            and process.poll() is None
            and not overflow.is_set()
            and not timed_out
        ):
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                timed_out = True
                request_termination()
    except OSError as error:
        raise ProcessError(failure_message) from error
    finally:
        finalize_process()
    if not cleanup_bounded or cleanup_error:
        raise ProcessError(failure_message)
    if overflow.is_set():
        raise ProcessError(f"{failure_message} exceeded its output limit")
    if timed_out:
        raise ProcessError(f"{failure_message} timed out")
    if stream_error.is_set() or process.returncode != 0:
        raise ProcessError(failure_message)
    return bytes(output)


def _default_search_tracker(repo: str, search_query: str) -> list[dict[str, Any]]:
    """Search all tracker states or fail explicitly; an error is never an empty result."""
    output = _run_tracker_command(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(MAX_TRACKER_RESULTS),
            "--search",
            f"{search_query} in:title",
            "--json",
            "number,title,url,state",
        ],
        output_limit=MAX_TRACKER_OUTPUT_BYTES,
        failure_message="tracker search failed",
    )
    try:
        matches = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise ProcessError("tracker search returned invalid data") from error
    return _validate_tracker_matches(matches)


def _default_create_issue(repo: str, title: str, body: str) -> str:
    """Create one issue and return its unvalidated provider result."""
    output = _run_tracker_command(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
        ],
        output_limit=MAX_TRACKER_URL_BYTES,
        failure_message="tracker issue creation failed",
    )
    try:
        return output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ProcessError("tracker issue creation returned an invalid URL") from error


def render_sanitized_issue_body(
    consumer: str,
    process_version: str,
    incident: Incident,
    state: dict[str, Any],
) -> str:
    """Render a bounded body from the allow-listed public incident projection."""
    safe_consumer = _safe_consumer(consumer)
    safe_version = _safe_identifier(process_version)
    safe_kind = _safe_identifier(incident.kind)
    safe_invariant = _safe_identifier(incident.invariant)
    details_rendered = json.dumps(
        _public_details(incident), indent=2, sort_keys=True
    )

    return f"""## Consumer evidence

Consumer: `{safe_consumer}`
Process authority: engineering-process `{safe_version}`
Incident kind: `{safe_kind}`
Invariant: `{safe_invariant}`

Observed behavior:
{_public_summary(incident)}

### Structured incident details

```json
{details_rendered}
```

## Expected invariant

The shared process must maintain the `{safe_invariant}` invariant under the `{safe_kind}` boundary without unhandled errors, dropped evidence, or unexpected operational thrashing.

## Scope

Shared process guidance and enforcement for `{safe_invariant}`. Product behavior stays in the consumer repository; this report is an evidence handoff only.
"""


def is_process_producer_change(state: dict[str, Any]) -> bool:
    """Check if the current change is modifying the shared process distribution itself."""
    contract = state.get("contract", {}).get("document", {})
    affected = contract.get("affectedProjects", [])
    return "engineering-process" in affected


def process_improvement_intake(
    project_root: Path,
    process_root: Path,
    state: dict[str, Any],
    actor: dict[str, str],
    *,
    search_tracker_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
    create_issue_fn: Callable[[str, str, str], str] | None = None,
    max_issues_per_finish: int = DEFAULT_MAX_ISSUES_PER_FINISH,
    max_issues_per_key: int = DEFAULT_MAX_ISSUES_PER_KEY,
    project: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect incidents and perform bounded, policy-authorized tracker handoff."""
    incidents = collect_incidents(project_root, process_root, state)
    if not incidents:
        return []

    project = project if project is not None else load_project(project_root, process_root)
    consumer = resolve_consumer_identity(project_root, project)
    process_repo = resolve_process_repo(project)
    search_tracker = search_tracker_fn if search_tracker_fn is not None else _default_search_tracker
    create_issue = create_issue_fn if create_issue_fn is not None else _default_create_issue

    is_producer = is_process_producer_change(state)
    created_count = 0
    created_by_key: dict[str, int] = {}
    results: list[dict[str, Any]] = []

    for incident in incidents:
        stable_key = stable_title_key(consumer, VERSION, incident.invariant, incident.kind)
        previous = _prior_intake_result(state, stable_key, process_repo)
        if previous is not None:
            results.append(previous)
            continue

        _record_intake_event(
            state,
            "incident-collected",
            actor,
            incident,
            stable_key,
            summary=_public_summary(incident),
            severity=incident.severity,
        )

        # Collection is observable even when policy forbids external I/O.
        if not _policy_allows_external_intake(project):
            _record_intake_event(
                state,
                "process-improvement-suppressed",
                actor,
                incident,
                stable_key,
                reason="policy-disabled",
            )
            results.append(
                {"status": "suppressed", "stableKey": stable_key, "reason": "policy-disabled"}
            )
            continue

        if process_repo is None:
            _record_intake_event(
                state,
                "process-improvement-suppressed",
                actor,
                incident,
                stable_key,
                reason="unsupported-tracker-namespace",
            )
            results.append(
                {
                    "status": "suppressed",
                    "stableKey": stable_key,
                    "reason": "unsupported-tracker-namespace",
                }
            )
            continue

        # A producer must not create or search for recursive process issues.
        if is_producer:
            _record_intake_event(
                state,
                "process-improvement-suppressed",
                actor,
                incident,
                stable_key,
                reason="recursion-breaker",
            )
            results.append(
                {"status": "suppressed", "stableKey": stable_key, "reason": "recursion-breaker"}
            )
            continue

        try:
            matches = _validate_tracker_matches(search_tracker(process_repo, stable_key))
            exact_matches = [
                item
                for item in matches
                if item["title"] == stable_key
                or item["title"].startswith(stable_key + " ")
            ]
            if len(exact_matches) > 1:
                raise ProcessError("tracker search returned ambiguous matching issues")
            if exact_matches:
                exact_match = exact_matches[0]
                record_url = exact_match.get("url")
                if not _valid_issue_url(process_repo, record_url):
                    raise ProcessError("tracker search returned an invalid issue URL")
                _record_intake_event(
                    state,
                    "process-improvement-reused",
                    actor,
                    incident,
                    stable_key,
                    recordUrl=record_url,
                    trackerState=exact_match.get("state"),
                )
                results.append(
                    {"status": "reused", "stableKey": stable_key, "recordUrl": record_url}
                )
                continue
        except Exception:
            _record_intake_event(
                state,
                "process-improvement-failed",
                actor,
                incident,
                stable_key,
                stage="search",
                errorCode="tracker-search-failed",
            )
            results.append(
                {
                    "status": "failed",
                    "stableKey": stable_key,
                    "errorCode": "tracker-search-failed",
                }
            )
            continue

        # Reuse is free of creation budget; only a missing match is suppressed here.
        if (
            max_issues_per_finish < 1
            or max_issues_per_key < 1
            or created_count >= max_issues_per_finish
            or created_by_key.get(stable_key, 0) >= max_issues_per_key
        ):
            reason = (
                "budget-exhausted:finish-limit-reached"
                if created_count >= max_issues_per_finish
                else "budget-exhausted:key-limit-reached"
            )
            _record_intake_event(
                state,
                "process-improvement-suppressed",
                actor,
                incident,
                stable_key,
                reason=reason,
            )
            results.append(
                {"status": "suppressed", "stableKey": stable_key, "reason": "budget-exhausted"}
            )
            continue

        body = render_sanitized_issue_body(consumer, VERSION, incident, state)
        title = f"{stable_key} {_public_summary(incident)}"
        try:
            record_url = create_issue(process_repo, title, body)
            if not _valid_issue_url(process_repo, record_url):
                raise ProcessError("tracker issue creation returned an invalid URL")
            created_count += 1
            created_by_key[stable_key] = created_by_key.get(stable_key, 0) + 1
            _record_intake_event(
                state,
                "process-improvement-created",
                actor,
                incident,
                stable_key,
                recordUrl=record_url,
            )
            results.append(
                {"status": "created", "stableKey": stable_key, "recordUrl": record_url}
            )
        except Exception:
            _record_intake_event(
                state,
                "process-improvement-failed",
                actor,
                incident,
                stable_key,
                stage="create",
                errorCode="tracker-create-failed",
            )
            results.append(
                {
                    "status": "failed",
                    "stableKey": stable_key,
                    "errorCode": "tracker-create-failed",
                }
            )

    return results


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
