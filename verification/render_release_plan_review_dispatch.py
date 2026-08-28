from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


EVENT_TYPE = "engineering-process-plan-review-required"
MAX_CLIENT_PAYLOAD_PROPERTIES = 10
MAX_EVENT_BYTES = 65_535
MAX_PLAN_DECISION_REVIEW_BYTES = 60_000
MAX_IDENTITY_CHARACTERS = 256
PLAN_REVIEW_PAYLOAD_FIELDS = {
    "artifact",
    "comparisonBase",
    "changeId",
    "commit",
    "continuationWorkflow",
    "maxPlanDecisionReviewBytes",
    "planDecisionReviewEncoding",
    "plannedRun",
    "repository",
    "reviewer",
}


class DispatchContractError(ValueError):
    pass


def _bounded_identity(value: str, label: str) -> str:
    if not value or len(value) > MAX_IDENTITY_CHARACTERS:
        raise DispatchContractError(
            f"{label} must contain between 1 and "
            f"{MAX_IDENTITY_CHARACTERS} characters"
        )
    if any(ord(character) < 0x20 for character in value):
        raise DispatchContractError(f"{label} contains a control character")
    return value


def _full_sha(value: str, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise DispatchContractError(
            f"{label} must be a full lowercase Git commit SHA"
        )
    return value


def _positive_decimal(value: str, label: str) -> str:
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise DispatchContractError(f"{label} must be a positive decimal integer")
    return value


def _positive_integer(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DispatchContractError(f"{label} must be a positive integer")
    return value


def _validate_event(document: dict[str, Any]) -> None:
    if set(document) != {"event_type", "client_payload"}:
        raise DispatchContractError("dispatch envelope has an unexpected contract")
    if document.get("event_type") != EVENT_TYPE:
        raise DispatchContractError("dispatch event type does not match")
    payload = document.get("client_payload")
    if not isinstance(payload, dict):
        raise DispatchContractError("client_payload must be an object")
    if len(payload) > MAX_CLIENT_PAYLOAD_PROPERTIES:
        raise DispatchContractError(
            "client_payload exceeds GitHub's top-level property limit"
        )
    if set(payload) != PLAN_REVIEW_PAYLOAD_FIELDS:
        raise DispatchContractError(
            "plan-review client_payload has an unexpected contract"
        )


def encode_event(document: dict[str, Any]) -> bytes:
    _validate_event(document)
    encoded = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise DispatchContractError("repository dispatch exceeds its byte limit")
    return encoded


def render_event(
    *,
    artifact: str,
    comparison_base: str,
    change_id: str,
    commit: str,
    continuation_workflow: str,
    max_plan_decision_review_bytes: int,
    plan_decision_review_encoding: str,
    repository: str,
    reviewer_actor: str,
    reviewer_context: str,
    planned_run_id: str,
    planned_run_attempt: int,
) -> dict[str, Any]:
    comparison_base = _full_sha(comparison_base, "comparison base")
    commit = _full_sha(commit, "candidate commit")
    if comparison_base == commit:
        raise DispatchContractError(
            "candidate commit must differ from its comparison base"
        )
    expected_artifact = f"planned-release-candidate-{commit}"
    if artifact != expected_artifact:
        raise DispatchContractError(
            "planned candidate artifact does not match the candidate commit"
        )
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", change_id) is None:
        raise DispatchContractError("change id is invalid")
    if continuation_workflow != "release-plan-approval.yml":
        raise DispatchContractError("continuation workflow is invalid")
    if plan_decision_review_encoding != "gzip+base64":
        raise DispatchContractError("plan review encoding is invalid")
    if (
        _positive_integer(
            max_plan_decision_review_bytes, "maximum plan review bytes"
        )
        > MAX_PLAN_DECISION_REVIEW_BYTES
    ):
        raise DispatchContractError("maximum plan review bytes exceeds its limit")
    if re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ) is None:
        raise DispatchContractError("repository identity is invalid")
    reviewer_actor = _bounded_identity(reviewer_actor, "reviewer actor")
    reviewer_context = _bounded_identity(reviewer_context, "reviewer context")
    planned_run_id = _positive_decimal(planned_run_id, "planned run id")
    planned_run_attempt = _positive_integer(
        planned_run_attempt, "planned run attempt"
    )
    document = {
        "event_type": EVENT_TYPE,
        "client_payload": {
            "artifact": artifact,
            "comparisonBase": comparison_base,
            "changeId": change_id,
            "commit": commit,
            "continuationWorkflow": continuation_workflow,
            "maxPlanDecisionReviewBytes": max_plan_decision_review_bytes,
            "planDecisionReviewEncoding": plan_decision_review_encoding,
            "plannedRun": {
                "id": planned_run_id,
                "attempt": planned_run_attempt,
            },
            "repository": repository,
            "reviewer": {
                "actorId": reviewer_actor,
                "contextId": reviewer_context,
                "kind": "agent",
            },
        },
    }
    encode_event(document)
    return document


def write_event(document: dict[str, Any], output: Path) -> int:
    encoded = encode_event(document)
    try:
        parent = output.parent
        parent_stat = parent.lstat()
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(
            parent_stat.st_mode
        ):
            raise DispatchContractError(
                "dispatch output parent must be a regular directory"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
    except DispatchContractError:
        raise
    except OSError as error:
        raise DispatchContractError(
            f"cannot write repository dispatch: {error}"
        ) from error
    return len(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render one bounded release plan-review repository dispatch"
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--comparison-base", required=True)
    parser.add_argument("--change-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--continuation-workflow", required=True)
    parser.add_argument("--max-plan-decision-review-bytes", type=int, required=True)
    parser.add_argument("--plan-decision-review-encoding", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--reviewer-actor", required=True)
    parser.add_argument("--reviewer-context", required=True)
    parser.add_argument("--planned-run-id", required=True)
    parser.add_argument("--planned-run-attempt", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        document = render_event(
            artifact=arguments.artifact,
            comparison_base=arguments.comparison_base,
            change_id=arguments.change_id,
            commit=arguments.commit,
            continuation_workflow=arguments.continuation_workflow,
            max_plan_decision_review_bytes=(
                arguments.max_plan_decision_review_bytes
            ),
            plan_decision_review_encoding=(
                arguments.plan_decision_review_encoding
            ),
            repository=arguments.repository,
            reviewer_actor=arguments.reviewer_actor,
            reviewer_context=arguments.reviewer_context,
            planned_run_id=arguments.planned_run_id,
            planned_run_attempt=arguments.planned_run_attempt,
        )
        size = write_event(document, arguments.output)
    except DispatchContractError as error:
        parser.error(str(error))
    print(f"rendered release plan-review dispatch: {size} bytes")


if __name__ == "__main__":
    main()
