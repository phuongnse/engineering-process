from __future__ import annotations

import re

from .contracts import ContractError
from .markdown import (
    contains_raw_html,
    mask_nonvisible_markdown_blocks,
    strip_html_comments,
)


AGENTS_START = "<!-- engineering-process:start -->"
AGENTS_END = "<!-- engineering-process:end -->"
_AGENTS_START_TOKEN = "ENGINEERING_PROCESS_MANAGED_AGENTS_START"
_AGENTS_END_TOKEN = "ENGINEERING_PROCESS_MANAGED_AGENTS_END"


def _normalized_markdown(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _agents_span(text: str) -> tuple[int, int]:
    if text.count(AGENTS_START) != 1 or text.count(AGENTS_END) != 1:
        raise ContractError("AGENTS.md: invalid engineering-process managed block")
    starts = list(re.finditer(rf"(?m)^{re.escape(AGENTS_START)}$", text))
    ends = list(re.finditer(rf"(?m)^{re.escape(AGENTS_END)}$", text))
    if len(starts) != 1 or len(ends) != 1:
        raise ContractError("AGENTS.md: managed markers must each occupy their own line")
    if starts[0].end() >= ends[0].start():
        raise ContractError(
            "AGENTS.md: engineering-process managed markers are out of order"
        )
    return starts[0].start(), ends[0].end()


def managed_agents_block(text: str) -> str:
    normalized = _normalized_markdown(text)
    start, end = _agents_span(normalized)
    return normalized[start:end]


def managed_agents_visibility_issues(text: str) -> list[str]:
    text = _normalized_markdown(text)
    try:
        _agents_span(text)
    except ContractError as error:
        return [str(error)]
    if _AGENTS_START_TOKEN in text or _AGENTS_END_TOKEN in text:
        return ["AGENTS.md contains reserved engineering-process marker text"]
    visible = text.replace(AGENTS_START, _AGENTS_START_TOKEN).replace(
        AGENTS_END, _AGENTS_END_TOKEN
    )
    visible, malformed_comments = strip_html_comments(visible)
    if malformed_comments:
        return ["AGENTS.md contains an unterminated or malformed HTML comment"]
    if contains_raw_html(visible):
        return [
            "AGENTS.md must not contain raw HTML; use visible CommonMark outside "
            "the managed markers"
        ]
    structural = mask_nonvisible_markdown_blocks(visible)
    lines = structural.splitlines()
    starts = [
        index for index, line in enumerate(lines) if line == _AGENTS_START_TOKEN
    ]
    ends = [index for index, line in enumerate(lines) if line == _AGENTS_END_TOKEN]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return [
            "AGENTS.md managed block must be visible and outside comments, code fences, "
            "or non-visible Markdown blocks"
        ]
    return []


def merge_managed_agents(current: str, block: str) -> str:
    canonical = managed_agents_block(block).strip()
    current = _normalized_markdown(current)
    start_count = current.count(AGENTS_START)
    end_count = current.count(AGENTS_END)
    if start_count == 0 and end_count == 0:
        if not current.strip():
            return canonical + "\n"
        return current.rstrip() + "\n\n" + canonical + "\n"
    start, end = _agents_span(current)
    prefix = current[:start].rstrip()
    suffix = current[end:].strip()
    parts = [part for part in (prefix, canonical, suffix) if part]
    return "\n\n".join(parts) + "\n"
