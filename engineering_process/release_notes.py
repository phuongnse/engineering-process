"""Render reviewed consumer release records using their selected document standard."""

from __future__ import annotations

from pathlib import Path
import re
import string
from typing import Any
from urllib.parse import quote

from .artifact_standards import ArtifactStandard, MAX_DOCUMENT_BYTES
from .contracts import ProcessError, validate_document
from .distribution import distribution_root, schemas_root


def _text(value: str) -> str:
    return "".join("\\" + character if character in string.punctuation else character for character in " ".join(value.split()))


def _reference(source: str, repository: str | None) -> str:
    source = source.strip()
    if source.startswith(("https://", "http://")):
        issue = re.fullmatch(re.escape(repository) + r"/(?:issues|pull)/([1-9][0-9]*)", source) if repository else None
        label = f"#{issue.group(1)}" if issue else "Source"
        return f"[{label}]({quote(source, safe=':/?#&=%@+~')})"
    # Code spans suppress invented GitHub issue, mention and commit links.
    source = " ".join(source.split())
    fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", source)), default=0))
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
            reference = _reference(change["source"], data.get("repositoryUrl"))
            lines.append(f"- {_text(change['summary'])} ({reference})")
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
