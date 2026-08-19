from __future__ import annotations

import re


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE_RE = re.compile(
    r"^ {0,3}(?:(?P<backticks>`{3,})(?P<backtick_info>[^`]*)|"
    r"(?P<tildes>~{3,})[^\r\n]*)$"
)
RAW_CONTAINER_RE = re.compile(
    r"^ {0,3}<(?P<tag>address|article|aside|blockquote|body|details|dialog|div|"
    r"fieldset|figure|footer|form|header|html|iframe|main|menu|nav|ol|pre|"
    r"script|section|style|table|textarea|ul)(?:\s|>|/)",
    re.IGNORECASE,
)


def strip_html_comments(text: str) -> tuple[str, bool]:
    visible = COMMENT_RE.sub("", text)
    malformed = "<!--" in visible or "-->" in visible
    return visible, malformed


def mask_fenced_code(text: str) -> str:
    visible: list[str] = []
    active_character: str | None = None
    active_length = 0
    for line in text.splitlines():
        match = FENCE_RE.fullmatch(line)
        if active_character is None:
            if match is None:
                visible.append(line)
                continue
            fence = match.group("backticks") or match.group("tildes")
            active_character = fence[0]
            active_length = len(fence)
            visible.append("")
            continue
        stripped = line.strip()
        if (
            stripped
            and set(stripped) == {active_character}
            and len(stripped) >= active_length
        ):
            active_character = None
            active_length = 0
        visible.append("")
    return "\n".join(visible)


def mask_raw_html_containers(text: str) -> str:
    visible: list[str] = []
    active_tag: str | None = None
    for line in text.splitlines():
        if active_tag is not None:
            visible.append("")
            if re.search(rf"</{re.escape(active_tag)}\s*>", line, re.IGNORECASE):
                active_tag = None
            continue
        match = RAW_CONTAINER_RE.match(line)
        if match is None:
            visible.append(line)
            continue
        visible.append("")
        tag = match.group("tag").casefold()
        if "/>" in line or re.search(
            rf"</{re.escape(tag)}\s*>", line[match.end() :], re.IGNORECASE
        ):
            continue
        active_tag = tag
    return "\n".join(visible)
