"""Render reviewed consumer release records using their selected document standard."""

from __future__ import annotations

import html
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from .artifact_standards import ArtifactStandard, MAX_DOCUMENT_BYTES
from .contracts import ProcessError, validate_document
from .distribution import distribution_root, schemas_root


def _text(value: str) -> str:
    """Render record text as a safe, readable Markdown inline value."""
    value = " ".join(value.split())
    # Details are metadata, not consumer-authored Markdown. Escape only syntax
    # that can change the inline meaning, while leaving ordinary punctuation
    # readable. HTML-sensitive text is entity-escaped so `&copy;` stays literal.
    value = html.escape(value, quote=False)
    value = value.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "~"):
        value = value.replace(character, "\\" + character)
    if value.startswith(("-", "+", "#")):
        value = "\\" + value
    return value


def _reference(source: str, repository: str | None) -> str:
    source = source.strip()
    if source.startswith(("https://", "http://")):
        issue = re.fullmatch(re.escape(repository) + r"/(?:issues|pull)/([1-9][0-9]*)", source) if repository else None
        label = f"#{issue.group(1)}" if issue else "Source"
        return f"[{label}]({quote(source, safe=':/?#&=%@+~')})"
    # Code spans suppress invented GitHub issue, mention and commit links.
    source = " ".join(source.split())
    fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", source)), default=0))
    if len(fence) == 1:
        return f"{fence}{source}{fence}"
    return f"{fence} {source} {fence}"


def render_notes(
    standard: ArtifactStandard,
    data: dict[str, Any],
    *,
    state: str = "ready",
    process_root: Path | None = None,
) -> str:
    if standard.document["adapter"] != "release-notes":
        raise ProcessError("selected standard requires a different release adapter")
    validate_document(data, "release-notes-data", schema_root=schemas_root(distribution_root(process_root)))
    if state not in {"draft", "ready"}:
        raise ProcessError("release notes state must be draft or ready")
    expected_sections = {section["id"] for section in standard.rules["sections"]}
    if set(data["sections"]) != expected_sections:
        raise ProcessError("release data sections must exactly match the selected standard")
    supported_types = {group["type"] for group in standard.rules["groups"]}
    unknown_types = sorted({change["type"] for change in data["changes"]} - supported_types)
    if unknown_types:
        raise ProcessError("release contains unsupported change types: " + ", ".join(unknown_types))
    values = [data["title"], data["introduction"], *data["sections"].values()]
    values.extend(change[key] for change in data["changes"] for key in ("summary", "source"))
    for change in data["changes"]:
        details = change["details"]
        values.extend(
            details[field]
            for field in ("problem", "changes", "apply", "compatibility", "notes")
        )
    if "footer" in data:
        values.append(data["footer"])
    if state == "ready" and any(standard.unresolved(value) for value in values):
        raise ProcessError("ready release notes contain an unresolved value")

    lines = [f"# {data['title']}", "", data["introduction"], ""]
    for group in standard.rules["groups"]:
        changes = [change for change in data["changes"] if change["type"] == group["type"]]
        if not changes:
            continue
        lines.extend([f"## {group['heading']}", ""])
        for change in changes:
            reference = _reference(
                change["source"], data.get("repositoryUrl")
            )
            details = change["details"]
            lines.append(f"- **{_text(change['summary'])}** ({reference})")
            lines.extend([
                f"  - **Problem:** {_text(details['problem'])}",
                f"  - **What changed:** {_text(details['changes'])}",
                "  - **Where:** " + ", ".join(
                    _reference(path, None) for path in details["affectedPaths"]
                ),
                f"  - **Apply:** {_text(details['apply'])}",
                f"  - **Compatibility:** {_text(details['compatibility'])}",
                f"  - **Notes:** {_text(details['notes'])}",
            ])
        lines.append("")
    for section in standard.rules["sections"]:
        lines.extend([f"## {section['heading']}", "", data["sections"][section["id"]], ""])
    if "footer" in data:
        lines.extend([data["footer"], ""])
    # Consumer-authored Markdown blocks are retained with an explicit LF export.
    body = "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n")
    if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ProcessError("release document exceeds its size limit")
    return body
