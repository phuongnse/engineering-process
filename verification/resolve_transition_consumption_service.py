from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from verification.validate_transition_check_exclusivity import (
    ExclusivityError,
    bounded_check_runs,
)


MAX_SERVICE_INPUT_BYTES = 2_000_000


class ServiceError(RuntimeError):
    pass


def _read_bounded(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ServiceError(f"{path}: must be a regular non-symlink file")
    content = path.read_bytes()
    if len(content) > MAX_SERVICE_INPUT_BYTES:
        raise ServiceError(f"{path}: exceeds {MAX_SERVICE_INPUT_BYTES} bytes")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(f"{path}: must contain UTF-8 JSON") from error
    return value


def resolve(
    validation: dict[str, Any],
    run: dict[str, Any],
    checks: Any,
) -> dict[str, Any]:
    if not isinstance(validation, dict) or not isinstance(run, dict):
        raise ServiceError("validation and run inputs must contain JSON objects")
    expected = validation.get("validationService")
    if not isinstance(expected, dict):
        raise ServiceError("validation artifact has no bound service identity")
    expected_run = int(expected["runId"])
    repository = run.get("repository")
    if not isinstance(repository, dict):
        raise ServiceError("validation run repository is missing")
    if (
        run.get("id") != expected_run
        or run.get("run_attempt") != expected["runAttempt"]
        or repository.get("full_name") != expected["repository"]
        or run.get("path") != expected["workflowPath"]
        or run.get("head_sha") != expected["headSha"]
        or run.get("event") != expected["event"]
    ):
        raise ServiceError("validation run does not match the protected service identity")
    status = run.get("status")
    conclusion = run.get("conclusion")
    if not (
        (status == "in_progress" and conclusion is None)
        or (status == "completed" and conclusion == "success")
    ):
        raise ServiceError("validation run is not successful or active")
    try:
        check_runs = bounded_check_runs(checks)
    except ExclusivityError as error:
        raise ServiceError(str(error)) from error
    matching = [
        item
        for item in check_runs
        if isinstance(item, dict)
        and item.get("name") == expected["checkContext"]
        and item.get("head_sha") == validation.get("headCheckpoint")
        and item.get("conclusion") == "success"
        and isinstance(item.get("app"), dict)
        and item["app"].get("id") == expected["checkAppId"]
    ]
    if len(matching) != 1:
        raise ServiceError("exactly one policy-authenticated completion check is required")
    check = matching[0]
    if not isinstance(check.get("id"), int) or check["id"] < 1:
        raise ServiceError("completion check id is invalid")
    return {
        "schemaVersion": 1,
        "kind": "engineering-process-transition-consumption-service",
        **{
            key: expected[key]
            for key in (
                "repository",
                "workflowPath",
                "workflowSha",
                "runId",
                "runAttempt",
                "runUrl",
                "event",
                "headSha",
                "checkContext",
                "checkAppId",
            )
        },
        "runStatus": status,
        "runConclusion": conclusion,
        "checkRunId": str(check["id"]),
        "checkHeadSha": check["head_sha"],
        "checkConclusion": "success",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the authenticated service chain for terminal transition consumption"
    )
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--checks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = resolve(
        _read_bounded(args.validation),
        _read_bounded(args.run),
        _read_bounded(args.checks),
    )
    if os.path.lexists(args.output):
        raise ServiceError(f"{args.output}: refusing to replace service evidence")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ServiceError, TypeError, ValueError) as error:
        print(f"transition consumption service failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
