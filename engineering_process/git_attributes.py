from __future__ import annotations

import re

from .contracts import ContractError


ATTRIBUTES_START = "# engineering-process:attributes:start"
ATTRIBUTES_END = "# engineering-process:attributes:end"
MANAGED_SKILLS_ATTRIBUTES = "/.agents/skills/** text=auto eol=lf"


def _normalized_text(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _managed_span(text: str) -> tuple[int, int]:
    if text.count(ATTRIBUTES_START) != 1 or text.count(ATTRIBUTES_END) != 1:
        raise ContractError(
            ".gitattributes: invalid engineering-process managed block"
        )
    starts = list(re.finditer(rf"(?m)^{re.escape(ATTRIBUTES_START)}$", text))
    ends = list(re.finditer(rf"(?m)^{re.escape(ATTRIBUTES_END)}$", text))
    if len(starts) != 1 or len(ends) != 1:
        raise ContractError(
            ".gitattributes: managed markers must each occupy their own line"
        )
    if starts[0].end() >= ends[0].start():
        raise ContractError(
            ".gitattributes: engineering-process managed markers are out of order"
        )
    return starts[0].start(), ends[0].end()


def canonical_attributes_block() -> str:
    return "\n".join(
        (ATTRIBUTES_START, MANAGED_SKILLS_ATTRIBUTES, ATTRIBUTES_END)
    )


def managed_attributes_issues(text: str) -> list[str]:
    normalized = _normalized_text(text)
    try:
        start, end = _managed_span(normalized)
    except ContractError as error:
        return [str(error)]
    issues: list[str] = []
    if normalized[start:end] != canonical_attributes_block():
        issues.append(
            ".gitattributes: managed block differs from the pinned distribution"
        )
    if normalized[end:].strip():
        issues.append(
            ".gitattributes: managed block must be the final non-whitespace content"
        )
    return issues


def merge_managed_attributes(current: str) -> str:
    normalized = _normalized_text(current)
    start_count = normalized.count(ATTRIBUTES_START)
    end_count = normalized.count(ATTRIBUTES_END)
    project_content = normalized.strip()
    if start_count or end_count:
        start, end = _managed_span(normalized)
        project_content = "\n\n".join(
            part
            for part in (normalized[:start].strip(), normalized[end:].strip())
            if part
        )
    parts = [part for part in (project_content, canonical_attributes_block()) if part]
    return "\n\n".join(parts) + "\n"
