from __future__ import annotations

from .contracts import ContractError


AGENTS_START = "<!-- engineering-process:start -->"
AGENTS_END = "<!-- engineering-process:end -->"


def _agents_span(text: str) -> tuple[int, int]:
    if text.count(AGENTS_START) != 1 or text.count(AGENTS_END) != 1:
        raise ContractError("AGENTS.md: invalid engineering-process managed block")
    start = text.index(AGENTS_START)
    try:
        end = text.index(AGENTS_END, start) + len(AGENTS_END)
    except ValueError as error:
        raise ContractError(
            "AGENTS.md: engineering-process managed markers are out of order"
        ) from error
    return start, end


def managed_agents_block(text: str) -> str:
    start, end = _agents_span(text)
    return text[start:end]


def merge_managed_agents(current: str, block: str) -> str:
    canonical = managed_agents_block(block).strip()
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
