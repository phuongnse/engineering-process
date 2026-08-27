from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import (
    ContractError,
    MAX_JSON_BYTES,
    canonical_json_digest,
    validate_plan_decision_review_assignment,
)
from engineering_process.lifecycle import review_context_reservation


RESERVATION_FIELDS = {
    "actorId",
    "changeId",
    "contextDigest",
    "cycle",
    "kind",
    "reservedAt",
    "schemaVersion",
}


def _stable_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ContractError(f"{label} must be a regular non-symlink file")
        if before.st_size > MAX_JSON_BYTES:
            raise ContractError(f"{label} exceeds its byte limit")
        content = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise ContractError(f"cannot read {label}: {error}") from error
    if (
        len(content) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_mode != before.st_mode
    ):
        raise ContractError(f"{label} changed while reading")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ContractError(f"{label} must be a JSON object")
    canonical = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if content != canonical:
        raise ContractError(f"{label} must use canonical lifecycle JSON bytes")
    return document, content


def _reservation_name(context_id: str) -> str:
    return hashlib.sha256(context_id.encode("utf-8")).hexdigest() + ".json"


def _validate_binding(
    assignment: dict[str, Any], reservation: dict[str, Any]
) -> str:
    validate_plan_decision_review_assignment(assignment, "plan decision assignment")
    reviewer = assignment["reviewer"]
    context_id = reviewer["contextId"]
    name = _reservation_name(context_id)
    expected_context_digest = f"sha256:{name[:-5]}"
    if set(reservation) != RESERVATION_FIELDS or reservation.get("schemaVersion") != 1:
        raise ContractError("review context reservation has an unexpected contract")
    if (
        reservation.get("contextDigest") != expected_context_digest
        or reservation.get("actorId") != reviewer["actorId"]
        or reservation.get("kind") != reviewer["kind"]
        or reservation.get("changeId") != assignment["changeId"]
        or reservation.get("cycle") != assignment["cycle"]
        or canonical_json_digest(reservation)
        != assignment["contextReservationSha256"]
    ):
        raise ContractError(
            "review context reservation does not match its plan decision assignment"
        )
    return name


def export_reservation(
    project_root: Path, assignment_path: Path, output_root: Path
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    assignment, _assignment_bytes = _stable_document(
        assignment_path, label="plan decision assignment"
    )
    reviewer = assignment.get("reviewer")
    if not isinstance(reviewer, dict) or not isinstance(
        reviewer.get("contextId"), str
    ):
        raise ContractError("plan decision assignment reviewer is invalid")
    source = (
        project_root
        / ".process"
        / "runs"
        / ".review-contexts"
        / _reservation_name(reviewer["contextId"])
    )
    reservation, content = _stable_document(
        source, label="review context reservation"
    )
    name = _validate_binding(assignment, reservation)
    try:
        output_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        destination = output_root / name
        with destination.open("xb") as stream:
            stream.write(content)
    except OSError as error:
        raise ContractError(f"cannot export review context reservation: {error}") from error
    return {
        "status": "passed",
        "operation": "export",
        "file": name,
        "contextReservationSha256": assignment["contextReservationSha256"],
    }


def restore_reservation(
    project_root: Path, assignment_path: Path, input_root: Path
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    assignment, _assignment_bytes = _stable_document(
        assignment_path, label="plan decision assignment"
    )
    reviewer = assignment.get("reviewer")
    if not isinstance(reviewer, dict) or not isinstance(
        reviewer.get("contextId"), str
    ):
        raise ContractError("plan decision assignment reviewer is invalid")
    expected_name = _reservation_name(reviewer["contextId"])
    try:
        input_stat = input_root.lstat()
        if stat.S_ISLNK(input_stat.st_mode) or not stat.S_ISDIR(input_stat.st_mode):
            raise ContractError("reservation handoff must be a regular directory")
        entries = list(input_root.iterdir())
    except OSError as error:
        raise ContractError(f"cannot inspect reservation handoff: {error}") from error
    if len(entries) != 1 or entries[0].name != expected_name:
        raise ContractError("reservation handoff must contain exactly the assigned file")
    reservation, content = _stable_document(
        entries[0], label="handoff review context reservation"
    )
    name = _validate_binding(assignment, reservation)
    registry = project_root / ".process" / "runs" / ".review-contexts"
    try:
        if registry.exists():
            registry_stat = registry.lstat()
            if stat.S_ISLNK(registry_stat.st_mode) or not stat.S_ISDIR(
                registry_stat.st_mode
            ):
                raise ContractError("review context registry must be a regular directory")
            if any(registry.iterdir()):
                raise ContractError("review context registry must be empty before restore")
        else:
            registry.mkdir(mode=0o700, parents=False, exist_ok=False)
        destination = registry / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    except ContractError:
        raise
    except OSError as error:
        raise ContractError(f"cannot restore review context reservation: {error}") from error
    restored = review_context_reservation(project_root, reviewer["contextId"])
    _validate_binding(assignment, restored)
    return {
        "status": "passed",
        "operation": "restore",
        "file": name,
        "contextReservationSha256": assignment["contextReservationSha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("export", "restore"):
        command = subparsers.add_parser(operation)
        command.add_argument("--project-root", type=Path, required=True)
        command.add_argument("--assignment", type=Path, required=True)
        if operation == "export":
            command.add_argument("--output-root", type=Path, required=True)
        else:
            command.add_argument("--input-root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.operation == "export":
            result = export_reservation(
                arguments.project_root, arguments.assignment, arguments.output_root
            )
        else:
            result = restore_reservation(
                arguments.project_root, arguments.assignment, arguments.input_root
            )
    except (ContractError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
