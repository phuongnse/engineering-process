from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


MAX_CHECK_PAGES = 10
MAX_CHECKS_PER_PAGE = 100
MAX_CHECKS = MAX_CHECK_PAGES * MAX_CHECKS_PER_PAGE
MAX_INPUT_BYTES = 2_000_000


class ExclusivityError(RuntimeError):
    pass


def read_bounded_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ExclusivityError(f"{path}: must be a regular non-symlink file")
    content = path.read_bytes()
    if len(content) > MAX_INPUT_BYTES:
        raise ExclusivityError(f"{path}: exceeds {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExclusivityError(f"{path}: must contain UTF-8 JSON") from error


def bounded_check_runs(document: Any) -> list[dict[str, Any]]:
    pages = document if isinstance(document, list) else [document]
    if not 1 <= len(pages) <= MAX_CHECK_PAGES:
        raise ExclusivityError("check-run page count is missing or exceeds 10")
    checks: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or set(page) < {"check_runs"}:
            raise ExclusivityError("check-run page is invalid")
        page_checks = page["check_runs"]
        if not isinstance(page_checks, list) or len(page_checks) > MAX_CHECKS_PER_PAGE:
            raise ExclusivityError("check-run page exceeds 100 entries")
        if any(not isinstance(item, dict) for item in page_checks):
            raise ExclusivityError("check-run entry is invalid")
        checks.extend(page_checks)
    if len(checks) > MAX_CHECKS:
        raise ExclusivityError("check-run set exceeds 1000 entries")
    identifiers = [item.get("id") for item in checks]
    if any(not isinstance(item, int) or item < 1 for item in identifiers):
        raise ExclusivityError("check-run id is invalid")
    if len(identifiers) != len(set(identifiers)):
        raise ExclusivityError("check-run pages contain duplicate ids")
    return checks


def validate_exclusivity(
    document: Any,
    *,
    context: str,
    app_id: int,
    expected_count: int,
    head_sha: str,
    expected_check_id: int | None = None,
) -> list[dict[str, Any]]:
    if expected_count not in {0, 1}:
        raise ExclusivityError("expected count must be zero or one")
    matching = [
        item
        for item in bounded_check_runs(document)
        if item.get("name") == context
        and isinstance(item.get("app"), dict)
        and item["app"].get("id") == app_id
    ]
    if len(matching) != expected_count:
        raise ExclusivityError(
            f"expected {expected_count} exact App-owned checks, found {len(matching)}"
        )
    if expected_count == 1:
        item = matching[0]
        if (
            item.get("head_sha") != head_sha
            or item.get("status") != "completed"
            or item.get("conclusion") != "success"
            or (
                expected_check_id is not None
                and item.get("id") != expected_check_id
            )
        ):
            raise ExclusivityError("durable consumption check identity is invalid")
    return matching


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate bounded exclusive App-owned authority-transition checks"
    )
    parser.add_argument("--checks", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--app-id", type=int, required=True)
    parser.add_argument("--expected-count", type=int, choices=(0, 1), required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--expected-check-id", type=int)
    args = parser.parse_args(argv)
    matching = validate_exclusivity(
        read_bounded_json(args.checks),
        context=args.context,
        app_id=args.app_id,
        expected_count=args.expected_count,
        head_sha=args.head_sha,
        expected_check_id=args.expected_check_id,
    )
    print(
        json.dumps(
            {
                "context": args.context,
                "matchingCheckIds": [str(item["id"]) for item in matching],
                "pageBound": MAX_CHECK_PAGES,
                "status": "passed",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExclusivityError, OSError) as error:
        print(f"transition check exclusivity failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
