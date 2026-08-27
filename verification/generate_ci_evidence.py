from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import jsonschema


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from engineering_process.supplemental import (
    build_supplemental_verification,
    write_supplemental_bundle,
)


def _validator(path: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate bounded supplemental CI verification evidence"
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-checkpoint", required=True)
    parser.add_argument("--comparison-base", required=True)
    parser.add_argument("--producer-actor", required=True)
    parser.add_argument("--producer-context", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--runner-os", required=True)
    parser.add_argument("--runner-arch", required=True)
    parser.add_argument("--triggered-by", required=True)
    parser.add_argument("--remote-request", type=Path)
    args = parser.parse_args(argv)

    authority_transition = None
    if args.remote_request is not None:
        request = json.loads(args.remote_request.read_text(encoding="utf-8"))
        authority_transition = request.get("authorityTransition")

    manifest, reports = build_supplemental_verification(
        args.project_root,
        expected_checkpoint=args.expected_checkpoint,
        comparison_base=args.comparison_base,
        producer_actor=args.producer_actor,
        producer_context=args.producer_context,
        provider=args.provider,
        repository=args.repository,
        event_name=args.event_name,
        workflow_name=args.workflow_name,
        workflow_ref=args.workflow_ref,
        workflow_sha=args.workflow_sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        job=args.job,
        run_url=args.run_url,
        runner_os=args.runner_os,
        runner_arch=args.runner_arch,
        triggered_by=args.triggered_by,
        authority_transition=authority_transition,
    )
    schema_root = args.project_root / "schemas"
    verification_validator = _validator(
        schema_root / "verification.schema.json"
    )
    for report in reports.values():
        verification_validator.validate(report)
    supplemental_validator = _validator(
        schema_root / "supplemental-verification.schema.json"
    )
    supplemental_validator.validate(manifest)
    output = write_supplemental_bundle(
        args.project_root,
        args.output_root,
        manifest,
        reports,
    )
    print(
        json.dumps(
            {
                "checkpoint": manifest["checkpoint"],
                "output": str(output),
                "profiles": [
                    entry["profile"] for entry in manifest["reports"]
                ],
                "status": manifest["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, jsonschema.ValidationError) as error:
        print(f"CI evidence generation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
