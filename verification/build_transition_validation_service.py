from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError, read_json
from engineering_process.transition import (
    _validate_transition_validation_service,
    require_protected_transition_policy_semantics,
    validate_protected_transition_policy,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the policy-bound identity of a protected transition validation run"
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    policy = validate_protected_transition_policy(
        read_json(args.policy), str(args.policy)
    )
    require_protected_transition_policy_semantics(policy)
    service = _validate_transition_validation_service(
        {
            "schemaVersion": 1,
            "kind": "engineering-process-transition-validation-service",
            "repository": args.repository,
            "workflowPath": policy["workflow"]["path"],
            "workflowSha": args.workflow_sha,
            "runId": args.run_id,
            "runAttempt": args.run_attempt,
            "runUrl": args.run_url,
            "event": args.event,
            "headSha": args.head_sha,
            "checkContext": policy["workflow"]["checkContext"],
            "checkAppId": policy["workflow"]["checkAppId"],
        },
        policy=policy,
        protected_base=args.workflow_sha,
    )
    if os.path.lexists(args.output):
        raise ContractError(f"{args.output}: refusing to replace validation service")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(service, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(service, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError) as error:
        print(f"transition validation service failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
