"""Issue record generation from one consumer-selected standard."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .artifact_standards import ArtifactStandard, MAX_DOCUMENT_BYTES
from .contracts import ProcessError, validate_document
from .distribution import distribution_root, schemas_root


def _record_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        return False


def _validate_field(field: dict[str, Any], value: str) -> None:
    format_name = field.get("format", "text")
    if format_name == "record-url" and not _record_url(value):
        raise ProcessError(f"issue field {field['label']} requires one durable HTTPS URL")
    if format_name == "record-references":
        references = value.split(", ")
        if value.casefold() != "none" and (
            not references or any(not _record_url(reference) for reference in references)
        ):
            raise ProcessError(
                f"issue field {field['label']} requires none or comma-separated durable HTTPS URLs"
            )


def render_issue(
    standard: ArtifactStandard,
    data: dict[str, Any],
    *,
    state: str = "open",
    process_root: Path | None = None,
) -> str:
    if standard.document["adapter"] != "issue":
        raise ProcessError("selected standard requires a different issue adapter")
    if state not in {"open", "closed"}:
        raise ProcessError("issue state must be open or closed")
    validate_document(
        data,
        "issue-data",
        schema_root=schemas_root(distribution_root(process_root)),
    )
    title_rules = standard.rules["title"]
    title = data["title"]
    if len(title) > title_rules["maxLength"]:
        raise ProcessError("issue title exceeds the selected length limit")
    if prefix := title_rules.get("prefix"):
        if not title.startswith(prefix):
            raise ProcessError(f"issue title must start with {prefix}")
    title_value = title[len(prefix):] if prefix else title
    if standard.unresolved(title_value):
        raise ProcessError(f"{state} issue has unresolved title")

    sections = standard.rules["states"][state]["sections"]
    fields = [field for section in sections for field in section["fields"]]
    checks = [check for section in sections for check in section["checks"]]
    expected_fields = {field["id"] for field in fields}
    expected_checks = {check["id"] for check in checks}
    unknown_fields = sorted(data["fields"].keys() - expected_fields)
    unknown_checks = sorted(data["checks"].keys() - expected_checks)
    if unknown_fields:
        raise ProcessError("issue data contains unknown fields: " + ", ".join(unknown_fields))
    if unknown_checks:
        raise ProcessError("issue data contains unknown checks: " + ", ".join(unknown_checks))

    lines = [f"# {title}", ""]
    for section in sections:
        lines.extend([section["heading"], ""])
        for field in section["fields"]:
            value = data["fields"].get(field["id"], standard.document["pendingValues"][0])
            if standard.unresolved(value):
                raise ProcessError(f"{state} issue has unresolved value for {field['label']}")
            _validate_field(field, value)
            lines.extend([f"### {field['label']}", "", value, ""])
        for check in section["checks"]:
            completed = data["checks"].get(check["id"], False)
            if state == "closed" and not completed:
                raise ProcessError(f"closed issue has unchecked item: {check['label']}")
            lines.append(f"- [{'x' if completed else ' '}] {check['label']}")
        if section["checks"]:
            lines.append("")
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ProcessError("issue record exceeds its size limit")
    return body
