from __future__ import annotations

import re


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*$")


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
            fence = match.group("fence")
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
