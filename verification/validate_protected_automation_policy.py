from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


MAX_POLICY_BYTES = 65_536
EXPECTED_ACTIONS = [
    "adopt",
    "commit",
    "deploy",
    "ephemeral-cleanup",
    "merge",
    "publish",
    "push",
    "release",
    "review-object",
]
EXPECTED_ESCALATION_REASONS = [
    "bounded-recovery-exhausted",
    "capability-unavailable",
    "decision-required",
]
REQUIRED_MERGE_GATES = [
    "requireCompletedLifecycle",
    "requireCurrentBase",
    "requireExactHead",
    "requireIndependentReview",
    "requireRequiredChecks",
]


class PolicyError(ValueError):
    pass


def _read_policy(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_POLICY_BYTES + 1)
    except OSError as error:
        raise PolicyError(f"cannot read protected automation policy: {error}") from error
    if len(content) > MAX_POLICY_BYTES:
        raise PolicyError(
            f"protected automation policy exceeds {MAX_POLICY_BYTES} bytes"
        )

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PolicyError(
                    f"protected automation policy contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PolicyError(f"invalid protected automation policy JSON: {error}") from error


def validate_standing_automation_policy(document: object) -> str:
    if not isinstance(document, dict):
        raise PolicyError("protected automation policy must be an object")
    required = {
        "schemaVersion",
        "kind",
        "enabled",
        "confirmationMode",
        "actions",
        "merge",
        "escalationReasons",
    }
    allowed = required | {"$schema"}
    keys = set(document)
    if keys - allowed:
        raise PolicyError("protected automation policy contains unknown fields")
    if required - keys:
        raise PolicyError("protected automation policy is missing required fields")
    if "$schema" in document and not isinstance(document["$schema"], str):
        raise PolicyError("protected automation policy $schema must be a string")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise PolicyError("protected automation policy schemaVersion must be 1")
    if document["kind"] != "engineering-process-standing-automation-policy":
        raise PolicyError("protected automation policy kind is invalid")
    if document["enabled"] is not True:
        raise PolicyError("protected automation policy must be enabled")
    if document["confirmationMode"] != "exceptions-only":
        raise PolicyError(
            "protected automation policy confirmationMode must be exceptions-only"
        )
    if document["actions"] != EXPECTED_ACTIONS:
        raise PolicyError(
            "protected automation policy must contain the complete ordered action set"
        )

    merge = document["merge"]
    if not isinstance(merge, dict):
        raise PolicyError("protected automation policy merge must be an object")
    expected_merge_keys = {"method", *REQUIRED_MERGE_GATES}
    if set(merge) != expected_merge_keys:
        raise PolicyError(
            "protected automation policy merge must contain exactly the required gates"
        )
    method = merge["method"]
    if method not in {"merge", "rebase", "squash"}:
        raise PolicyError("protected automation policy merge method is invalid")
    for gate in REQUIRED_MERGE_GATES:
        if merge[gate] is not True:
            raise PolicyError(f"protected automation policy merge gate {gate} must be true")

    if document["escalationReasons"] != EXPECTED_ESCALATION_REASONS:
        raise PolicyError(
            "protected automation policy must contain only the complete ordered "
            "exceptions-only reason set"
        )
    return method


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        method = validate_standing_automation_policy(_read_policy(arguments.policy))
    except PolicyError as error:
        parser.error(str(error))
    json.dump(
        {"mergeMethod": method, "status": "valid"},
        sys.stdout,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
