"""JSON contracts used by the public CLI.

JSON Schema owns document shape. This module intentionally contains only bounded
I/O, canonical hashing, and schema dispatch; cross-document lifecycle relations live
next to the state machine that enforces them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


MAX_JSON_BYTES = 2_000_000
CONTRACT_KINDS = (
    "change",
    "plan",
    "process-graph",
    "process-lock",
    "project",
    "project-legacy",
    "receipt",
    "release-change",
    "release",
    "review",
    "run",
)


class ProcessError(RuntimeError):
    """A deterministic, user-facing process failure."""


def read_json(path: Path, *, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ProcessError(f"cannot read {path}: {error}") from error
    if size > maximum_bytes:
        raise ProcessError(f"{path} exceeds {maximum_bytes} bytes")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProcessError(f"{path} is not valid UTF-8 JSON: {error}") from error


def canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ProcessError(f"value is not canonical JSON: {error}") from error
    return encoded.encode("utf-8")


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    """Write canonical human-readable JSON without exposing a partial file."""
    data = (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProcessError(f"cannot write {path}: {error}") from error


def validate_document(
    document: Any,
    kind: str,
    *,
    schema_root: Path,
    source: str = "document",
) -> Any:
    if kind not in CONTRACT_KINDS:
        raise ProcessError(f"unknown contract kind: {kind}")
    schema_path = schema_root / f"{kind}.schema.json"
    schema = read_json(schema_path)
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        rendered: list[str] = []
        for error in errors[:20]:
            location = ".".join(str(part) for part in error.absolute_path)
            rendered.append(
                f"{source}{'.' + location if location else ''}: {error.message}"
            )
        if len(errors) > 20:
            rendered.append(f"{source}: {len(errors) - 20} more schema errors")
        raise ProcessError("\n".join(rendered))
    return document


def load_and_validate(
    path: Path,
    kind: str,
    *,
    schema_root: Path,
) -> Any:
    return validate_document(
        read_json(path), kind, schema_root=schema_root, source=str(path)
    )
