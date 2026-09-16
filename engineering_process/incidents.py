"""Automated bounded process-improvement incident intake for change finish."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from .contracts import ProcessError
from . import VERSION
from .project import load_project
from .repository import _git

DEFAULT_MAX_ISSUES_PER_FINISH = 3
DEFAULT_MAX_ISSUES_PER_KEY = 1
DEFAULT_PROCESS_REPO = "phuongnse/engineering-process"

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


def stable_title_key(consumer: str, process_version: str, invariant: str, kind: str) -> str:
    """Format the stable non-sensitive deduplication title key."""
    clean_consumer = re.sub(r"[^a-zA-Z0-9_.-]", "-", consumer).strip("-")
    clean_invariant = re.sub(r"[^a-zA-Z0-9_.-]", "-", invariant).strip("-")
    clean_kind = re.sub(r"[^a-zA-Z0-9_.-]", "-", kind).strip("-")
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


def resolve_process_repo(project: dict[str, Any]) -> str:
    """Resolve the destination repository for process issues."""
    prefix = (
        project.get("lifecycle", {})
        .get("processChanges", {})
        .get("acceptedIssueUrlPrefix", "")
    )
    match = re.search(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/issues/?", prefix)
    if match:
        return match.group(1)
    return DEFAULT_PROCESS_REPO


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


def _default_search_tracker(repo: str, search_query: str) -> list[dict[str, Any]]:
    """Execute gh issue list to find existing matching issues."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--search",
                f"{search_query} in:title",
                "--json",
                "number,title,url,state",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return []


def _default_create_issue(repo: str, title: str, body: str) -> str:
    """Execute gh issue create and return the created permanent issue URL."""
    try:
        result = subprocess.run(
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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        detail = result.stderr.strip()
        raise ProcessError(f"gh issue create failed: {detail}")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessError(f"cannot create issue via gh: {error}") from error


def render_sanitized_issue_body(
    consumer: str,
    process_version: str,
    incident: Incident,
    state: dict[str, Any],
) -> str:
    """Render a sanitized issue body complying with public disclosure standards."""
    clean_details = {
        k: v
        for k, v in incident.details.items()
        if not any(secret in k.lower() for secret in ("token", "secret", "key", "pass", "cred"))
    }
    details_rendered = json.dumps(clean_details, indent=2, sort_keys=True)

    return f"""## Consumer evidence

Consumer: `{consumer}`
Process authority: engineering-process `{process_version}`
Incident kind: `{incident.kind}`
Invariant: `{incident.invariant}`

Observed behavior:
{incident.summary}

### Structured incident details

```json
{details_rendered}
```

## Expected invariant

The shared process must maintain the `{incident.invariant}` invariant under the `{incident.kind}` boundary without unhandled errors, dropped evidence, or unexpected operational thrashing.

## Scope

Shared process guidance and enforcement for `{incident.invariant}`. Product behavior stays in the consumer repository; this report is an evidence handoff only.
"""


def is_process_producer_change(state: dict[str, Any]) -> bool:
    """Check if the current change is modifying the shared process distribution itself."""
    contract = state.get("contract", {}).get("document", {})
    affected = contract.get("affectedProjects", [])
    source = contract.get("source", "")
    return (
        "engineering-process" in affected
        or "phuongnse/engineering-process" in source
    )


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
) -> list[dict[str, Any]]:
    """Collect, evaluate, deduplicate, and record process improvement incidents before finish."""
    incidents = collect_incidents(project_root, process_root, state)
    if not incidents:
        return []

    project = load_project(project_root, process_root)
    consumer = resolve_consumer_identity(project_root, project)
    process_repo = resolve_process_repo(project)
    search_tracker = search_tracker_fn if search_tracker_fn is not None else _default_search_tracker
    create_issue = create_issue_fn if create_issue_fn is not None else _default_create_issue

    is_producer = is_process_producer_change(state)
    created_count = 0
    results: list[dict[str, Any]] = []

    for incident in incidents:
        # Record incident collection event
        state["history"].append(
            {
                "event": "incident-collected",
                "at": _now_iso(),
                "actor": actor,
                "details": {
                    "kind": incident.kind,
                    "invariant": incident.invariant,
                    "summary": incident.summary,
                    "severity": incident.severity,
                },
            }
        )

        stable_key = stable_title_key(consumer, VERSION, incident.invariant, incident.kind)
        search_sub = f"[{incident.invariant}][{incident.kind}]"

        # 1. Tracker search and deduplication across all issue states
        matches = search_tracker(process_repo, search_sub)
        exact_match = next((item for item in matches if search_sub in item.get("title", "")), None)

        if exact_match:
            record_url = exact_match.get("url")
            state["history"].append(
                {
                    "event": "process-improvement-reused",
                    "at": _now_iso(),
                    "actor": actor,
                    "details": {
                        "stableKey": stable_key,
                        "recordUrl": record_url,
                        "kind": incident.kind,
                        "invariant": incident.invariant,
                        "state": exact_match.get("state"),
                    },
                }
            )
            results.append({"status": "reused", "stableKey": stable_key, "recordUrl": record_url})
            continue

        # 2. Recursion breaker: process-producer changes cannot autonomously create recursive process issues
        if is_producer:
            state["history"].append(
                {
                    "event": "process-improvement-suppressed",
                    "at": _now_iso(),
                    "actor": actor,
                    "details": {
                        "stableKey": stable_key,
                        "kind": incident.kind,
                        "invariant": incident.invariant,
                        "reason": "recursion-breaker:process-producer-change",
                    },
                }
            )
            results.append({"status": "suppressed", "stableKey": stable_key, "reason": "recursion-breaker"})
            continue

        # 3. Finite creation budget check
        if created_count >= max_issues_per_finish:
            state["history"].append(
                {
                    "event": "process-improvement-suppressed",
                    "at": _now_iso(),
                    "actor": actor,
                    "details": {
                        "stableKey": stable_key,
                        "kind": incident.kind,
                        "invariant": incident.invariant,
                        "reason": "budget-exhausted:finish-limit-reached",
                    },
                }
            )
            results.append({"status": "suppressed", "stableKey": stable_key, "reason": "budget-exhausted"})
            continue

        # 4. Render sanitized issue and create
        body = render_sanitized_issue_body(consumer, VERSION, incident, state)
        title = f"{stable_key} {incident.summary}"
        try:
            record_url = create_issue(process_repo, title, body)
            created_count += 1
            state["history"].append(
                {
                    "event": "process-improvement-created",
                    "at": _now_iso(),
                    "actor": actor,
                    "details": {
                        "stableKey": stable_key,
                        "recordUrl": record_url,
                        "kind": incident.kind,
                        "invariant": incident.invariant,
                    },
                }
            )
            results.append({"status": "created", "stableKey": stable_key, "recordUrl": record_url})
        except Exception as error:
            state["history"].append(
                {
                    "event": "process-improvement-failed",
                    "at": _now_iso(),
                    "actor": actor,
                    "details": {
                        "stableKey": stable_key,
                        "kind": incident.kind,
                        "invariant": incident.invariant,
                        "error": str(error),
                    },
                }
            )
            results.append({"status": "failed", "stableKey": stable_key, "error": str(error)})

    return results


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
