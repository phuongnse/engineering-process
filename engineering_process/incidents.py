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
from . import VERSION
from .project import load_project
from .repository import _git

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
        elif key in {"cycle", "verificationCount", "cycleCount"}:
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

    # 1. Evidence-integrity: explicit evidence-invalidated events
    for event in history:
        if event.get("event") == "evidence-invalidated":
            details = event.get("details", {})
            profile = details.get("profile", "unknown-profile")
            reason = details.get("reason", "input-digest-mismatch")
            _add(
                Incident(
                    kind="evidence-integrity",
                    invariant=profile,
                    summary=f"Verification report for '{profile}' was invalidated due to {reason}",
                    details=details,
                    severity="high",
                )
            )

    # 1c. Publication-boundary: callers may persist a structured preflight failure
    # before returning the lifecycle operation's error. Never infer one from text.
    for event in history:
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

    # 1b. Evidence-integrity: redundant profile-verified events within the same cycle
    verified_by_cycle: dict[int, dict[str, int]] = {}
    for event in history:
        if event.get("event") == "profile-verified":
            details = event.get("details", {})
            profile = details.get("profile")
            if profile:
                cycle = event.get("actor", {}).get("cycle") or state.get("cycle", 1)
                verified_by_cycle.setdefault(cycle, {})
                verified_by_cycle[cycle][profile] = verified_by_cycle[cycle].get(profile, 0) + 1

    for cycle, counts in verified_by_cycle.items():
        for profile, count in counts.items():
            if count > 1:
                _add(
                    Incident(
                        kind="evidence-integrity",
                        invariant="verification-repetition",
                        summary=f"Profile '{profile}' required {count} passing verification runs in cycle {cycle}",
                        details={"profile": profile, "cycle": cycle, "verificationCount": count},
                        severity="medium",
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

    # 3. Governance-thrashing: multiple correction cycles or reviewer replacement
    if state.get("cycle", 1) >= 2:
        _add(
            Incident(
                kind="governance-thrashing",
                invariant="excessive-review-cycles",
                summary=f"Change required {state['cycle']} correction cycles before approval",
                details={"cycleCount": state["cycle"]},
                severity="medium",
            )
        )

    for event in history:
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
    """Run a tracker command with bounded stdout, time, and cleanup."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ProcessError(failure_message) from error
    assert process.stdout is not None

    output = bytearray()
    overflow = threading.Event()
    stream_error = threading.Event()

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

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + 30
    timed_out = False
    while reader.is_alive():
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)

    if overflow.is_set() or timed_out:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            timed_out = True
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

    reader.join(timeout=2)
    if reader.is_alive():
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
