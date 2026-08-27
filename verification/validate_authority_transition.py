from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from engineering_process.transition import validate_bootstrap_transition_candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one bootstrap candidate with source-owned verifier code"
    )
    parser.add_argument("--controller-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--intent", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--target-checkout", type=Path, required=True)
    parser.add_argument("--target-process-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--release-receipt", type=Path, required=True)
    parser.add_argument("--artifact-attestation", type=Path, required=True)
    parser.add_argument("--target-repository-proof", type=Path, required=True)
    parser.add_argument("--protected-base", required=True)
    parser.add_argument("--validation-service", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    result = validate_bootstrap_transition_candidate(
        args.controller_root,
        args.candidate_root,
        policy_path=args.policy,
        intent_path=args.intent,
        authorization_path=args.authorization,
        target_checkout=args.target_checkout,
        target_process_root=args.target_process_root,
        artifact_root=args.artifact_root,
        release_receipt_path=args.release_receipt,
        artifact_attestation_path=args.artifact_attestation,
        repository_proof_path=args.target_repository_proof,
        protected_base_ref=args.protected_base,
        validation_service_path=args.validation_service,
    )
    if os.path.lexists(args.output):
        raise ContractError(f"{args.output}: refusing to replace validation output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ContractError(f"cannot preserve authority-transition validation: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"authority-transition validation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
