from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .contracts import ContractError


INTERNAL_PHASES = frozenset(
    {
        "specified",
        "planned",
        "implementing",
        "improvement-required",
        "improvement-pending",
        "verified",
        "review-pending",
        "changes-requested",
        "approved",
        "completed",
    }
)
EXTERNAL_STATES = frozenset({"unregistered", "awaiting-human-merge"})


def _routes(
    value: dict[str, dict[str, str | None]],
) -> Mapping[str, Mapping[str, str | None]]:
    return MappingProxyType(
        {
            phase: MappingProxyType(dict(transitions))
            for phase, transitions in value.items()
        }
    )


LIFECYCLE_ROUTE_TARGETS = _routes(
    {
        "unregistered": {
            "failure": "unregistered",
            "success": "specified",
        },
        "specified": {
            "failure": "specified",
            "success": "planned",
        },
        "planned": {
            "failure": "planned",
            "success": "implementing",
        },
        "implementing": {
            "all-required-passed": "verified",
            "failure": "improvement-required",
            "implementation-continued": "implementing",
            "profile-passed": "implementing",
        },
        "improvement-required": {
            "blocked-classified": "improvement-required",
            "failure": "improvement-required",
            "local-classified": "implementing",
            "review-classified": "changes-requested",
            "shared-classified": "improvement-pending",
        },
        "improvement-pending": {
            "chain-closed": "implementing",
            "failure": "improvement-pending",
            "producer-rejected": "improvement-required",
            "review-chain-closed": "changes-requested",
        },
        "verified": {
            "failure": "verified",
            "reviewer-assigned": "review-pending",
            "source-changed": "implementing",
        },
        "review-pending": {
            "approved": "approved",
            "changes-requested": "improvement-required",
            "failure": "review-pending",
        },
        "changes-requested": {
            "failure": "changes-requested",
            "success": "implementing",
        },
        "approved": {
            "failure": "approved",
            "success": "completed",
        },
        "completed": {
            "failure": "completed",
            "published": "awaiting-human-merge",
        },
        "awaiting-human-merge": {
            "merged": None,
            "not-merged": "awaiting-human-merge",
        },
    }
)


def lifecycle_next_state(phase: str, result: str) -> str | None:
    transitions = LIFECYCLE_ROUTE_TARGETS.get(phase)
    if transitions is None or result not in transitions:
        raise ContractError(
            f"lifecycle transition {phase!r} with result {result!r} is not declared"
        )
    return transitions[result]
